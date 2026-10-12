"""Tests for the offline parts of harness-loop/tasks/fresh/build.sh and collect.py.

Nothing here runs SWE-gen or spends money: the cost gate, the missing-swegen exit,
test_command / workdir extraction, and the manifest written from a fake built task.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FRESH = Path(__file__).resolve().parent.parent / "tasks" / "fresh"
sys.path.insert(0, str(FRESH))
import collect  # noqa: E402


def cand(repo="acme/widgets", pr=7, language="python", kind="bugfix", tests=("tests/test_widget.py",)):
    return {"repo": repo, "pr": pr, "url": f"https://github.com/{repo}/pull/{pr}", "title": f"Fix {pr}",
            "merged_at": "2026-09-02T10:00:00Z", "language": language, "kind": kind, "stars": 900,
            "issues": [1], "issue_created_at": "2026-08-30T00:00:00Z", "source_files": 4,
            "test_files": list(tests)}


class Tmp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def candidates(self, rows, name="candidates-2026-10.json"):
        p = self.dir / name
        p.write_text(json.dumps({"window": {}, "candidates": rows}))
        return p


class BuildShGateTest(Tmp):
    def build(self, *args):
        """Run build.sh with a PATH holding python3 and system tools only (no swegen)."""
        bin_dir = self.dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        if not (bin_dir / "python3").exists():
            (bin_dir / "python3").symlink_to(sys.executable)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.dir)}
        return subprocess.run(["bash", str(FRESH / "build.sh"), *args],
                              capture_output=True, text=True, env=env)

    def test_prints_count_and_estimate_and_refuses_without_budget(self):
        """
        Before any spend build.sh must show how many tasks and what they may
        cost, and stop (exit 3) unless --confirm-budget is passed.
        """
        # GIVEN 3 candidates (2 python bugfix, 1 typescript feature)
        p = self.candidates([cand(pr=1), cand(pr=2), cand(pr=3, language="typescript", kind="feature")])
        # WHEN build.sh runs without --confirm-budget
        r = self.build("--candidates", str(p))
        # THEN it prints the split and 3 x $12 = $36, and refuses
        self.assertEqual(r.returncode, collect.PLAN_REFUSED, r.stderr)
        self.assertIn("3 candidates (2 python, 1 typescript; 2 bugfix, 1 feature)", r.stdout)
        self.assertIn("up to $36.00", r.stdout)
        self.assertIn("pass --confirm-budget", r.stdout)

    def test_refuses_budget_below_estimate(self):
        """A confirmed budget smaller than the estimate is still a refusal."""
        # GIVEN 3 candidates at $12 each
        p = self.candidates([cand(pr=i) for i in range(3)])
        # WHEN the confirmed budget is $35
        r = self.build("--candidates", str(p), "--confirm-budget", "35")
        # THEN it refuses
        self.assertEqual(r.returncode, collect.PLAN_REFUSED)
        self.assertIn("below the estimate", r.stdout)

    def test_exits_clearly_when_swegen_missing(self):
        """
        With the budget confirmed but swegen not installed, build.sh stops with
        a message saying so (exit 4), and writes no build output.
        """
        # GIVEN 1 candidate and a sufficient budget, and no swegen on PATH
        p = self.candidates([cand()])
        # WHEN build.sh runs
        r = self.build("--candidates", str(p), "--per-task-usd", "5", "--confirm-budget", "5")
        # THEN it says swegen is missing
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("budget $5.00 confirmed", r.stdout)
        self.assertIn("swegen is not installed", r.stderr)


class TestCommandTest(unittest.TestCase):
    HIDDEN = {"tests/test_widget.py", "web/src/a.test.ts"}

    def cmd(self, sh):
        return collect.test_command(sh, self.HIDDEN)

    def test_pytest_drops_hidden_file_and_pipe(self):
        """
        SWE-gen's test.sh names the PR's test files; the visible command keeps
        the runner and flags but none of those paths, and no `| tee` tail.
        """
        # GIVEN a test.sh that copies hidden tests then runs them
        sh = ('#!/bin/bash\ncp "/tests/tests/test_widget.py" "tests/test_widget.py"\n'
              "/opt/venv/bin/pytest -xvs tests/test_widget.py::test_x -p no:cacheprovider 2>&1 | tee log\n")
        # WHEN the test command is extracted
        # THEN it is the runner and its flags only
        self.assertEqual(self.cmd(sh), "/opt/venv/bin/pytest -xvs -p no:cacheprovider")

    def test_vitest_with_env_prefix_and_hidden_ts_file(self):
        """An env prefix is dropped and a TS spec path removed."""
        # GIVEN a vitest line with CI=1 and a hidden spec path
        sh = "cd /app && CI=1 npx vitest run web/src/a.test.ts --reporter=dot\n"
        # WHEN extracted
        # THEN only runner and flags remain
        self.assertEqual(self.cmd(sh), "npx vitest run --reporter=dot")

    def test_unlisted_test_file_and_variable_are_dropped(self):
        """
        A test file path not in the hidden list, and a $VAR (which SWE-gen
        expands to the copied tests), are still dropped: safer to run all
        visible tests than to name a file the agent may not have.
        """
        # GIVEN pytest with an unknown test file and a variable
        sh = 'python -m pytest -q pkg/other_test.py "$TEST_FILES"\n'
        # WHEN extracted
        # THEN both are gone
        self.assertEqual(self.cmd(sh), "python -m pytest -q")

    def test_test_folder_is_kept(self):
        """A tests/ folder is not a hidden file; pytest tests/ stays as is."""
        # GIVEN pytest on a folder
        # WHEN extracted
        # THEN the folder is kept
        self.assertEqual(self.cmd("pytest -q tests/\n"), "pytest -q tests/")

    def test_no_runner_gives_none(self):
        """A test.sh with no recognisable runner yields null ('no visible tests')."""
        # GIVEN a script with only setup lines
        # WHEN extracted
        # THEN None
        self.assertIsNone(self.cmd("set -e\ncd /app\nmake check\n"))

    def test_extracted_command_passes_manifest_leak_check(self):
        """The command collect.py writes must never trip manifest.py's own leak check."""
        # GIVEN a command extracted from a line naming every hidden file
        out = self.cmd("pytest tests/test_widget.py web/src/a.test.ts -q\n")
        # WHEN manifest.py's tokenizer looks for hidden names
        toks = list(collect.manifest._tokens(out))
        # THEN none match
        self.assertFalse([h for h in self.HIDDEN for t in toks if collect.manifest._names(t, h)])


