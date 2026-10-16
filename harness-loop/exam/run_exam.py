#!/usr/bin/env python3
"""Run an exam task set through /work at one agent-workflow commit, graded by hidden tests.

    run_exam.py --harness <ref|sha> --tasks <manifest> --budget-usd X
                [--screen] [--tries N] [--agent workflow|oracle|nop|controls]
                [--kind baseline|candidate|harm|controls|pick] [--model opus|sonnet|<id>]
                [--max-budget-usd 12.05] [--concurrency K] [--env docker|daytona] [--addon <dir>]
                [--early-stop-vs <sealed baseline run>]

Each task x try runs `harbor run` (WorkflowAgent, or Harbor's own oracle / nop
agents; `controls` runs both) and becomes a try folder made by
.claude/skills/coordinator/save_try.py --from-files, with the coordinator's own
try folders under inner/ and a result.json:
  {task, try, status: ok|budget_cut|rate_limited|error, reward, cost_usd, wall_s, harness_commit,
   harness_version, model, origin_url, bead_branch_in_origin, exam_no_pr_line, harbor_trial_dir}
Rewards count only status ok tries. A workflow try is an error when its
recorded harness_commit is not the requested sha, or its harness_version (the
.claude/HARNESS_VERSION its own install wrote, from agent/exam/result.json)
differs from the preflight install's. One with no cost (killed
before claude's result line) is booked at max_budget_usd with cost_unknown.
A try whose filing raises is saved as an error and the run marked incomplete.
Exit 1 when every try errored (the run is still sealed).

--addon <dir>: an add-on dir (e.g. a private one holding a private task set)
installed over the harness by the preflight install and by WorkflowAgent
(`--ak harness_addon=<abs dir>`). The preflight install's .claude/HARNESS_VERSION
({commit, addon, addon_sha256}) is recorded as harness_version in run.json; each
workflow result.json records the try's own (a crashed try: the preflight's). Oracle/nop/controls install no harness: harness_version is
null there and --addon is only recorded (run.json `addon`).

Layout: $HARNESS_LOOP_HOME (default ~/.harness-loop; an --addon run refuses that default)/runs/<run-id>/
  run.json  summary.json  summary.sha256  <task>/try-<n>/  harbor/ (Harbor's job dirs)
  (controls: <agent>/<task>/try-<n>/). run-id = <utc-date>-<sha7>-<set>[-screen][-<agent>],
  plus -2, -3 ... when that dir exists. At the end the run is sealed (compare.seal)
  and made read-only.

Budget: before each run, if spent + (runs in flight + 1) x the mean cost so far
($15 before the first workflow result) would exceed --budget-usd, nothing more
starts and the run is marked incomplete. Each workflow run gets
max_budget_usd = --max-budget-usd (default 12.05 = 3 x the B18.2 probe's max);
a run that hits it is status budget_cut.

Early stop (--early-stop-vs, --screen workflow runs only): tasks run cheapest first by the
baseline's per-task cost (tasks the baseline has no ok try for last), and the first try runs alone
before any other slot fills, so it is a cheap dry run of the candidate. After each batch of finished
tries, compare.screen_fail_decided asks whether the
screen rule can still pass; once it cannot, nothing more starts, runs in flight finish, and the run
is marked incomplete with run.json early_stop = the reason (compare.py's screen then fails it).

Where tasks run: --env docker (default) builds each task's container on this
machine with the local Docker engine; --env daytona rents a cloud sandbox. Either
way each task x try gets a fresh container that Harbor deletes afterwards; with
docker, run_exam also removes any container or network of that try Harbor left.
--concurrency defaults to what fits in Docker (local_concurrency) or 1 on daytona.

Model route: HARNESS_MODEL_ACCESS (trial_export.model_route): the Claude Code
subscription by default, whose sandboxes need CLAUDE_CODE_OAUTH_TOKEN from
`claude setup-token` (they cannot read this machine's keychain login); bedrock
only when set to bedrock; api (ANTHROPIC_API_KEY) only when loop.py's exam
credential chain picked it. Checked before any try starts; never switched on a
failure. A try that hits a usage limit (status rate_limited) stops the run like
a budget stop: nothing more starts, the run is incomplete and run.json
rate_limited holds claude's message, so a limit never reads as an unsolved task.

Env: Harbor gets only HARBOR_ENV, plus the route's model credential for workflow
runs (CLAUDE_CODE_OAUTH_TOKEN, the AWS keys, or ANTHROPIC_API_KEY), plus DAYTONA_* (daytona) or
DOCKER_* (docker): Harbor copies some host vars into the sandbox. EXAM_HARBOR / EXAM_DOCKER override the `harbor run` / `docker` command
prefixes (tests use fakes).
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

EXAM_DIR = Path(__file__).resolve().parent
LOOP_DIR = EXAM_DIR.parent
AW_ROOT = LOOP_DIR.parent
sys.path.insert(0, str(EXAM_DIR))
sys.path.insert(0, str(LOOP_DIR / "tasks"))
import compare  # noqa: E402
import manifest  # noqa: E402
import trial_export  # noqa: E402

SAVE_TRY = AW_ROOT / ".claude/skills/coordinator/save_try.py"
INSTALLER = LOOP_DIR / "install_harness.py"
DEFAULT_HARBOR = f"{LOOP_DIR / '.venv/bin/harbor'} run"
FIRST_ESTIMATE_USD = 15.0
DAYTONA_CPUS, DAYTONA_MEMORY_MB = 4, 8192  # Daytona's per-sandbox cap
# What nearly every task asks for (task.toml [environment]), and the memory left
# to the Docker engine itself, for local_concurrency.
TASK_CPUS, TASK_MEMORY_MB, DOCKER_RESERVE_MB = 2, 4096, 1024
# The only host env Harbor sees. Harbor copies some host vars (e.g.
# CLAUDE_CODE_MAX_OUTPUT_TOKENS, DISABLE_ADAPTIVE_THINKING) into the sandbox,
# which would make a baseline and a candidate differ by who launched them.
HARBOR_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "EXAM_HARBOR")
# The route's model credential, for WorkflowAgent only: oracle and nop never call a model.
MODEL_ENV = {"subscription": (trial_export.OAUTH_TOKEN,), "api": ("ANTHROPIC_API_KEY",),
             "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION")}
# Where the docker CLI finds the engine; none of these holds a secret.
DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
MODELS = {"bedrock": {"opus": trial_export.BEDROCK_MODELS["ANTHROPIC_MODEL"],
                      "sonnet": trial_export.BEDROCK_MODELS["ANTHROPIC_DEFAULT_SONNET_MODEL"]},
          "subscription": trial_export.API_MODELS, "api": trial_export.API_MODELS}


def model_id(args):
    """The model id --model names on this run's route (an id passes through)."""
    return MODELS[args.route].get(args.model, args.model)


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def branch_of(repo, ref):
    """The branch name if ref names a local branch, else None."""
    p = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"])
    return ref if p.returncode == 0 else None


