#!/usr/bin/env python3
"""Run an exam task set through /work at one agent-workflow commit, graded by hidden tests.

    run_exam.py --harness <ref|sha> --tasks <manifest> --budget-usd X
                [--screen] [--tries N] [--agent workflow|oracle|nop|controls]
                [--kind baseline|candidate|harm|controls|pick] [--model opus|sonnet|<id>]
                [--max-budget-usd 12.05] [--concurrency K] [--env daytona]

Each task x try runs `harbor run` (WorkflowAgent, or Harbor's own oracle / nop
agents; `controls` runs both) and becomes a try folder made by
.claude/skills/coordinator/save_try.py --from-files, with the coordinator's own
try folders under inner/ and a result.json:
  {task, try, status: ok|budget_cut|error, reward, cost_usd, wall_s, harness_commit,
   model, origin_url, bead_branch_in_origin, exam_no_pr_line, harbor_trial_dir}
Rewards count only status ok tries.

Layout: $HARNESS_LOOP_HOME (default ~/.harness-loop)/runs/<run-id>/
  run.json  summary.json  summary.sha256  <task>/try-<n>/  harbor/ (Harbor's job dirs)
  (controls: <agent>/<task>/try-<n>/). run-id = <utc-date>-<sha7>-<set>[-screen][-<agent>],
  plus -2, -3 ... when that dir exists. At the end the run is sealed (compare.seal)
  and made read-only.

Budget: before each run, if spent + (runs in flight + 1) x the mean cost so far
($15 before the first workflow result) would exceed --budget-usd, nothing more
starts and the run is marked incomplete. Each workflow run gets
max_budget_usd = --max-budget-usd (default 12.05 = 3 x the B18.2 probe's max);
a run that hits it is status budget_cut.

Env: EXAM_HARBOR overrides the `harbor run` command prefix (tests use a fake).
"""
import argparse
import datetime
import json
import os
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
MAX_CPUS, MAX_MEMORY_MB = 4, 8192  # Daytona's per-sandbox cap
MODELS = {"opus": trial_export.BEDROCK_MODELS["ANTHROPIC_MODEL"],
          "sonnet": trial_export.BEDROCK_MODELS["ANTHROPIC_DEFAULT_SONNET_MODEL"]}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def branch_of(repo, ref):
    """The branch name if ref names a local branch, else None."""
    p = subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"])
    return ref if p.returncode == 0 else None


def resource_overrides(task_dir):
    """Harbor flags capping a task that asks Daytona for more than it allows."""
    toml = Path(task_dir) / "task.toml"
    if not toml.is_file():
        return []
    data = tomllib.loads(toml.read_text())
    envs = [data.get("environment", {}), data.get("verifier", {}).get("environment", {})]
    if any(e.get("cpus", 0) > MAX_CPUS or e.get("memory_mb", 0) > MAX_MEMORY_MB for e in envs):
        return ["--override-cpus", str(MAX_CPUS), "--override-memory-mb", str(MAX_MEMORY_MB)]
    return []


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


