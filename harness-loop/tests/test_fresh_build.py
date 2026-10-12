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
        self.assertIn("estimated cost: $36.00 (3 x $12.00 per task, an estimate, not a cap", r.stdout)
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

    def test_exits_clearly_when_daytona_sdk_missing(self):
        """
        With the budget confirmed but the daytona SDK not installed, build.sh
        stops with a message saying so (exit 4), and writes no build output.
        """
        # GIVEN 1 candidate and a sufficient budget, and no daytona module
        p = self.candidates([cand()])
        # WHEN build.sh runs
        r = self.build("--candidates", str(p), "--per-task-usd", "5", "--confirm-budget", "5")
        # THEN it says swegen is missing
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("budget $5.00 confirmed", r.stdout)
        self.assertIn("daytona SDK is missing", r.stderr)


class BuildShLoopTest(Tmp):
    """build.sh past the gate, with a fake daytona module and a python3 wrapper that stands
    in for daytona_build.py: it records the driver's env and cwd and prints a cost line."""

    PARENT_ENV = {
        "GITHUB_TOKEN": "gh-test", "GEMINI_API_KEY_DEV": "gem-test",
        "AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "secret-test", "AWS_REGION": "us-east-1",
        "SWEGEN_EXTRA": "kept", "DAYTONA_API_KEY": "dt-test",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus-id", "ANTHROPIC_SMALL_FAST_MODEL": "haiku-id",
        # planted: none of these may reach the child
        "AWS_ADMIN_X": "admin-test", "ANTHROPIC_AUTH_TOKEN": "auth-test", "ANTHROPIC_API_KEY": "key-test",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test", "GEMINI_API_KEY_PROD": "prod-test",
    }

    def build(self, *args, cost=1.0, drop=()):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        calls = self.dir / "calls"
        calls.mkdir(exist_ok=True)
        (self.dir / "pylib" / "daytona").mkdir(parents=True, exist_ok=True)
        (self.dir / "pylib" / "daytona" / "__init__.py").write_text("")
        wrapper = bin_dir / "python3"
        wrapper.write_text(
            "#!/bin/bash\n"
            'case "$1" in */daytona_build.py)\n'
            f'  n=$(ls "{calls}" | wc -l | tr -d " "); env > "{calls}/$n.env"; pwd > "{calls}/$n.cwd"\n'
            f'  echo "total_cost_usd: {cost}"; exit 1 ;;\n'
            "esac\n"
            f'exec "{sys.executable}" "$@"\n')
        wrapper.chmod(0o755)
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(self.dir),
               "PYTHONPATH": str(self.dir / "pylib"),
               **{k: v for k, v in self.PARENT_ENV.items() if k not in drop}}
        r = subprocess.run(["bash", str(FRESH / "build.sh"), "--tasks-dir", str(self.dir), *args],
                           capture_output=True, text=True, env=env)
        envs = [dict(l.split("=", 1) for l in f.read_text().splitlines() if "=" in l)
                for f in sorted(calls.glob("*.env"))]
        cwds = [f.read_text().strip() for f in sorted(calls.glob("*.cwd"))]
        return r, envs, cwds

    def test_child_gets_only_the_allowlisted_env(self):
        """
        The SWE-gen child runs a Claude session with permission checks off, so
        planted admin keys and other tokens must never reach it.
        """
        # GIVEN a parent env holding the needed vars plus planted secrets
        p = self.candidates([cand()])
        # WHEN build.sh runs one task
        r, envs, cwds = self.build("--candidates", str(p), "--confirm-budget", "12")
        # THEN the child saw only the allowlist
        self.assertEqual(len(envs), 1, r.stdout + r.stderr)
        child = envs[0]
        for planted in ("AWS_ADMIN_X", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                        "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY_PROD", "GEMINI_API_KEY_DEV"):
            self.assertNotIn(planted, child)
        self.assertNotIn("admin-test", "\n".join(child.values()))
        self.assertEqual(child["OPENAI_API_KEY"], "gem-test")
        self.assertEqual(child["GITHUB_TOKEN"], "gh-test")
        self.assertEqual(child["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertEqual(child["SWEGEN_EXTRA"], "kept")
        self.assertEqual(child["DAYTONA_API_KEY"], "dt-test")  # the driver keeps it out of the sandbox
        self.assertEqual(child["ANTHROPIC_DEFAULT_OPUS_MODEL"], "opus-id")
        self.assertEqual(child["ANTHROPIC_SMALL_FAST_MODEL"], "haiku-id")
        self.assertNotIn("ANTHROPIC_MODEL", child)
        self.assertNotIn("AWS_SESSION_TOKEN", child)
        # AND it ran from a scratch folder under the cache, not the caller's cwd
        self.assertIn("/cache/fresh-2026-10/work.", cwds[0])

    def test_stops_once_measured_spend_reaches_budget(self):
        """
        The estimate is not a cap: after each task its measured cost is added,
        and no new task starts once the running total reaches the budget.
        """
        # GIVEN 3 candidates, an estimate of $30 and tasks that really cost $20 each
        p = self.candidates([cand(pr=i) for i in (1, 2, 3)])
        # WHEN build.sh runs with a $30 budget
        r, envs, _ = self.build("--candidates", str(p), "--per-task-usd", "10",
                                "--confirm-budget", "30", cost=20)
        # THEN only 2 tasks start ($20, then $40 >= $30 stops the loop)
        self.assertEqual(len(envs), 2, r.stdout + r.stderr)
        self.assertIn("stopping: spent $40.0 of the $30 budget", r.stdout)
        spend = (self.dir / "cache/fresh-2026-10/spend.tsv").read_text().splitlines()
        self.assertEqual([l.split("\t")[1:] for l in spend], [["20.0", "measured"]] * 2)

    def test_limit_runs_a_small_first_batch(self):
        """--limit N plans and builds only the first N candidates."""
        # GIVEN 3 candidates
        p = self.candidates([cand(pr=i) for i in (1, 2, 3)])
        # WHEN build.sh runs with --limit 1
        r, envs, _ = self.build("--candidates", str(p), "--limit", "1", "--confirm-budget", "12")
        # THEN the plan covers 1 task and only 1 task runs
        self.assertIn("1 candidates", r.stdout)
        self.assertEqual(len(envs), 1, r.stdout + r.stderr)

    def test_no_github_token_means_none_reaches_the_child(self):
        """GITHUB_TOKEN is optional and never filled from the gh login."""
        # GIVEN no GITHUB_TOKEN in the parent env
        p = self.candidates([cand()])
        # WHEN build.sh runs one task
        r, envs, _ = self.build("--candidates", str(p), "--confirm-budget", "12", drop=("GITHUB_TOKEN",))
        # THEN the task runs without any GitHub token
        self.assertEqual(len(envs), 1, r.stdout + r.stderr)
        self.assertNotIn("GITHUB_TOKEN", envs[0])

    def test_missing_credentials_stop_before_any_task(self):
        """The Gemini key and the AWS key + region are required up front."""
        p = self.candidates([cand()])
        for var in ("GEMINI_API_KEY_DEV", "DAYTONA_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_REGION"):
            with self.subTest(var=var):
                # GIVEN the var unset
                # WHEN build.sh runs with a confirmed budget
                r, envs, _ = self.build("--candidates", str(p), "--confirm-budget", "12", drop=(var,))
                # THEN it exits 4 naming the var, and no task ran
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn(var, r.stderr)
                self.assertEqual(envs, [])


class SandboxEnvTest(unittest.TestCase):
    def test_only_model_routing_and_bedrock_gemini_keys_enter_the_sandbox(self):
        """
        The sandboxed SWE-gen gets the Bedrock key, the Gemini key and model ids;
        never the Daytona key, admin keys, logins or anything else.
        """
        import daytona_build
        # GIVEN the driver's env, with secrets that must stay out
        env = {**BuildShLoopTest.PARENT_ENV, "OPENAI_API_KEY": "gem-test", "OPENAI_BASE_URL": "u",
               "SWEGEN_LLM_MODEL": "g", "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/Users/x",
               "PATH": "/x", "AWS_SESSION_TOKEN": ""}
        # WHEN filtered for the sandbox
        got = daytona_build.sandbox_env(env)
        # THEN exactly the allowlist (empty values dropped)
        self.assertEqual(sorted(got), sorted([
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "GITHUB_TOKEN", "SWEGEN_LLM_MODEL", "SWEGEN_EXTRA",
            "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"]))


class CostTest(unittest.TestCase):
    def test_sandbox_time_is_added_to_the_session_cost(self):
        """Daytona time counts against the budget too."""
        log = "swegen_shim: total_cost_usd: 4.0\ndaytona_seconds: 3600\n"
        self.assertEqual(collect.cost(log, 3600, 13.5, 0.34), (4.34, "measured"))

    def test_measured_cost_is_summed_from_the_log(self):
        """Every total_cost_usd reported in the log counts (JSON or key=value form)."""
        log = 'x\n{"type": "result", "total_cost_usd": 3.5}\nResultMessage(total_cost_usd=1.25)\n'
        self.assertEqual(collect.cost(log, 600, 13.5), (4.75, "measured"))

    def test_elapsed_time_when_log_has_no_cost(self):
        """With no reported cost the task is charged elapsed hours x rate."""
        self.assertEqual(collect.cost("no cost here", 1800, 13.5), (6.75, "elapsed"))


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

    def test_hidden_path_without_a_test_name_is_dropped(self):
        """
        A hidden file whose name does not look like a test (a fixture, a check
        script) is dropped only because it is in the hidden list.
        """
        # GIVEN a runner naming two such files
        sh = "pytest src/check_x.py fixtures/data.json -q\n"
        # WHEN they are hidden, and as a control when nothing is hidden
        hidden = collect.test_command(sh, {"src/check_x.py", "fixtures/data.json"})
        control = collect.test_command(sh, set())
        # THEN only the hidden run drops them
        self.assertEqual(hidden, "pytest -q")
        self.assertEqual(control, "pytest src/check_x.py fixtures/data.json -q")

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
