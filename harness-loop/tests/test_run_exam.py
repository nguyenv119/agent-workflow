"""Tests for harness-loop/exam/run_exam.py with a fake `harbor run` (EXAM_HARBOR env override).

The fake writes the trial dir a real Harbor + WorkflowAgent run leaves behind
(result.json, verifier/reward.txt, agent/exam/*, agent/tries/), driven by a
JSON spec per task, and logs its argv and env keys so what run_exam passes is
visible. Its log and spec paths come first on its own command line, because
run_exam passes Harbor only an allowlisted env. A fake `docker` (EXAM_DOCKER)
answers `docker info` like a 10-CPU / 7.65 GiB Docker Desktop, lists the
containers in its own spec file, and logs every call.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from helpers import make_repo

EXAM = Path(__file__).resolve().parents[1] / "exam"
SCRIPT = EXAM / "run_exam.py"
VENV_PY = EXAM.parent / ".venv/bin/python"
sys.path.insert(0, str(EXAM))
import compare  # noqa: E402
import run_exam  # noqa: E402

FAKE_HARBOR = textwrap.dedent('''\
    import json, os, sys
    from pathlib import Path
    log, spec_path, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": argv, "env": sorted(os.environ)}) + "\\n")
    def opt(*names):
        for i, a in enumerate(argv):
            if a in names:
                return argv[i + 1]
    ak = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--ak")
    task = Path(opt("-p")).name
    agent = opt("-a")
    spec = json.loads(Path(spec_path).read_text()).get(task, {})
    reward = {"oracle": 1, "nop": 0}.get(agent, spec.get("reward", 1))
    if spec.get("crash"):
        (Path(opt("-o")) / opt("--job-name") / "t__abc" / "agent").mkdir(parents=True)
        (Path(opt("-o")) / opt("--job-name") / "t__abc" / "agent" / "exam").write_text("not a dir")
        (Path(opt("-o")) / opt("--job-name") / "t__abc" / "result.json").write_text("{}")
        sys.exit(0)
    cost = 0 if agent in ("oracle", "nop") else spec.get("cost", 8.0)
    trial = Path(opt("-o")) / opt("--job-name") / (task[:20] + "__abc")
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier/reward.txt").write_text(f"{reward}\\n")
    (trial / "verifier/test-stdout.txt").write_text("tests ran\\n")
    (trial / "result.json").write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": reward}},
        "agent_result": {"cost_usd": cost}, "exception_info": None}))
    agent_dir = trial / "agent"
    agent_dir.mkdir()
    if agent in ("oracle", "nop"):
        (agent_dir / f"{agent}.txt").write_text(f"{agent} ran\\n")
        sys.exit(0)
    exam = agent_dir / "exam"
    exam.mkdir()
    branch = spec.get("branch", "feature/bd-exam-1-fix")
    for name, text in {"start.sha": "a" * 40, "end.sha": "b" * 40, "diff.patch": "diff --git a/x b/x\\n",
                       "branch.txt": branch, "origin_url.txt": "file:///tmp/origin.git",
                       "transcript.jsonl": '{"type": "user"}\\n'}.items():
        if name == "transcript.jsonl" and spec.get("no_transcript"):
            continue
        (exam / name).write_text(text + "\\n")
    (agent_dir / "claude-code.txt").write_text('{"type": "result"}\\n')
    subtype = spec.get("subtype", "success")
    (exam / "result.json").write_text(json.dumps({
        "harness_commit": spec.get("harness_commit", ak["harness_commit"]), "branch": branch or None,
        "origin_url": "file:///tmp/origin.git",
        "status": {"success": "ok", "error_max_budget_usd": "budget_cut"}.get(subtype, "error"),
        "total_cost_usd": cost, "exam_no_pr_line": True}))
    inner = agent_dir / "tries/app/exam-1/try-1"
    inner.mkdir(parents=True)
    (inner / "note.md").write_text("inner try\\n")
''')

FAKE_DOCKER = textwrap.dedent('''\
    import json, sys
    log, spec_path, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    spec = json.load(open(spec_path))
    with open(log, "a") as fh:
        fh.write(json.dumps(argv) + "\\n")
    if spec.get("down"):
        sys.exit("Cannot connect to the Docker daemon")
    if argv[0] == "info":
        print(spec.get("info", "10 8217473024"))  # Docker Desktop's default share of a 16 GiB Mac
    elif argv[:2] == ["ps", "-a"]:
        print("\\n".join(spec.get("containers", [])))
    elif argv[:2] == ["network", "ls"]:
        print("\\n".join(spec.get("networks", [])))
''')


class RunExamTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        self.fake = self.root / "fake_harbor.py"
        self.fake.write_text(FAKE_HARBOR)
        self.log = self.root / "harbor.log"
        self.spec = self.root / "spec.json"
        self.spec.write_text("{}")
        self.fake_docker = self.root / "fake_docker.py"
        self.fake_docker.write_text(FAKE_DOCKER)
        self.docker_log = self.root / "docker.log"
        self.docker_spec = self.root / "docker.json"
        self.docker_spec.write_text("{}")
        self.sha = subprocess.run(["git", "-C", str(EXAM), "rev-parse", "HEAD"],
                                  text=True, capture_output=True, check=True).stdout.strip()

    def tearDown(self):
        for dirpath, dirs, files in os.walk(self.root):
            for n in dirs + files:
                p = Path(dirpath) / n
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(self.root)

    def manifest(self, tasks, screen=()):
        """tasks: {task id: cpus}. Writes task folders and a manifest; returns its path."""
        rows = []
        for tid, cpus in tasks.items():
            tdir = self.root / "cache" / tid
            (tdir / "tests").mkdir(parents=True)
            (tdir / "tests/test.sh").write_text("true\n")
            (tdir / "task.toml").write_text(f"[environment]\ncpus = {cpus}\nmemory_mb = 4096\n")
            rows.append({"id": tid, "harbor_path": f"cache/{tid}", "repo": None, "pr": None,
                         "merged_at": None, "language": "python", "kind": "bugfix", "workdir": "/app",
                         "test_command": "pytest -q", "title": f"Fix {tid}", "screen": tid in screen})
        m = self.root / "m.json"
        m.write_text(json.dumps({"set": "demo", "source": "test", "tasks": rows}))
        return m

    def run_exam(self, *args, check=True, extra_env=None):
        env = {**os.environ, **(extra_env or {}), "HARNESS_LOOP_HOME": str(self.home),
               "EXAM_HARBOR": f"{sys.executable} {self.fake} {self.log} {self.spec} run",
               "EXAM_DOCKER": f"{sys.executable} {self.fake_docker} {self.docker_log} {self.docker_spec}"}
        p = subprocess.run([sys.executable, str(SCRIPT), "--harness", "HEAD", *map(str, args)],
                           env=env, text=True, capture_output=True)
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def only_run(self):
        runs = [d for d in (self.home / "runs").iterdir() if d.is_dir()]
        self.assertEqual(len(runs), 1)
        return runs[0]

    def results(self, run):
        return {f.parent.parent.name: json.loads(f.read_text()) for f in run.glob("*/try-*/result.json")}

    def argvs(self):
        return [json.loads(l)["argv"] for l in self.log.read_text().splitlines()]

    def envs(self):
        return [json.loads(l)["env"] for l in self.log.read_text().splitlines()]

    def docker_calls(self):
        if not self.docker_log.exists():
            return []
        return [json.loads(l) for l in self.docker_log.read_text().splitlines()]

    def test_complete_run_writes_one_try_folder_per_task(self):
        """
        Each try becomes a save_try.py folder plus result.json carrying what the
        loop and win check read: the sha that ran, the local origin, whether the
        bead branch reached it, and the no-PR line.
        """
        # GIVEN two tasks, t1 solved at $8, t2 unsolved at $6
        m = self.manifest({"t1": 2, "t2": 2})
        self.spec.write_text(json.dumps({"t1": {"reward": 1, "cost": 8.0}, "t2": {"reward": 0, "cost": 6.0}}))
        # WHEN run_exam runs it as a baseline
        self.run_exam("--tasks", m, "--budget-usd", 100, "--kind", "baseline")
        # THEN every try folder holds a transcript, gates, inner tries and result.json
        run = self.only_run()
        res = self.results(run)
        self.assertEqual(set(res), {"t1", "t2"})
        r1 = res["t1"]
        self.assertEqual((r1["status"], r1["reward"], r1["cost_usd"], r1["try"]), ("ok", 1, 8.0, 1))
        self.assertEqual(r1["harness_commit"], self.sha)
        self.assertTrue(r1["origin_url"].startswith("file://"))
        self.assertIs(r1["bead_branch_in_origin"], True)
        self.assertIs(r1["exam_no_pr_line"], True)
        self.assertTrue(Path(r1["harbor_trial_dir"]).is_dir())
        t1 = run / "t1/try-1"
        self.assertGreater((t1 / "transcript.jsonl").stat().st_size, 0)
        self.assertIn("reward: 1", (t1 / "gates.txt").read_text())
        self.assertTrue((t1 / "inner/app/exam-1/try-1/note.md").is_file())
        # AND run.json has what the win check reads
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual((meta["kind"], meta["status"], meta["solved"], meta["tasks"]),
                         ("baseline", "complete", 1, ["t1", "t2"]))
        self.assertEqual(meta["harness_commit"], self.sha)
        self.assertEqual(meta["rewards"], {"t1": 1.0, "t2": 0.0})
        # AND summary.json totals it
        summ = json.loads((run / "summary.json").read_text())
        self.assertEqual((summ["solved"], summ["total_cost_usd"], summ["counts"]), (1, 14.0, {"ok": 2}))
        # AND the run is sealed and read-only
        self.assertEqual((run / "summary.sha256").read_text(), compare.listing(run))
        self.assertFalse(os.access(t1 / "result.json", os.W_OK))
        self.assertTrue(run.name.endswith(f"-{self.sha[:7]}-demo"))

    def test_budget_stops_before_a_run_that_would_cross_it(self):
        """--budget-usd must never be crossed: stop, and mark the run incomplete."""
        # GIVEN three tasks at $8 each and a $20 budget
        m = self.manifest({"t1": 2, "t2": 2, "t3": 2})
        # WHEN run_exam runs (first estimate $15, then the $8 mean: 0+15, 8+8 ok; 16+8 > 20)
        self.run_exam("--tasks", m, "--budget-usd", 20)
        # THEN two tasks ran and the run is incomplete
        run = self.only_run()
        self.assertEqual(len(self.results(run)), 2)
        self.assertEqual(json.loads((run / "run.json").read_text())["status"], "incomplete")
        self.assertEqual(json.loads((run / "summary.json").read_text())["status"], "incomplete")

    def test_default_estimate_blocks_the_first_run(self):
        """Before any result the estimate is $15, so a $10 budget runs nothing."""
        # GIVEN one task and a $10 budget
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 10)
        # THEN Harbor never ran and the run is incomplete
        self.assertFalse(self.log.exists())
        self.assertEqual(json.loads((self.only_run() / "run.json").read_text())["status"], "incomplete")

    def test_budget_cut_try_reward_does_not_count(self):
        """A try stopped by max_budget_usd is budget_cut; its reward is not a solve."""
        # GIVEN a task that hits the per-run cap but whose verifier still says 1
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"reward": 1, "cost": 12.1, "subtype": "error_max_budget_usd"}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN the try is budget_cut and the run solved nothing
        run = self.only_run()
        self.assertEqual(self.results(run)["t1"]["status"], "budget_cut")
        summ = json.loads((run / "summary.json").read_text())
        self.assertEqual((summ["solved"], summ["counts"]), (0, {"budget_cut": 1}))

    def test_ok_run_without_a_verifier_reward_is_an_error(self):
        """/work finishing is not a score: with no verifier reward the try is an error."""
        # GIVEN a task whose agent finished but whose verifier produced no reward
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"reward": None}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100, check=False)
        # THEN the try is an error and the task has no reward in run.json
        run = self.only_run()
        self.assertEqual(self.results(run)["t1"]["status"], "error")
        self.assertEqual(json.loads((run / "run.json").read_text())["rewards"], {})

    def test_harbor_sees_only_allowlisted_env(self):
        """
        Harbor copies some host vars into the sandbox, so whoever launches a run
        must not change what it measures, and admin keys must not leave the host.
        """
        # GIVEN a host env with an admin key, an API key and a claude tuning var
        m = self.manifest({"t1": 2})
        planted = {"AWS_ADMIN_X": "x", "ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1",
                   "AWS_ACCESS_KEY_ID": "id", "DAYTONA_API_KEY": "d"}
        # WHEN run_exam runs on Daytona
        self.run_exam("--tasks", m, "--budget-usd", 100, "--env", "daytona", extra_env=planted)
        # THEN none of the planted vars reached Harbor, but the Bedrock and Daytona keys did
        env = self.envs()[0]
        for k in ("AWS_ADMIN_X", "ANTHROPIC_API_KEY", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
            self.assertNotIn(k, env)
        self.assertIn("AWS_ACCESS_KEY_ID", env)
        self.assertIn("DAYTONA_API_KEY", env)

    def test_missing_cost_is_booked_at_the_cap(self):
        """A run killed before its result line has an unknown cost; booking $0 would hide it."""
        # GIVEN a workflow try whose stream has no total_cost_usd
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"cost": None, "subtype": None}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100, check=False)
        # THEN the try is booked at the $12.05 cap and flagged
        r = self.results(self.only_run())["t1"]
        self.assertEqual((r["cost_usd"], r["cost_unknown"]), (12.05, True))

    def test_transcript_falls_back_to_claude_code_log(self):
        """With no parent transcript found, the try keeps the agent's own stream-json."""
        # GIVEN a workflow try whose export has no transcript.jsonl
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"no_transcript": True}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN the try's transcript is agent/claude-code.txt
        self.assertEqual((self.only_run() / "t1/try-1/transcript.jsonl").read_text(), '{"type": "result"}\n')

    def test_wrong_harness_commit_is_an_error(self):
        """A try that ran some other harness than the one requested must not be scored."""
        # GIVEN a task whose agent recorded a different sha
        m = self.manifest({"t1": 2, "t2": 2})
        self.spec.write_text(json.dumps({"t1": {"harness_commit": "f" * 40}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN t1 is an error and t2 is ok
        res = self.results(self.only_run())
        self.assertEqual((res["t1"]["status"], res["t2"]["status"]), ("error", "ok"))

    def test_every_try_errored_exits_nonzero(self):
        """A run where nothing worked must not look like a run that scored 0."""
        # GIVEN one task whose verifier gives no reward
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"reward": None}}))
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, check=False)
        # THEN it exits non-zero, and the run is still sealed
        self.assertNotEqual(p.returncode, 0)
        run = self.only_run()
        self.assertEqual((run / "summary.sha256").read_text(), compare.listing(run))

    def test_a_raising_try_is_filed_as_error_and_the_run_still_seals(self):
        """One broken trial dir must not leave the whole run unsealed."""
        # GIVEN two tasks, t1's trial export broken so filing it raises
        m = self.manifest({"t1": 2, "t2": 2})
        self.spec.write_text(json.dumps({"t1": {"crash": True}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN t1 is an error, t2 ok, the run incomplete and sealed
        run = self.only_run()
        res = self.results(run)
        self.assertEqual((res["t1"]["status"], res["t2"]["status"]), ("error", "ok"))
        self.assertEqual(json.loads((run / "run.json").read_text())["status"], "incomplete")
        self.assertEqual((run / "summary.sha256").read_text(), compare.listing(run))

    def test_budget_counts_runs_in_flight(self):
        """
        A start counts the runs still in flight; when that would cross the budget it
        waits for one to finish and re-checks with the real cost, stopping only when
        nothing is in flight.
        """
        # GIVEN four $8 tasks, concurrency 4 and a $30 budget (first estimate $15)
        m = self.manifest({"t1": 2, "t2": 2, "t3": 2, "t4": 2})
        # WHEN run_exam runs: t1, t2 start (0 + 2 x 15 = 30); t3 waits, then starts at
        # 8 + 2 x 8 or 16 + 8; t4 never fits (32 > 30). Not counting in-flight runs
        # would start all 4; stopping instead of waiting would run only 2.
        self.run_exam("--tasks", m, "--budget-usd", 30, "--concurrency", 4)
        # THEN exactly three tries ran and the run is incomplete
        run = self.only_run()
        self.assertEqual(len(self.results(run)), 3)
        self.assertEqual(json.loads((run / "run.json").read_text())["status"], "incomplete")

    def test_missing_bead_branch_is_recorded(self):
        """bead_branch_in_origin is false when finish_task found no branch on origin."""
        # GIVEN a task whose run pushed no branch
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"branch": ""}}))
        # WHEN run_exam runs / THEN the result says the branch is not in origin
        self.run_exam("--tasks", m, "--budget-usd", 100)
        self.assertIs(self.results(self.only_run())["t1"]["bead_branch_in_origin"], False)

    def test_harbor_flags_cap_cpus_and_pass_the_run_cap(self):
        """Daytona caps sandboxes at 4 CPUs; every workflow run gets max_budget_usd."""
        # GIVEN one task asking for 6 CPUs and one asking for 2
        m = self.manifest({"big": 6, "small": 2})
        # WHEN run_exam runs on Daytona with the default per-run cap
        self.run_exam("--tasks", m, "--budget-usd", 100, "--env", "daytona")
        # THEN only the big task gets the CPU/memory override
        by_task = {Path(a[a.index("-p") + 1]).name: a for a in self.argvs()}
        big, small = by_task["big"], by_task["small"]
        self.assertEqual(big[big.index("--override-cpus") + 1], "4")
        self.assertEqual(big[big.index("--override-memory-mb") + 1], "4096")  # what it asked for
        self.assertNotIn("--override-cpus", small)
        # AND both get the workflow agent, the sha and the $12.05 cap
        for a in (big, small):
            self.assertEqual(a[a.index("--agent") + 1] if "--agent" in a else a[a.index("-a") + 1],
                             "workflow_agent:WorkflowAgent")
            self.assertIn(f"harness_commit={self.sha}", a)
            self.assertIn("max_budget_usd=12.05", a)
            self.assertIn("workdir=/app", a)

    def test_screen_flag_runs_only_screen_tasks(self):
        """--screen runs the manifest's screen tasks and names the run -screen."""
        # GIVEN two tasks, one marked screen
        m = self.manifest({"t1": 2, "t2": 2}, screen=("t2",))
        # WHEN run_exam --screen runs
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen")
        # THEN only t2 ran
        run = self.only_run()
        self.assertEqual(set(self.results(run)), {"t2"})
        self.assertTrue(run.name.endswith("-demo-screen"))

    def test_controls_run_records_oracle_and_nop_rewards(self):
        """One controls run holds both Harbor control agents, keyed per agent."""
        # GIVEN two tasks
        m = self.manifest({"t1": 2, "t2": 2})
        # WHEN run_exam --agent controls runs
        self.run_exam("--tasks", m, "--budget-usd", 10, "--agent", "controls")
        # THEN run.json has oracle=1 and nop=0 on both tasks, try folders per agent
        run = self.only_run()
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual(meta["kind"], "controls")
        self.assertEqual(meta["rewards"], {"oracle": {"t1": 1.0, "t2": 1.0}, "nop": {"t1": 0.0, "t2": 0.0}})
        self.assertTrue((run / "oracle/t1/try-1/result.json").is_file())
        self.assertGreater((run / "nop/t2/try-1/transcript.jsonl").stat().st_size, 0)
        self.assertEqual((run / "summary.sha256").read_text(), compare.listing(run))

    def test_second_run_same_day_gets_its_own_dir(self):
        """Two baseline runs of one sha on one day must not share a run dir."""
        # GIVEN one task run twice
        m = self.manifest({"t1": 2})
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # WHEN run again
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN there are two sealed runs, the second suffixed -2
        names = sorted(d.name for d in (self.home / "runs").iterdir() if d.is_dir())
        self.assertEqual(len(names), 2)
        self.assertEqual(names[1], names[0] + "-2")

    def test_docker_is_the_default_env(self):
        """
        Local Docker is free, so it is the default: each try is a fresh container
        Harbor deletes, and Daytona keys never reach a local run.
        """
        # GIVEN a host env with a Daytona key and a Docker host
        m = self.manifest({"t1": 2})
        planted = {"DAYTONA_API_KEY": "d", "DOCKER_HOST": "unix:///tmp/d.sock", "AWS_ACCESS_KEY_ID": "id"}
        # WHEN run_exam runs with no --env
        self.run_exam("--tasks", m, "--budget-usd", 100, extra_env=planted)
        # THEN Harbor runs it in docker and deletes the environment afterwards
        argv = self.argvs()[0]
        self.assertEqual(argv[argv.index("-e") + 1], "docker")
        self.assertIn("--delete", argv)
        # AND Harbor sees the Docker host and the Bedrock key but not the Daytona key
        env = self.envs()[0]
        self.assertIn("DOCKER_HOST", env)
        self.assertIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("DAYTONA_API_KEY", env)
        # AND run.json records the env and the concurrency picked from docker info
        meta = json.loads((self.only_run() / "run.json").read_text())
        self.assertEqual((meta["env"], meta["concurrency"]), ("docker", 1))

    def test_daytona_flag_skips_docker(self):
        """--env daytona never asks the local Docker anything and runs one at a time by default."""
        # GIVEN a machine whose Docker is down
        m = self.manifest({"t1": 2})
        self.docker_spec.write_text(json.dumps({"down": True}))
        # WHEN run_exam runs on Daytona
        self.run_exam("--tasks", m, "--budget-usd", 100, "--env", "daytona")
        # THEN it ran, made no docker call, and used concurrency 1
        self.assertEqual(self.docker_calls(), [])
        argv = self.argvs()[0]
        self.assertEqual(argv[argv.index("-e") + 1], "daytona")
        self.assertEqual(json.loads((self.only_run() / "run.json").read_text())["concurrency"], 1)

    def test_docker_down_fails_before_any_run(self):
        """A stopped Docker must fail fast with what to do, not hang or file error tries."""
        # GIVEN Docker is not running
        m = self.manifest({"t1": 2})
        self.docker_spec.write_text(json.dumps({"down": True}))
        # WHEN run_exam runs with the default env
        p = self.run_exam("--tasks", m, "--budget-usd", 100, check=False)
        # THEN it exits non-zero naming Docker Desktop, and Harbor never ran
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Docker Desktop", p.stderr)
        self.assertFalse(self.log.exists())

    def test_controls_get_no_model_keys(self):
        """Oracle and nop never call a model, so the Bedrock keys stay on the host."""
        # GIVEN Bedrock keys on the host
        m = self.manifest({"t1": 2})
        planted = {"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "s"}
        # WHEN a controls run happens
        self.run_exam("--tasks", m, "--budget-usd", 1, "--agent", "controls", extra_env=planted)
        # THEN neither Harbor run saw them
        for env in self.envs():
            self.assertNotIn("AWS_ACCESS_KEY_ID", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_leftover_containers_of_the_try_are_removed(self):
        """
        If Harbor dies before `compose down`, the try's containers would pile up;
        run_exam removes those of this trial only, never anyone else's.
        """
        # GIVEN Docker holds the trial's main and verifier containers, its network, and an unrelated container
        m = self.manifest({"t1": 2})
        self.docker_spec.write_text(json.dumps({
            "containers": ["c1\tt1__abc__env", "c2\tt1__abc__verifier__tests", "c3\tsomeone-else", "c4\t"],
            "networks": ["n1\tt1__abc__env", "n2\tbridge-of-someone"]}))
        # WHEN run_exam runs the try (the fake Harbor's trial dir is t1__abc)
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN exactly the trial's containers and network were removed
        calls = self.docker_calls()
        self.assertIn(["rm", "-f", "-v", "c1", "c2"], calls)
        self.assertIn(["network", "rm", "n1"], calls)
        removed = [c for c in calls if c[0] == "rm" or c[:2] == ["network", "rm"]]
        self.assertEqual(len(removed), 2)
        # AND the job log says so
        log = next((self.only_run() / "harbor").glob("t1-try1.log")).read_text()
        self.assertIn("removed leftover docker objects: c1 c2 n1", log)

    def test_local_concurrency_fits_docker_memory_and_cpus(self):
        """Each task takes 2 CPUs and 4 GiB; 1 GiB stays for the engine; at least one runs."""
        for cpus, mem, want in [(10, 7837, 1),    # Docker Desktop default on a 16 GiB Mac
                                (10, 10240, 2),   # 10 GiB given to Docker
                                (10, 16384, 3),
                                (4, 65536, 2),    # CPU-bound
                                (1, 1024, 1)]:    # never zero
            self.assertEqual(run_exam.local_concurrency(cpus, mem), want, (cpus, mem))

    def test_resource_overrides_cap_to_the_sandbox(self):
        """A task asking for more than one sandbox has is capped to it, per resource."""
        # GIVEN a task asking 4 CPUs / 8 GiB, its verifier 6 CPUs / 12 GiB, and a 2 CPU / 4 GiB task
        big, small = self.root / "big", self.root / "small"
        big.mkdir()
        small.mkdir()
        (big / "task.toml").write_text("[environment]\ncpus = 4\nmemory_mb = 8192\n"
                                       "[verifier.environment]\ncpus = 6\nmemory_mb = 12288\n")
        (small / "task.toml").write_text("[environment]\ncpus = 2\nmemory_mb = 4096\n")
        # WHEN/THEN Daytona (4 / 8192) caps both resources of the big task
        self.assertEqual(run_exam.resource_overrides(big, 4, 8192),
                         ["--override-cpus", "4", "--override-memory-mb", "8192"])
        # AND a 10-CPU / 6813 MB Docker keeps its 6 CPUs but caps memory
        self.assertEqual(run_exam.resource_overrides(big, 10, 6813),
                         ["--override-cpus", "6", "--override-memory-mb", "6813"])
        # AND the small task is untouched in both
        self.assertEqual(run_exam.resource_overrides(small, 4, 8192), [])
        self.assertEqual(run_exam.resource_overrides(small, 10, 6813), [])

    @unittest.skipUnless(VENV_PY.exists(), "needs harness-loop/.venv with harbor installed")
    def test_compose_project_matches_harbor(self):
        """The cleanup must name projects exactly as Harbor does, or it would miss them."""
        names = ["ibm__sarama_19952bd2_ff06f3a2__LL5VSTR__env", "_x.Y__abc", "a/b c"]
        code = ("import json, sys; from harbor.environments.docker.docker import "
                "_sanitize_docker_compose_project_name as f; print(json.dumps([f(n) for n in sys.argv[1:]]))")
        r = subprocess.run([str(VENV_PY), "-c", code, *names], text=True, capture_output=True, check=True)
        self.assertEqual([run_exam.compose_project(n) for n in names], json.loads(r.stdout))

    def test_branch_of_names_a_branch_ref_only(self):
        """A harm run records its branch name; a sha or HEAD records none."""
        # GIVEN a repo with a branch
        repo, sha = make_repo(self.root / "r", {"a": "1"})
        subprocess.run(["git", "-C", str(repo), "branch", "exam-control/planted-harm"], check=True)
        # WHEN/THEN branch_of names the branch only for a branch ref
        self.assertEqual(run_exam.branch_of(repo, "exam-control/planted-harm"), "exam-control/planted-harm")
        self.assertIsNone(run_exam.branch_of(repo, sha))
        self.assertIsNone(run_exam.branch_of(repo, "HEAD"))


if __name__ == "__main__":
    unittest.main()
