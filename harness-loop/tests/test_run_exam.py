"""Tests for harness-loop/exam/run_exam.py with a fake `harbor run` (EXAM_HARBOR env override).

The fake writes the trial dir a real Harbor + WorkflowAgent run leaves behind
(result.json, verifier/reward.txt, agent/exam/*, agent/tries/), driven by a
JSON spec per task, and logs its argv so the flags run_exam passes are visible.
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
sys.path.insert(0, str(EXAM))
import compare  # noqa: E402
import run_exam  # noqa: E402

FAKE_HARBOR = textwrap.dedent('''\
    import json, os, sys
    from pathlib import Path
    argv = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps(argv) + "\\n")
    def opt(*names):
        for i, a in enumerate(argv):
            if a in names:
                return argv[i + 1]
    ak = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--ak")
    task = Path(opt("-p")).name
    agent = opt("-a")
    spec = json.loads(Path(os.environ["FAKE_SPEC"]).read_text()).get(task, {})
    reward = {"oracle": 1, "nop": 0}.get(agent, spec.get("reward", 1))
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
        (exam / name).write_text(text + "\\n")
    subtype = spec.get("subtype", "success")
    (exam / "result.json").write_text(json.dumps({
        "harness_commit": ak["harness_commit"], "branch": branch or None,
        "origin_url": "file:///tmp/origin.git",
        "status": {"success": "ok", "error_max_budget_usd": "budget_cut"}.get(subtype, "error"),
        "total_cost_usd": cost, "exam_no_pr_line": True}))
    inner = agent_dir / "tries/app/exam-1/try-1"
    inner.mkdir(parents=True)
    (inner / "note.md").write_text("inner try\\n")
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

    def run_exam(self, *args, check=True):
        env = {**os.environ, "HARNESS_LOOP_HOME": str(self.home), "FAKE_LOG": str(self.log),
               "FAKE_SPEC": str(self.spec), "EXAM_HARBOR": f"{sys.executable} {self.fake} run"}
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
        return [json.loads(l) for l in self.log.read_text().splitlines()]

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
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN the try is an error and the task has no reward in run.json
        run = self.only_run()
        self.assertEqual(self.results(run)["t1"]["status"], "error")
        self.assertEqual(json.loads((run / "run.json").read_text())["rewards"], {})

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
        # WHEN run_exam runs with the default per-run cap
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN only the big task gets the CPU/memory override
        by_task = {Path(a[a.index("-p") + 1]).name: a for a in self.argvs()}
        big, small = by_task["big"], by_task["small"]
        self.assertEqual(big[big.index("--override-cpus") + 1], "4")
        self.assertEqual(big[big.index("--override-memory-mb") + 1], "8192")
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