def harbor_cmd(args, task, agent, jobs_dir, job_name, sha, run_id):
    cmd = shlex.split(os.environ.get("EXAM_HARBOR", DEFAULT_HARBOR))
    cmd += ["-p", task["task_dir"], "-e", args.env, "-o", str(jobs_dir), "--job-name", job_name,
            "-y", "-q", *resource_overrides(task["task_dir"])]
    if agent != "workflow":
        return cmd + ["-a", agent]
    cmd += ["-a", "workflow_agent:WorkflowAgent",
            "--agent-timeout-multiplier", "4", "--agent-setup-timeout-multiplier", "3"]
    kw = {"harness_commit": sha, "max_budget_usd": args.max_budget_usd, "title": task["title"],
          "workdir": task["workdir"], "model": MODELS.get(args.model, args.model), "run_name": run_id}
    if task["test_command"]:
        kw["test_command"] = task["test_command"]
    for k, v in kw.items():
        cmd += ["--ak", f"{k}={v}"]
    return cmd


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
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(EXAM_DIR), os.environ.get("PYTHONPATH")]))}
    t0 = time.monotonic()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    wall = round(time.monotonic() - t0, 1)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job_name}.log").write_text(f"$ {shlex.join(cmd)}\nexit {p.returncode}\n{p.stdout}\n{p.stderr}")
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
            log = trial / "agent" / f"{agent}.txt"
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
        cost = exam.get("total_cost_usd")
        if cost is None:
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
        "reward": reward, "cost_usd": cost, "wall_s": wall,
        "harness_commit": exam.get("harness_commit") or ctx["sha"],
        "model": MODELS.get(ctx["args"].model, ctx["args"].model) if agent == "workflow" else None,
        "origin_url": exam.get("origin_url"), "bead_branch_in_origin": bool(exam.get("branch")),
        "exam_no_pr_line": bool(exam.get("exam_no_pr_line")),
        "harbor_trial_dir": str(trial) if trial else None,
    }
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def schedule(ctx, jobs):
    """Run jobs (task, try, agent) up to --concurrency at once inside the budget. True if all ran."""
    args = ctx["args"]
    done, pending = [], set()
    first = FIRST_ESTIMATE_USD if args.agent == "workflow" else 0.0
    queue = list(jobs)
    with ThreadPoolExecutor(args.concurrency) as ex:
        while queue or pending:
            while queue and len(pending) < args.concurrency:
                spent = sum(r["cost_usd"] for r in done)
                est = statistics.fmean(r["cost_usd"] for r in done) if done else first
                if spent + est * (len(pending) + 1) > args.budget_usd:
                    print(f"run_exam: budget stop: spent ${spent:.2f} + {len(pending) + 1} x ${est:.2f} "
                          f"> ${args.budget_usd}; {len(queue)} run(s) not started", file=sys.stderr)
                    ctx["not_started"] = len(queue)
                    queue.clear()
                    break
                pending.add(ex.submit(run_try, ctx, *queue.pop(0)))
            if pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    r = f.result()
                    done.append(r)
                    print(f"run_exam: {r['agent']} {r['task']} try-{r['try']}: {r['status']} "
                          f"reward={r['reward']} ${r['cost_usd']}", file=sys.stderr)
    return not ctx.get("not_started")


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
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--env", default="daytona")
    args = ap.parse_args(argv)

    sha = git(AW_ROOT, "rev-parse", "--verify", f"{args.harness}^{{commit}}")
    data = manifest.load(args.tasks, require_cache=True)
    tasks = [t for t in data["tasks"] if t["screen"] or not args.screen]
    if not tasks:
        sys.exit(f"run_exam: no {'screen ' if args.screen else ''}tasks in {args.tasks}")
    if args.agent == "workflow":
        # Fail before any spend if the harness at this sha does not install.
        with tempfile.TemporaryDirectory(prefix="exam-harness-") as tmp:
            subprocess.run([sys.executable, str(INSTALLER), "--repo", str(AW_ROOT), "--commit", sha,
                            "--target", tmp], check=True, capture_output=True, text=True)

    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    base = f"{date}-{sha[:7]}-{data['set']}{'-screen' if args.screen else ''}"
    if args.agent != "workflow":
        base += f"-{args.agent}"
    home = Path(os.environ.get("HARNESS_LOOP_HOME", Path.home() / ".harness-loop"))
    run = new_run_dir(home / "runs", base)
    controls = args.agent == "controls"
    meta = {"run_id": run.name, "kind": args.kind or ("controls" if args.agent != "workflow" else "candidate"),
            "harness_ref": args.harness, "branch": branch_of(AW_ROOT, args.harness), "harness_commit": sha,
            "set": data["set"], "manifest": str(Path(args.tasks).resolve()), "screen": args.screen,
            "agent": args.agent, "model": MODELS.get(args.model, args.model) if args.agent == "workflow" else None,
            "tries": args.tries, "tasks": sorted(t["id"] for t in tasks), "budget_usd": args.budget_usd,
            "max_budget_usd": args.max_budget_usd, "status": "running",
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    (run / "run.json").write_text(json.dumps(meta, indent=2) + "\n")
    agents = ["oracle", "nop"] if controls else [args.agent]
    jobs = [(t, n, a) for n in range(1, args.tries + 1) for t in tasks for a in agents]
    ctx = {"args": args, "run": run, "sha": sha, "controls": controls}
    finish(ctx, meta, schedule(ctx, jobs))


if __name__ == "__main__":
    main()
