"""Tests for harness-loop/exam/run_exam.py with a fake `harbor run` (EXAM_HARBOR env override).

The fake writes the trial dir a real Harbor + WorkflowAgent run leaves behind
(result.json, verifier/reward.txt, agent/exam/*, agent/tries/), driven by a
JSON spec per task, and logs its argv and env keys so what run_exam passes is
visible. Its log and spec paths come first on its own command line, because
run_exam passes Harbor only an allowlisted env. A fake `docker` (EXAM_DOCKER)
answers `docker info` like a 10-CPU / 7.65 GiB Docker Desktop, lists the
containers in its own spec file, and logs every call.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from helpers import make_repo

EXAM = Path(__file__).resolve().parents[1] / "exam"
SCRIPT = EXAM / "run_exam.py"
VENV_PY = EXAM.parent / ".venv/bin/python"
sys.path.insert(0, str(EXAM))
import compare  # noqa: E402
import run_exam  # noqa: E402

FAKE_HARBOR = textwrap.dedent('''\
    import hashlib, json, os, sys
    from pathlib import Path
    log, spec_path, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")  # logged as a hash: the value never lands in a file
    with open(log, "a") as fh:
        fh.write(json.dumps({"argv": argv, "env": sorted(os.environ),
                             "token_sha256": hashlib.sha256(token.encode()).hexdigest() if token else None}) + "\\n")
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
    (trial / "config.json").write_text("{}")  # Harbor writes it when the trial starts
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
    installed = {}
    if "harness_installed" in ak:  # what _pack_harness records for an installed tree
        sys.path.insert(0, LOOP_DIR)
        import install_harness
        installed = {"installed_sha256": spec.get("installed_sha256",
                                                  install_harness.installed_sha256(ak["harness_installed"]))}
        version = spec.get("harness_version", install_harness.installed_version(ak["harness_installed"]))
        ak["harness_commit"] = (version or {}).get("commit")
    elif "harness_version" in spec:
        version = spec["harness_version"]
    else:  # what WorkflowAgent._pack_harness records: a real install of the same kwargs
        import subprocess, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [sys.executable, INSTALLER, "--repo", AW_ROOT, "--commit", ak["harness_commit"], "--target", tmp]
            if "harness_addon" in ak:
                cmd += ["--addon", ak["harness_addon"]]
            subprocess.run(cmd, check=True, capture_output=True)
            version = json.loads((Path(tmp) / ".claude/HARNESS_VERSION").read_text())
    status = {"success": "ok", "error_max_budget_usd": "budget_cut"}.get(subtype, "error")
    (exam / "result.json").write_text(json.dumps({
        "harness_commit": spec.get("harness_commit", ak["harness_commit"]), "branch": branch or None,
        "origin_url": spec.get("origin_url", "file:///tmp/origin.git"), "rate_limited": spec.get("rate_limited"),
        "status": "rate_limited" if spec.get("rate_limited") else status,
        "total_cost_usd": cost, "exam_no_pr_line": True, "harness_version": version, **installed}))
    inner = agent_dir / "tries/app/exam-1/try-1"
    inner.mkdir(parents=True)
    (inner / "note.md").write_text("inner try\\n")
    for rel, text in spec.get("inner_files", {}).items():  # extra files an agent left in its own try folders
        (inner / rel).parent.mkdir(parents=True, exist_ok=True)
        (inner / rel).write_text(text)
''')

FAKE_DOCKER = textwrap.dedent('''\
    import json, sys
    log, spec_path, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
    spec = json.load(open(spec_path))
    with open(log, "a") as fh:
        fh.write(json.dumps(argv) + "\\n")
    if spec.get("down"):
        sys.exit("Cannot connect to the Docker daemon")
    if spec.get("ps_fails") and argv[0] != "info":
        sys.exit("error listing")
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
        self.fake.write_text(FAKE_HARBOR.replace("INSTALLER, ", f"{str(run_exam.INSTALLER)!r}, ", 1)
                             .replace("AW_ROOT, ", f"{str(run_exam.AW_ROOT)!r}, ", 1)
                             .replace("LOOP_DIR)", f"{str(run_exam.LOOP_DIR)!r})", 1))
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

    def run_exam(self, *args, check=True, extra_env=None, harness=("--harness", "HEAD")):
        env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_MODEL_ACCESS", "CLAUDE_CODE_OAUTH_TOKEN")}
        env = {**env, "HARNESS_LOOP_HOME": str(self.home), "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
               "EXAM_HARBOR": f"{sys.executable} {self.fake} {self.log} {self.spec} run",
               "EXAM_DOCKER": f"{sys.executable} {self.fake_docker} {self.docker_log} {self.docker_spec}",
               **(extra_env or {})}
        p = subprocess.run([sys.executable, str(SCRIPT), *map(str, harness), *map(str, args)],
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

    def token_hashes(self):
        """sha256 of the CLAUDE_CODE_OAUTH_TOKEN each harbor call got (None when it had none)."""
        return [json.loads(l)["token_sha256"] for l in self.log.read_text().splitlines()]

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

    def planted(self):
        """Fake credentials built at run time: (db password, aws key id)."""
        return "hunter" + "2" * 6 + "xyz", "AKIA" + "ABCDEFGH12345678"

    def test_inner_tries_and_result_json_are_redacted_and_not_group_readable(self):
        """
        The try folder's inner/ is a copy of the agent's own tries and result.json
        is written after save_try's redact pass, so neither was cleaned. A secret
        an agent saw would sit in them for the year the tries are kept.
        """
        # GIVEN an agent that left a fake password and key in nested inner files, and an origin url holding a password
        pw, akia = self.planted()
        url = f"postgres://app_user:{pw}@db.example.com:5432/main"
        m = self.manifest({"t1": 2})
        self.spec.write_text(json.dumps({"t1": {"origin_url": url, "inner_files": {
            "gates.txt": f"FAIL connecting to {url}\n", "deep/er/note.md": f"key {akia}\n"}}}))
        # WHEN run_exam runs it
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN no file under the try folder holds either secret, and the clean text is kept
        t1 = self.only_run() / "t1/try-1"
        files = [f for f in t1.rglob("*") if f.is_file()]
        self.assertIn(t1 / "inner/app/exam-1/try-1/deep/er/note.md", files)
        for f in files:
            data = f.read_text(errors="replace")
            for secret in (pw, akia):
                self.assertNotIn(secret, data, str(f))
        self.assertIn("REDACTED", (t1 / "inner/app/exam-1/try-1/gates.txt").read_text())
        self.assertEqual(json.loads((t1 / "result.json").read_text())["task"], "t1")
        # AND nothing in inner or result.json is readable by group or others (the run is sealed read-only after)
        for f in [t1 / "result.json", *(t1 / "inner").rglob("*")]:
            self.assertEqual(f.stat().st_mode & 0o077, 0, str(f))

    def test_redact_inner_and_write_result_leave_mode_600(self):
        """The helpers run_try uses: every file is cleaned in place and left 0600, and the count is returned."""
        # GIVEN an inner tree with a secret in a nested file and a result dict with one in a field
        pw, akia = self.planted()
        inner = self.root / "inner"
        (inner / "a/b").mkdir(parents=True)
        (inner / "a/b/f.txt").write_text(f"id {akia}\n")
        (inner / "a/b/f.txt").chmod(0o644)
        folder = self.root / "folder"
        folder.mkdir()
        # WHEN both helpers run
        n = run_exam.redact_inner(inner)
        run_exam.write_result(folder, {"origin_url": f"postgres://u:{pw}@h:5432/d", "task": "t"})
        # THEN the secrets are gone, the files are exactly 0600 and the dirs 0700
        self.assertEqual(n, 1)
        self.assertNotIn(akia, (inner / "a/b/f.txt").read_text())
        self.assertNotIn(pw, (folder / "result.json").read_text())
        self.assertEqual(json.loads((folder / "result.json").read_text())["task"], "t")
        for f in (inner / "a/b/f.txt", folder / "result.json"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600, str(f))
        self.assertEqual((inner / "a/b").stat().st_mode & 0o777, 0o700)

    def test_failed_redaction_leaves_no_unredacted_copy(self):
        """
        Fail closed, as save_try does: if the redactor cannot clean a file, the
        file must not stay behind with its secret, and the error must surface so
        the try is filed as an error instead of looking clean.
        """
        # GIVEN an inner tree and a result dict, and a redactor that always raises OSError
        pw, akia = self.planted()
        inner = self.root / "inner"
        inner.mkdir()
        (inner / "f.txt").write_text(f"id {akia}\n")
        folder = self.root / "folder"
        folder.mkdir()
        with unittest.mock.patch.object(run_exam, "redact_file", side_effect=OSError("disk full")):
            # WHEN each helper runs THEN it raises
            with self.assertRaises(OSError):
                run_exam.redact_inner(inner)
            with self.assertRaises(OSError):
                run_exam.write_result(folder, {"origin_url": f"postgres://u:{pw}@h:5432/d"})
        # AND the unredacted inner tree and result.json are gone
        self.assertFalse(inner.exists())
        self.assertFalse((folder / "result.json").exists())

    def test_crashed_try_result_is_redacted(self):
        """A crash message can quote a credential from the trial dir; the error try's result.json must not keep it."""
        # GIVEN a run context and an exception whose text holds a fake key
        _, akia = self.planted()
        ctx = {"run": self.root / "run", "controls": False, "sha": "a" * 40, "harness_version": None,
               "args": argparse.Namespace(max_budget_usd=12.05)}
        # WHEN the crashed try is filed
        res = run_exam.crashed_try(ctx, ({"id": "t1"}, 1, "workflow"), RuntimeError(f"boom {akia}"))
        # THEN the file on disk has no key, is not group readable, and the returned dict is unchanged
        f = ctx["run"] / "t1/try-1/result.json"
        self.assertNotIn(akia, f.read_text())
        self.assertEqual(f.stat().st_mode & 0o077, 0)
        self.assertEqual(res["status"], "error")

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
                   "AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "s", "DAYTONA_API_KEY": "d",
                   "HARNESS_MODEL_ACCESS": "bedrock"}
        # WHEN run_exam runs on Daytona on the bedrock route
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

    def test_try_that_installed_another_harness_version_is_an_error(self):
        """
        Each try installs the harness itself; if its installed HARNESS_VERSION
        (e.g. a different add-on hash) differs from the preflight's, the try
        did not sit the exam this run claims and must not be scored.
        """
        # GIVEN t1's agent recorded another add-on hash, t2's matches, t3 recorded none
        m = self.manifest({"t1": 2, "t2": 2, "t3": 2})
        other = {"commit": self.sha, "addon": "/elsewhere", "addon_sha256": "0" * 64}
        self.spec.write_text(json.dumps({"t1": {"harness_version": other}, "t3": {"harness_version": None}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN t1 and t3 are errors carrying their own versions, t2 is ok
        run = self.only_run()
        res = self.results(run)
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual([res[t]["status"] for t in ("t1", "t2", "t3")], ["error", "ok", "error"])
        self.assertEqual(res["t1"]["harness_version"], other)
        self.assertIsNone(res["t3"]["harness_version"])
        self.assertEqual(res["t2"]["harness_version"], meta["harness_version"])

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
        # AND the crashed try still names the harness version the run installed
        self.assertEqual(res["t1"]["harness_version"], json.loads((run / "run.json").read_text())["harness_version"])
        self.assertIsNotNone(res["t1"]["harness_version"])
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

    def screen_baseline(self):
        """A sealed main screen run of t1..t3, all solved, t2 cheapest ($3), then t3 ($6), then t1 ($9)."""
        m = self.manifest({"t1": 2, "t2": 2, "t3": 2}, screen=("t1", "t2", "t3"))
        self.spec.write_text(json.dumps({"t1": {"cost": 9.0}, "t2": {"cost": 3.0}, "t3": {"cost": 6.0}}))
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen", "--kind", "baseline")
        return m, self.only_run()

    def test_early_stop_runs_cheapest_task_first_and_starts_nothing_after_a_decided_fail(self):
        """
        With --early-stop-vs the candidate runs main's cheapest task first (the dry run) and, once the
        screen can no longer pass, starts nothing more and marks the run incomplete with the reason.
        This is what keeps a broken candidate from paying for the whole screen.
        """
        # GIVEN main's sealed screen run, and a candidate that errors on t2 (main's cheapest task)
        m, base = self.screen_baseline()
        self.log.unlink()
        self.spec.write_text(json.dumps({"t2": {"subtype": "error"}}))
        # WHEN the candidate screen runs with --early-stop-vs main's run (exit 1: its only try errored)
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen", "--early-stop-vs", base, check=False)
        # THEN only t2 ran, and the run says why it stopped
        self.assertEqual([Path(a[a.index("-p") + 1]).name for a in self.argvs()], ["t2"])
        run = next(d for d in (self.home / "runs").iterdir() if d != base)
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual(meta["status"], "incomplete")
        self.assertIn("error", meta["early_stop"])

    def test_early_stop_lets_a_passing_candidate_finish_every_task(self):
        """A candidate that can still pass is never stopped: it runs every task, cheapest first, complete."""
        # GIVEN main's sealed screen run and a candidate that solves everything at main's costs
        m, base = self.screen_baseline()
        self.log.unlink()
        # WHEN the candidate screen runs with --early-stop-vs
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen", "--early-stop-vs", base)
        # THEN all three ran in main's cost order and the run is complete with no early stop
        self.assertEqual([Path(a[a.index("-p") + 1]).name for a in self.argvs()], ["t2", "t3", "t1"])
        run = next(d for d in (self.home / "runs").iterdir() if d != base)
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual((meta["status"], meta.get("early_stop")), ("complete", None))

    def test_early_stop_slack_0_stops_once_the_candidate_cannot_match_mains_solved(self):
        """
        --early-stop-slack reaches the early stop: with 0, a candidate that misses main's cheapest task can
        reach at most 2 of main's 3 solved, so nothing more starts (slack 1 would run on: 2 >= 3 - 1).
        Breaks: the canary (slack 0) pays for tasks of a candidate its screen already fails.
        """
        # GIVEN main's sealed screen run (3/3), and a candidate that misses t2 (main's cheapest task)
        m, base = self.screen_baseline()
        self.log.unlink()
        self.spec.write_text(json.dumps({"t2": {"reward": 0}}))
        # WHEN the candidate screen runs with --early-stop-vs main's run and --early-stop-slack 0
        self.run_exam("--tasks", m, "--budget-usd", 100, "--screen", "--early-stop-vs", base,
                      "--early-stop-slack", 0, check=False)
        # THEN only t2 ran, and the run stopped on the solved rule
        self.assertEqual([Path(a[a.index("-p") + 1]).name for a in self.argvs()], ["t2"])
        run = next(d for d in (self.home / "runs").iterdir() if d != base)
        self.assertIn("solved", json.loads((run / "run.json").read_text())["early_stop"])

    def test_early_stop_without_screen_is_refused(self):
        """Early stop mirrors the screen rule only; on a full exam it would cut a run the margin needs."""
        # GIVEN main's screen run, WHEN a non-screen run asks for early stop
        m, base = self.screen_baseline()
        p = self.run_exam("--tasks", m, "--budget-usd", 100, "--early-stop-vs", base, check=False)
        # THEN it exits before any run
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--early-stop-vs", p.stderr)
        self.assertEqual(len(list((self.home / "runs").iterdir())), 1)

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
        planted = {"DAYTONA_API_KEY": "d", "DOCKER_HOST": "unix:///tmp/d.sock", "AWS_ACCESS_KEY_ID": "id",
                   "AWS_SECRET_ACCESS_KEY": "s", "HARNESS_MODEL_ACCESS": "bedrock"}
        # WHEN run_exam runs with no --env (bedrock route)
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

    def test_subscription_is_the_default_route_and_harbor_gets_only_the_token(self):
        """
        With no route set the workflow runs go on the subscription: Harbor sees the OAuth token and no AWS key,
        the agent is told model_access=subscription with a first-party model id, and run.json records the route.
        """
        # GIVEN Bedrock keys on the host too, and no HARNESS_MODEL_ACCESS
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100,
                      extra_env={"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "s", "CLAUDE_CODE_USE_BEDROCK": "1"})
        # THEN Harbor got the token only, and the subscription kwargs
        env, argv = self.envs()[0], self.argvs()[0]
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertFalse({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_USE_BEDROCK"} & set(env))
        ak = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--ak")
        self.assertEqual((ak["model_access"], ak["model"]), ("subscription", "claude-opus-5-5"))
        meta = json.loads((self.only_run() / "run.json").read_text())
        self.assertEqual((meta["model_access"], meta["model"]), ("subscription", "claude-opus-5-5"))

    def test_api_route_gives_harbor_only_the_api_key(self):
        """
        HARNESS_MODEL_ACCESS=api (the exam's last fallback, set by loop.py): Harbor sees ANTHROPIC_API_KEY and no
        token or AWS key, the agent gets model_access=api with a first-party model id, and run.json records it.
        """
        # GIVEN an API key, Bedrock keys and the token on the host
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs on the api route
        self.run_exam("--tasks", m, "--budget-usd", 100,
                      extra_env={"HARNESS_MODEL_ACCESS": "api", "ANTHROPIC_API_KEY": "k", "AWS_ACCESS_KEY_ID": "id",
                                 "AWS_SECRET_ACCESS_KEY": "s"})
        # THEN Harbor got the key only, and the api kwargs
        env, argv = self.envs()[0], self.argvs()[0]
        self.assertIn("ANTHROPIC_API_KEY", env)
        self.assertFalse({"CLAUDE_CODE_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} & set(env))
        ak = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--ak")
        self.assertEqual((ak["model_access"], ak["model"]), ("api", "claude-opus-5-5"))
        self.assertEqual(json.loads((self.only_run() / "run.json").read_text())["model_access"], "api")

    def test_subscription_without_token_refuses_before_any_try(self):
        """
        The sandbox cannot read the keychain login: with no token every try would error and be booked at the
        cap, so run_exam refuses before Harbor runs, naming `claude setup-token`, and never falls back to Bedrock.
        """
        # GIVEN Bedrock keys but no OAuth token
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, check=False,
                          extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "", "AWS_ACCESS_KEY_ID": "id",
                                     "AWS_SECRET_ACCESS_KEY": "s", "CLAUDE_CODE_USE_BEDROCK": "1"})
        # THEN it exits non-zero naming the fix, and Harbor never ran and no run dir exists
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("claude setup-token", p.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.home / "runs").exists())

    def test_rate_limited_try_stops_the_run_unscored(self):
        """
        A try that hits the usage limit stops the run like a budget stop: nothing more starts, the run is
        incomplete with run.json rate_limited, and the try counts as neither solved nor unsolved.
        """
        # GIVEN three tasks at concurrency 1, the first hitting the session limit
        m = self.manifest({"t1": 2, "t2": 2, "t3": 2})
        self.spec.write_text(json.dumps({"t1": {"rate_limited": "You've hit your session limit", "cost": 0.5}}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100, "--concurrency", 1)
        # THEN only t1 ran; the run is incomplete, says why, and scores nothing
        run = self.only_run()
        self.assertEqual(len(self.argvs()), 1)
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual((meta["status"], meta["rate_limited"]), ("incomplete", "You've hit your session limit"))
        self.assertEqual(meta["rewards"], {})
        summary = json.loads((run / "summary.json").read_text())
        self.assertEqual((summary["counts"], summary["solved"]), ({"rate_limited": 1}, 0))
        self.assertEqual(self.results(run)["t1"]["rate_limited"], "You've hit your session limit")

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
        # GIVEN Docker holds the trial's main and verifier containers and its network, plus a
        # concurrent try of the same task, a task whose name extends this trial's, an unlabelled
        # container and an unrelated network
        m = self.manifest({"t1": 2})
        self.docker_spec.write_text(json.dumps({
            "containers": ["c1\tt1__abc__env", "c2\tt1__abc__verifier__tests", "c3\tt1__xyz__env",
                           "c4\tt1__abcd__env", "c5\t", "c6\tsomeone-else"],
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

    def test_only_trial_dirs_name_projects(self):
        """A non-trial dir in the job dir (Harbor's .sources) must not become a match prefix."""
        # GIVEN a job dir with one trial and a .sources dir, and containers for both names
        job = self.root / "job"
        (job / "t1__abc").mkdir(parents=True)
        (job / "t1__abc/config.json").write_text("{}")
        (job / ".sources").mkdir()
        self.docker_spec.write_text(json.dumps({"containers": ["c1\tt1__abc__env", "c2\t0-sources__env"]}))
        env = {"EXAM_DOCKER": f"{sys.executable} {self.fake_docker} {self.docker_log} {self.docker_spec}"}
        # WHEN the cleanup runs
        with unittest.mock.patch.dict(os.environ, env):
            removed = run_exam.remove_leftovers(job)
        # THEN only the trial's container went
        self.assertEqual(removed, ["c1"])

    def test_failed_cleanup_keeps_the_try(self):
        """If docker cannot list containers after a try, the try is still filed with its reward."""
        # GIVEN docker answers info but fails every listing
        m = self.manifest({"t1": 2})
        self.docker_spec.write_text(json.dumps({"ps_fails": True}))
        # WHEN run_exam runs
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN the try is ok with its reward and nothing was removed
        r = self.results(self.only_run())["t1"]
        self.assertEqual((r["status"], r["reward"]), ("ok", 1))
        self.assertFalse(any(c[0] == "rm" for c in self.docker_calls()))

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

    def addon_dir(self):
        """An add-on with one skill and a private task folder listed in .installignore."""
        addon = self.root / "addon"
        (addon / "skills").mkdir(parents=True)
        (addon / "skills/x.md").write_text("X")
        (addon / "private/t1").mkdir(parents=True)
        (addon / "private/t1/hidden.txt").write_text("hidden")
        (addon / ".installignore").write_text("private\n")
        data = b"X"
        want = hashlib.sha256(b"skills/x.md\0" + str(len(data)).encode() + b"\0" + data).hexdigest()
        return addon, want

    def test_addon_reaches_harbor_and_its_hash_is_recorded(self):
        """
        A private task set runs under the harness plus an add-on. The add-on
        must reach WorkflowAgent, and the installed HARNESS_VERSION (with the
        add-on hash) must be in run.json and every result.json, or a result
        could not say which add-on produced it.
        """
        # GIVEN two tasks and an add-on
        m = self.manifest({"t1": 2, "t2": 2})
        addon, want = self.addon_dir()
        # WHEN run_exam runs with --addon
        self.run_exam("--tasks", m, "--budget-usd", 100, "--addon", addon)
        # THEN every harbor call carries --ak harness_addon=<abs dir>
        for a in self.argvs():
            self.assertIn(f"harness_addon={addon.resolve()}", a)
        # AND run.json and every result.json record the installed version
        run = self.only_run()
        meta = json.loads((run / "run.json").read_text())
        self.assertEqual(meta["harness_version"],
                         {"commit": self.sha, "addon": str(addon.resolve()), "addon_sha256": want})
        self.assertEqual(meta["addon"], str(addon.resolve()))
        for r in self.results(run).values():
            self.assertEqual(r["harness_version"], meta["harness_version"])

    def test_workflow_run_without_addon_records_null_addon_hash(self):
        """Without --addon the version still names the commit, with a null add-on hash."""
        # GIVEN one task
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs without --addon
        self.run_exam("--tasks", m, "--budget-usd", 100)
        # THEN no harness_addon kwarg and the version has null add-on fields
        self.assertFalse(any(x.startswith("harness_addon=") for x in self.argvs()[0]))
        r = self.results(self.only_run())["t1"]
        self.assertEqual(r["harness_version"], {"commit": self.sha, "addon": None, "addon_sha256": None})

    def test_controls_run_with_addon_records_null_harness_version(self):
        """Oracle and nop install no harness, so their version is null; the add-on is still recorded."""
        # GIVEN one task and an add-on
        m = self.manifest({"t1": 2})
        addon, _ = self.addon_dir()
        # WHEN a controls run is given --addon
        self.run_exam("--tasks", m, "--budget-usd", 10, "--agent", "controls", "--addon", addon)
        # THEN run.json and both results have a null harness_version, and no kwarg reached harbor
        run = self.only_run()
        meta = json.loads((run / "run.json").read_text())
        self.assertIsNone(meta["harness_version"])
        self.assertEqual(meta["addon"], str(addon.resolve()))
        for agent in ("oracle", "nop"):
            self.assertIsNone(json.loads((run / agent / "t1/try-1/result.json").read_text())["harness_version"])
        self.assertFalse(any(x.startswith("harness_addon=") for a in self.argvs() for x in a))

    def test_missing_addon_dir_exits_before_any_run(self):
        """A mistyped --addon must fail before spend, not run the exam without it."""
        # GIVEN one task and a path that does not exist
        m = self.manifest({"t1": 2})
        # WHEN run_exam runs / THEN it exits non-zero and Harbor never ran
        p = self.run_exam("--tasks", m, "--budget-usd", 100, "--addon", self.root / "nope", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_addon_run_in_the_default_loop_home_is_refused(self):
        """
        The improver reads ~/.harness-loop/runs/ and writes public PRs, so a
        private (--addon) run must never land there: run_exam refuses before
        any Harbor call.
        """
        # GIVEN HARNESS_LOOP_HOME that is the user's default ~/.harness-loop
        m = self.manifest({"t1": 2})
        addon, _ = self.addon_dir()
        user = self.root / "user"
        user.mkdir()
        (user / ".harness-loop").symlink_to(self.home)
        # WHEN run_exam runs with --addon
        p = self.run_exam("--tasks", m, "--budget-usd", 100, "--addon", addon, check=False,
                          extra_env={"HOME": str(user)})
        # THEN refused naming the rule, Harbor never ran, no run dir
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("HARNESS_LOOP_HOME", p.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.home / "runs").exists() and any((self.home / "runs").iterdir()))

    def test_missing_harbor_is_refused_before_any_run_dir(self):
        """
        A checkout with no harness-loop/.venv has no `harbor`: every try would raise and be
        booked at max_budget_usd. run_exam exits up front naming the fix, with no run dir.
        """
        # GIVEN one task and EXAM_HARBOR pointing at a harbor that does not exist
        m = self.manifest({"t1": 2})
        missing = self.root / "no-venv/bin/harbor"
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, check=False,
                          extra_env={"EXAM_HARBOR": f"{missing} run"})
        # THEN it exits non-zero naming the missing command and both fixes, and no run dir exists
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("Traceback", p.stderr)
        for part in (str(missing), "harness-loop/.venv", "EXAM_HARBOR"):
            self.assertIn(part, p.stderr)
        self.assertFalse((self.home / "runs").exists() and any((self.home / "runs").iterdir()))

    def test_missing_task_cache_is_refused_before_any_run_dir(self):
        """A task folder that was never downloaded stops run_exam with a one-line error, not a traceback."""
        # GIVEN a manifest whose task folder is gone
        m = self.manifest({"t1": 2})
        shutil.rmtree(self.root / "cache/t1")
        # WHEN run_exam runs
        p = self.run_exam("--tasks", m, "--budget-usd", 100, check=False)
        # THEN it exits non-zero naming the task, Harbor never ran, and no run dir exists
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("t1: task folder not downloaded", p.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.home / "runs").exists() and any((self.home / "runs").iterdir()))


class ScheduleEarlyStop(unittest.TestCase):
    """run_exam.schedule with --early-stop-vs, run_try replaced by a stub that returns set results."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def baseline(self, tries):
        """compare.load of a sealed main run. tries: {task: [(status, reward, cost), ...]}."""
        run = self.root / "base"
        for task, rows in tries.items():
            for i, (st, reward, cost) in enumerate(rows, 1):
                d = run / task / f"try-{i}"
                d.mkdir(parents=True)
                (d / "result.json").write_text(json.dumps(
                    {"task": task, "try": i, "status": st, "reward": reward, "cost_usd": cost}))
        (run / "run.json").write_text(json.dumps({"max_budget_usd": 12.05, "status": "complete"}))
        compare.seal(run)
        return compare.load(run)

    def schedule(self, base, outcomes, events, wait=None):
        """Run tasks in outcomes' order at concurrency 2 with early stop; returns schedule()'s result."""
        lock = threading.Lock()

        def run_try(ctx, task, k, agent):
            with lock:
                events.append(("start", task["id"]))
            time.sleep(0.05)
            with lock:
                events.append(("end", task["id"]))
            st, reward, cost = outcomes[task["id"]]
            return {"task": task["id"], "try": k, "agent": agent, "status": st, "reward": reward, "cost_usd": cost}

        args = argparse.Namespace(agent="workflow", concurrency=2, budget_usd=100.0, max_budget_usd=12.05,
                                  early_stop_slack=compare.SCREEN_SOLVED_SLACK)
        self.ctx = {"args": args, "early": base}
        jobs = [({"id": t}, 1, "workflow") for t in outcomes]
        with unittest.mock.patch.object(run_exam, "run_try", run_try), \
                unittest.mock.patch.object(run_exam, "wait", wait or run_exam.wait):
            return run_exam.schedule(self.ctx, jobs)

    def test_two_tries_finishing_in_one_batch_are_both_counted_before_deciding(self):
        """
        Main errored on X and solved Y and Z. The candidate solves X and errors on Y in the same batch, Z queued.
        Errors are even (1 and 1), so the run must go on. Deciding after Y alone, with X finished but not yet
        counted, dropped X from the pairing and wrongly stopped on "error tries 1 > main's 0".
        """
        # GIVEN main's run, and a batch that hands back Y's finished try before X's
        base = self.baseline({"A": [("ok", 1, 10.0)], "X": [("error", None, 0.0)],
                              "Y": [("ok", 1, 10.0)], "Z": [("ok", 1, 10.0)]})

        def batch(fs, return_when):
            got = concurrent.futures.wait(fs, return_when=concurrent.futures.ALL_COMPLETED)
            return sorted(got.done, key=lambda f: f.result()["task"] != "Y"), got.not_done

        # WHEN A runs, then X and Y finish together, then Z is due
        events = []
        complete = self.schedule(base, {"A": ("ok", 1, 1.0), "X": ("ok", 1, 1.0), "Y": ("error", None, 1.0),
                                        "Z": ("ok", 1, 1.0)}, events, wait=batch)
        # THEN nothing stopped early and Z ran
        self.assertIsNone(self.ctx.get("early_stop"))
        self.assertTrue(complete)
        self.assertIn(("start", "Z"), events)

    def test_first_try_runs_alone_before_other_slots_fill(self):
        """With early stop the first (cheapest) try is the paid dry run: nothing else starts until it ends."""
        # GIVEN main solved three tasks, and concurrency 2
        base = self.baseline({t: [("ok", 1, 10.0)] for t in ("A", "B", "C")})
        # WHEN the candidate screen runs
        events = []
        self.schedule(base, {t: ("ok", 1, 1.0) for t in ("A", "B", "C")}, events)
        # THEN A ended before B started, and B and C then ran side by side
        self.assertEqual(events[:2], [("start", "A"), ("end", "A")])
        self.assertEqual(set(events[2:4]), {("start", "B"), ("start", "C")})


if __name__ == "__main__":
    unittest.main()