def resource_overrides(task_dir, max_cpus, max_memory_mb):
    """Harbor flags capping a task that asks for more CPUs or memory than one sandbox has."""
    toml = Path(task_dir) / "task.toml"
    if not toml.is_file():
        return []
    data = tomllib.loads(toml.read_text())
    envs = [data.get("environment", {}), data.get("verifier", {}).get("environment", {})]
    cpus = max(e.get("cpus", 0) for e in envs)
    memory = max(e.get("memory_mb", 0) for e in envs)
    if cpus > max_cpus or memory > max_memory_mb:
        return ["--override-cpus", str(min(cpus, max_cpus)) if cpus else str(max_cpus),
                "--override-memory-mb", str(min(memory, max_memory_mb)) if memory else str(max_memory_mb)]
    return []


def docker(*args, timeout=120):
    cmd = [*shlex.split(os.environ.get("EXAM_DOCKER", "docker")), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def docker_capacity():
    """(cpus, memory_mb) the local Docker engine gives containers; exits if it is not reachable."""
    try:
        p = docker("info", "--format", "{{.NCPU}} {{.MemTotal}}", timeout=60)
    except subprocess.TimeoutExpired:
        sys.exit("run_exam: `docker info` hung for 60 s; quit and reopen Docker Desktop, then retry")
    except FileNotFoundError:
        sys.exit("run_exam: no docker CLI; install Docker Desktop or pass --env daytona")
    if p.returncode:
        sys.exit(f"run_exam: Docker is not running (open Docker Desktop, or pass --env daytona): "
                 f"{p.stderr.strip()[-300:]}")
    try:
        cpus, mem = p.stdout.split()
        return int(cpus), int(mem) // 2**20
    except ValueError:
        sys.exit(f"run_exam: unexpected `docker info` output: {p.stdout.strip()[:200]!r}")


def local_concurrency(cpus, memory_mb):
    """How many 2-CPU / 4 GiB tasks fit in Docker at once, keeping 1 GiB for the engine (min 1)."""
    return max(1, min(cpus // TASK_CPUS, (memory_mb - DOCKER_RESERVE_MB) // TASK_MEMORY_MB))


def compose_project(name):
    """Harbor's compose project name for a session id (its _sanitize_docker_compose_project_name)."""
    name = name.lower()
    if not re.match(r"^[a-z0-9]", name):
        name = "0" + name
    return re.sub(r"[^a-z0-9_-]", "-", name)


def remove_leftovers(job_dir):
    """Remove containers and networks Harbor left behind for this job's trials. Returns their ids.

    Harbor runs `docker compose down` itself; this covers a Harbor that crashed or was killed.
    A trial dir (it holds config.json) is named after the trial, and every compose project of
    that trial is `<trial>__env` or `<trial>__verifier__<key>`; the verifier's session id first
    turns characters other than alphanumerics and -._ into _, so both spellings are matched.
    """
    job_dir = Path(job_dir)
    trials = [d.name for d in job_dir.iterdir() if (d / "config.json").is_file()] if job_dir.is_dir() else []
    prefixes = tuple({compose_project(n) + "__" for n in trials}
                     | {compose_project(re.sub(r"[^\w.-]", "_", n)) + "__" for n in trials})
    removed = []
    if not prefixes:
        return removed
    for kind, rm in (("container", ["rm", "-f", "-v"]), ("network", ["network", "rm"])):
        ls = ["ps", "-a"] if kind == "container" else ["network", "ls"]
        p = docker(*ls, "--format", '{{.ID}}\t{{.Label "com.docker.compose.project"}}')
        ids = [row.split("\t")[0] for row in p.stdout.splitlines()
               if "\t" in row and row.split("\t")[1].startswith(prefixes)]
        if ids:
            docker(*rm, *ids)
            removed += ids
    return removed


def new_run_dir(runs, base):
    runs.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        d = runs / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir()
            return d
        except FileExistsError:
            n += 1


def harbor_prefix():
    return shlex.split(os.environ.get("EXAM_HARBOR", DEFAULT_HARBOR))


def harbor_cmd(args, task, agent, jobs_dir, job_name, sha, run_id):
    cmd = harbor_prefix()
    cmd += ["-p", task["task_dir"], "-e", args.env, "-o", str(jobs_dir), "--job-name", job_name,
            "-y", "-q", "--delete", *resource_overrides(task["task_dir"], *args.sandbox_cap)]
    if agent != "workflow":
        return cmd + ["-a", agent]
    cmd += ["-a", "workflow_agent:WorkflowAgent",
            "--agent-timeout-multiplier", "4", "--agent-setup-timeout-multiplier", "3"]
    kw = {"harness_commit": sha, "max_budget_usd": args.max_budget_usd, "title": task["title"],
          "workdir": task["workdir"], "model": model_id(args), "run_name": run_id,
          "model_access": args.route}
    if task["test_command"]:
        kw["test_command"] = task["test_command"]
    if args.addon:
        kw["harness_addon"] = args.addon
    for k, v in kw.items():
        cmd += ["--ak", f"{k}={v}"]
    return cmd


def harbor_env(host, where="docker", agent="workflow", route="subscription"):
    """Allowlisted host env for `harbor run`, with the exam dir on PYTHONPATH."""
    keep = HARBOR_ENV + (MODEL_ENV[route] if agent == "workflow" else ()) + (DOCKER_ENV if where == "docker" else ())
    env = {k: v for k, v in host.items()
           if k in keep or (where == "daytona" and k.startswith("DAYTONA_"))}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(EXAM_DIR), host.get("PYTHONPATH")]))
    return env


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def run_try(ctx, task, n, agent):
    """Run one task x try in Harbor and file it as a try folder. Returns result.json's dict."""
    tries_root, repo_name = (ctx["run"], agent) if ctx["controls"] else (ctx["run"].parent, ctx["run"].name)
    jobs_dir = ctx["run"] / "harbor"
    job_name = f"{agent + '-' if ctx['controls'] else ''}{task['id']}-try{n}"
    cmd = harbor_cmd(ctx["args"], task, agent, jobs_dir, job_name, ctx["sha"], ctx["run"].name)
    t0 = time.monotonic()
    p = subprocess.run(cmd, env=harbor_env(os.environ, ctx["args"].env, agent, ctx["args"].route), capture_output=True, text=True)
    wall = round(time.monotonic() - t0, 1)
    leftovers = []
    if ctx["args"].env == "docker":
        try:
            leftovers = remove_leftovers(jobs_dir / job_name)
        except (OSError, subprocess.SubprocessError) as e:  # keep the try; the log says what failed
            leftovers = [f"(cleanup failed: {e!r})"]
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job_name}.log").write_text(
        f"$ {shlex.join(cmd)}\nexit {p.returncode}\n{p.stdout}\n{p.stderr}"
        + (f"\nremoved leftover docker objects: {' '.join(leftovers)}\n" if leftovers else ""))
    trials = [r.parent for r in sorted((jobs_dir / job_name).glob("*/result.json"))]
    trial = trials[0] if trials else None

    exam, harbor_res = {}, {}
    with tempfile.TemporaryDirectory(prefix="exam-try-") as tmp:
        stage = Path(tmp)
        transcript = None
        if trial:
            trial_export.finalize(trial)  # writes agent/exam/gates.txt from the verifier
            shutil.copytree(trial / "agent/exam", stage, dirs_exist_ok=True)
            exam = read_json(stage / "result.json")
            harbor_res = read_json(trial / "result.json")
            # No parent transcript found: fall back to the agent's own log
            # (WorkflowAgent's stream-json, or Harbor's oracle/nop log).
            name = "claude-code.txt" if agent == "workflow" else f"{agent}.txt"
            log = trial / "agent" / name
            if not (stage / "transcript.jsonl").is_file() and log.is_file() and log.stat().st_size:
                transcript = log
        else:
            (stage / "gates.txt").write_text(f"harbor left no trial dir (exit {p.returncode})\n{p.stderr[-2000:]}")
        for name in ("start.sha", "end.sha"):
            if not (stage / name).is_file():
                (stage / name).write_text(f"none ({agent} agent)\n")
        if not (stage / "diff.patch").is_file():
            (stage / "diff.patch").write_text("")

        reward = ((harbor_res.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if exam:
            status = exam.get("status", "error")
        else:
            status = "ok" if trial else "error"
        if status == "ok" and reward is None:
            status = "error"
        if agent == "workflow" and (exam.get("harness_commit") != ctx["sha"]
                                    or exam.get("harness_version") != ctx["harness_version"]):
            status = "error"  # the agent ran some other harness (or left no record of it)
        cost_unknown = False
        if agent == "workflow":
            cost = exam.get("total_cost_usd")
            if cost is None:  # e.g. killed at the agent timeout: no result line
                cost, cost_unknown = ctx["args"].max_budget_usd, True
        else:
            cost = (harbor_res.get("agent_result") or {}).get("cost_usd") or 0.0
        (stage / "note.md").write_text(
            f"exam {ctx['run'].name}: agent {agent}, status {status}, reward {reward}, "
            f"cost ${cost}, harbor exit {p.returncode}\n")

        save = [sys.executable, str(SAVE_TRY), "--bead", task["id"], "--from-files", str(stage),
                "--repo-name", repo_name, "--result", "pass" if status == "ok" and reward == 1 else "fail"]
        if transcript:
            save += ["--transcript", str(transcript)]
        folder = Path(subprocess.run(save, env={**os.environ, "HARNESS_TRIES_DIR": str(tries_root)},
                                     check=True, capture_output=True, text=True).stdout.strip())
    if trial and (trial / "agent/tries").is_dir():
        shutil.copytree(trial / "agent/tries", folder / "inner")
    result = {
        "task": task["id"], "try": int(folder.name.split("-")[1]), "agent": agent, "status": status,
        "reward": reward, "cost_usd": cost, "cost_unknown": cost_unknown, "wall_s": wall,
        "harness_commit": exam.get("harness_commit") or ctx["sha"],
        "harness_version": exam.get("harness_version") if agent == "workflow" else None,
        "model": model_id(ctx["args"]) if agent == "workflow" else None,
        "origin_url": exam.get("origin_url"), "bead_branch_in_origin": bool(exam.get("branch")),
        "exam_no_pr_line": bool(exam.get("exam_no_pr_line")),
        "harbor_trial_dir": str(trial) if trial else None,
        **({"rate_limited": exam.get("rate_limited") or "usage limit"} if status == "rate_limited" else {}),
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def schedule(ctx, jobs):
    """Run jobs (task, try, agent) up to --concurrency at once inside the budget. True if all ran."""
    args = ctx["args"]
    done, pending, futures = [], set(), {}
    first = FIRST_ESTIMATE_USD if args.agent == "workflow" else 0.0
    queue = list(jobs)
    with ThreadPoolExecutor(args.concurrency) as ex:
        while queue or pending:
            # With early stop the first (cheapest) try runs alone: it is the paid dry run.
            slots = 1 if ctx.get("early") and not done else args.concurrency
            while queue and len(pending) < slots:
                spent = sum(r["cost_usd"] for r in done)
                est = statistics.fmean(r["cost_usd"] for r in done) if done else first
                if spent + est * (len(pending) + 1) > args.budget_usd:
                    if pending:  # wait for a run in flight, then re-check with its real cost
                        break
                    print(f"run_exam: budget stop: spent ${spent:.2f} + {len(pending) + 1} x ${est:.2f} "
                          f"> ${args.budget_usd}; {len(queue)} run(s) not started", file=sys.stderr)
                    ctx["not_started"] = len(queue)
                    queue.clear()
                    break
                job = queue.pop(0)
                fut = ex.submit(run_try, ctx, *job)
                futures[fut] = job
                pending.add(fut)
            if pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    try:
                        r = f.result()
                    except Exception as e:  # noqa: BLE001 - file it, keep the run sealable
                        r = crashed_try(ctx, futures[f], e)
                        ctx["crashed"] = True
                    done.append(r)
                    print(f"run_exam: {r['agent']} {r['task']} try-{r['try']}: {r['status']} "
                          f"reward={r['reward']} ${r['cost_usd']}", file=sys.stderr)
                    if r["status"] == "rate_limited" and not ctx.get("rate_limited"):
                        ctx["rate_limited"] = r.get("rate_limited") or "usage limit"
                        print(f"run_exam: rate limit: {ctx['rate_limited']}; {len(queue)} run(s) not started",
                              file=sys.stderr)
                        queue.clear()
                # Decide once the whole batch is in done: a finished try not yet appended would be in
                # neither done nor left, and its task would drop out of the pairing.
                left = {j[0]["id"] for j in queue} | {futures[f][0]["id"] for f in pending}
                why = ctx.get("early") and queue and compare.screen_fail_decided(
                    ctx["early"], done, left, args.max_budget_usd)
                if why:
                    print(f"run_exam: early stop: {why}; {len(queue)} run(s) not started", file=sys.stderr)
                    ctx["early_stop"] = why
                    queue.clear()
    return not any(ctx.get(k) for k in ("not_started", "crashed", "early_stop", "rate_limited"))


def crashed_try(ctx, job, exc):
    """File a try whose run_exam step raised as status error, booked at the cap."""
    task, _, agent = job
    base = (ctx["run"] / agent if ctx["controls"] else ctx["run"]) / task["id"]
    base.mkdir(parents=True, exist_ok=True)
    k = 1
    while (base / f"try-{k}").exists():
        k += 1
    folder = base / f"try-{k}"
    folder.mkdir()
    (folder / "note.md").write_text(f"run_exam raised while filing this try: {exc!r}\n")
    cost = ctx["args"].max_budget_usd if agent == "workflow" else 0.0
    result = {"task": task["id"], "try": k, "agent": agent, "status": "error", "reward": None,
              "cost_usd": cost, "cost_unknown": agent == "workflow", "wall_s": None,
              "harness_commit": ctx["sha"], "error": repr(exc),
              "harness_version": ctx["harness_version"] if agent == "workflow" else None}
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def score(results):
    """{task: mean reward over ok tries} for one agent's results."""
    by = {}
    for r in results:
        if r["status"] == "ok":
            by.setdefault(r["task"], []).append(float(r["reward"]))
    return {t: statistics.fmean(v) for t, v in sorted(by.items())}


def finish(ctx, meta, complete):
    run = ctx["run"]
    results = [json.loads(f.read_text()) for f in compare.sealed_files(run) if f.name == "result.json"]
    agents = sorted({r["agent"] for r in results}) or [ctx["args"].agent]
    per_agent = {a: score([r for r in results if r["agent"] == a]) for a in agents}
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    status = "complete" if complete else "incomplete"
    if ctx["controls"]:
        rewards = {a: per_agent.get(a, {}) for a in ("oracle", "nop")}
        solved = {a: sum(v >= 0.5 for v in rw.values()) for a, rw in rewards.items()}
        mean = None
    else:
        rewards = per_agent.get(ctx["args"].agent, {})
        solved = sum(v >= 0.5 for v in rewards.values())
        mean = statistics.fmean(rewards.values()) if rewards else None
    total = round(sum(r["cost_usd"] for r in results), 4)
    summary = {"run_id": run.name, "status": status, "solved": solved, "mean_reward": mean,
               "total_cost_usd": total, "per_task_rewards": rewards, "counts": counts}
    (run / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    meta.update(status=status, solved=solved, rewards=rewards, total_cost_usd=total,
                finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    (run / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    compare.seal(run)
    subprocess.run(["chmod", "-R", "a-w", str(run)], check=True)
    print(f"run_exam: {run} {status} solved={solved} cost=${total:.2f}")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--budget-usd", type=float, required=True)
    ap.add_argument("--screen", action="store_true")
    ap.add_argument("--tries", type=int, default=1)
    ap.add_argument("--agent", default="workflow", choices=["workflow", "oracle", "nop", "controls"])
    ap.add_argument("--kind", choices=["baseline", "candidate", "harm", "controls", "pick"])
    ap.add_argument("--model", default="opus")
    ap.add_argument("--max-budget-usd", type=float, default=12.05)
    ap.add_argument("--concurrency", type=int, help="default: what fits in Docker, or 1 on daytona")
    ap.add_argument("--env", default="docker", choices=["docker", "daytona"])
    ap.add_argument("--addon")
    ap.add_argument("--early-stop-vs", help="sealed baseline run: stop once the screen can no longer pass")
    args = ap.parse_args(argv)
    try:
        args.route = trial_export.model_route(os.environ, trial_export.EXAM_ROUTES)
        if args.agent == "workflow":  # the sandbox's credential, checked before any try is booked
            trial_export.sandbox_env(os.environ, args.route, tries_dir="/")
    except (ValueError, RuntimeError) as e:
        sys.exit(f"run_exam: {e}")
    if not shutil.which(harbor_prefix()[0]):  # else every try errors and is booked at max_budget_usd
        sys.exit(f"run_exam: harbor not found ({harbor_prefix()[0]}): create harness-loop/.venv "
                 "or set EXAM_HARBOR")
    if args.early_stop_vs and (not args.screen or args.agent != "workflow"):
        sys.exit("run_exam: --early-stop-vs needs --screen and --agent workflow")
    if args.addon:
        args.addon = str(Path(args.addon).resolve())
        if not Path(args.addon).is_dir():
            sys.exit(f"run_exam: add-on dir not found: {args.addon}")
        loop_home = (Path.home() / ".harness-loop").resolve()
        if Path(os.environ.get("HARNESS_LOOP_HOME", loop_home)).resolve() == loop_home:
            sys.exit("run_exam: an --addon run is private: set HARNESS_LOOP_HOME to a private dir, "
                     "never ~/.harness-loop (the improver reads its runs/)")
    if args.env == "docker":
        cpus, memory_mb = docker_capacity()
        args.sandbox_cap = (cpus, memory_mb - DOCKER_RESERVE_MB)
        auto = local_concurrency(cpus, memory_mb)
    else:
        args.sandbox_cap, auto = (DAYTONA_CPUS, DAYTONA_MEMORY_MB), 1
    args.concurrency = args.concurrency or auto

    sha = git(AW_ROOT, "rev-parse", "--verify", f"{args.harness}^{{commit}}")
    try:
        data = manifest.load(args.tasks, require_cache=True)
    except ValueError as e:
        sys.exit(f"run_exam: {e}")
    tasks = [t for t in data["tasks"] if t["screen"] or not args.screen]
    if not tasks:
        sys.exit(f"run_exam: no {'screen ' if args.screen else ''}tasks in {args.tasks}")
    early = compare.load(args.early_stop_vs) if args.early_stop_vs else None
    if early:  # cheapest first: the first task is the dry run
        tasks.sort(key=lambda t: (not compare.counted(early[0].get(t["id"], []), False, 0),
                                  sum(float(r.get("cost_usd") or 0) for r in early[0].get(t["id"], []))))
    harness_version = None
    if args.agent == "workflow":
        # Fail before any spend if the harness at this sha does not install.
        with tempfile.TemporaryDirectory(prefix="exam-harness-") as tmp:
            subprocess.run([sys.executable, str(INSTALLER), "--repo", str(AW_ROOT), "--commit", sha,
                            "--target", tmp, *(["--addon", args.addon] if args.addon else [])],
                           check=True, capture_output=True, text=True)
            harness_version = json.loads((Path(tmp) / ".claude/HARNESS_VERSION").read_text())

    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    base = f"{date}-{sha[:7]}-{data['set']}{'-screen' if args.screen else ''}"
    if args.agent != "workflow":
        base += f"-{args.agent}"
    home = Path(os.environ.get("HARNESS_LOOP_HOME", Path.home() / ".harness-loop"))
    run = new_run_dir(home / "runs", base)
    controls = args.agent == "controls"
    meta = {"run_id": run.name, "kind": args.kind or ("controls" if args.agent != "workflow" else "candidate"),
            "harness_ref": args.harness, "branch": branch_of(AW_ROOT, args.harness), "harness_commit": sha,
            "harness_version": harness_version, "addon": args.addon, "set": data["set"], "manifest": str(Path(args.tasks).resolve()), "screen": args.screen,
            "agent": args.agent, "model": model_id(args) if args.agent == "workflow" else None, "model_access": args.route,
            "tries": args.tries, "tasks": sorted(t["id"] for t in tasks), "budget_usd": args.budget_usd,
            "max_budget_usd": args.max_budget_usd, "env": args.env, "concurrency": args.concurrency,
            "status": "running",
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (run / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    agents = ["oracle", "nop"] if controls else [args.agent]
    jobs = [(t, n, a) for n in range(1, args.tries + 1) for t in tasks for a in agents]
    ctx = {"args": args, "run": run, "sha": sha, "controls": controls, "harness_version": harness_version,
           "early": early}
    complete = schedule(ctx, jobs)
    if ctx.get("early_stop"):
        meta["early_stop"] = ctx["early_stop"]
    if ctx.get("rate_limited"):
        meta["rate_limited"] = ctx["rate_limited"]
    results = finish(ctx, meta, complete)
    if results and all(r["status"] == "error" for r in results):
        sys.exit("run_exam: every try errored")


if __name__ == "__main__":
    main()