class WorkdirTest(unittest.TestCase):
    def test_last_workdir_wins(self):
        """Dockerfiles may change WORKDIR; the agent starts in the last one."""
        # GIVEN two WORKDIRs
        # WHEN read
        # THEN the last is returned, and none gives None
        self.assertEqual(collect.workdir("FROM x\nWORKDIR /app\nRUN a\nWORKDIR /app/src\n"), "/app/src")
        self.assertIsNone(collect.workdir("FROM x\n"))


class ManifestTest(Tmp):
    def fake_task(self, tid, workdir="/app/src"):
        """A built Harbor task as SWE-gen lays it out, under cache/fresh-2026-10/<tid>/."""
        t = self.dir / "cache" / "fresh-2026-10" / tid / "acme-widgets-7"
        (t / "environment").mkdir(parents=True)
        (t / "tests").mkdir()
        (t / "task.toml").write_text("")
        (t / "environment" / "Dockerfile").write_text(f"FROM ubuntu:24.04\nWORKDIR {workdir}\n")
        (t / "tests" / "test_widget.py").write_text("")
        (t / "tests" / "test.sh").write_text("pytest -xvs tests/test_widget.py\n")
        return t

    def test_manifest_has_only_passed_tasks_and_validates(self):
        """
        Only tasks SWE-gen validated (listed in passed.txt) enter the manifest,
        with workdir from the Dockerfile and a test_command free of hidden
        paths; the written file loads cleanly with manifest.py.
        """
        # GIVEN two candidates, one built and passed, one failed
        ok, bad = cand(pr=7), cand(pr=8)
        p = self.candidates([ok, bad])
        build = self.dir / "cache" / "fresh-2026-10"
        self.fake_task(collect.task_id("acme/widgets", 7))
        (build / "passed.txt").write_text("acme__widgets-7\n")
        (build / "failed.txt").write_text("acme__widgets-8\n")
        out = self.dir / "fresh-2026-10.json"
        # WHEN collect.py writes the manifest
        r = subprocess.run([sys.executable, str(FRESH / "collect.py"), "manifest", str(p), str(build), str(out)],
                           capture_output=True, text=True)
        # THEN one row, valid, with the expected fields
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = collect.manifest.load(out, require_cache=True)
        (row,) = doc["tasks"]
        self.assertEqual((row["id"], row["workdir"], row["test_command"], row["screen"]),
                         ("acme__widgets-7", "/app/src", "pytest -xvs", False))
        self.assertEqual(row["harbor_path"], "cache/fresh-2026-10/acme__widgets-7/acme-widgets-7")
        self.assertEqual(doc["set"], "fresh-2026-10")
        self.assertIn("leak-checked 1/1", r.stdout)

    def test_missing_workdir_fails_and_writes_nothing(self):
        """A task whose Dockerfile has no WORKDIR is an error, not a row with workdir null."""
        # GIVEN a passed task whose Dockerfile lacks WORKDIR
        p = self.candidates([cand()])
        build = self.dir / "cache" / "fresh-2026-10"
        t = self.fake_task("acme__widgets-7")
        (t / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        (build / "passed.txt").write_text("acme__widgets-7\n")
        out = self.dir / "fresh-2026-10.json"
        # WHEN the manifest is built
        with self.assertRaisesRegex(ValueError, "no WORKDIR"):
            collect.main(["manifest", str(p), str(build), str(out)])
        # THEN no manifest file exists
        self.assertFalse(out.exists())

    def test_list_prints_id_repo_pr(self):
        """build.sh's loop reads 'id repo pr' lines."""
        # GIVEN one candidate
        p = self.candidates([cand(repo="Acme/Widgets", pr=12)])
        # WHEN listed
        r = subprocess.run([sys.executable, str(FRESH / "collect.py"), "list", str(p)],
                           capture_output=True, text=True)
        # THEN the id is lowercase with __ for the slash
        self.assertEqual(r.stdout, "acme__widgets-12 Acme/Widgets 12\n")


if __name__ == "__main__":
    unittest.main()
