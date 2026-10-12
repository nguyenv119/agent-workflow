"""Tests for harness-loop/tasks/manifest.py (load + validate a task manifest)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent / "tasks"
sys.path.insert(0, str(TASKS))
import manifest  # noqa: E402


def row(**over):
    """One valid task row; keyword args override fields."""
    base = {"id": "t1", "harbor_path": "cache/set/t1", "repo": None, "pr": None,
            "merged_at": None, "language": "python", "kind": "bugfix", "workdir": "/app",
            "test_command": "pytest -q", "title": "Fix t1", "screen": False}
    base.update(over)
    return base


class ManifestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, tasks, **top):
        """Write a manifest with these task rows (top-level fields overridable) and return its path."""
        doc = {"set": "demo", "source": "x", "tasks": tasks}
        doc.update(top)
        p = self.dir / "m.json"
        p.write_text(json.dumps(doc))
        return p

    def cache_hidden(self, *rels, task="t1", test_sh=None):
        """Create a cached task folder whose hidden tests/ holds the given relative files."""
        tests = self.dir / "cache/set" / task / "tests"
        tests.mkdir(parents=True, exist_ok=True)
        for rel in rels:
            (tests / rel).parent.mkdir(parents=True, exist_ok=True)
            (tests / rel).write_text("")
        if test_sh is not None:
            (tests / "test.sh").write_text(test_sh)
        return tests.parent

    def cli(self, *args):
        return subprocess.run([sys.executable, str(TASKS / "manifest.py"), *args],
                              capture_output=True, text=True)

    def test_valid_manifest_summarises_languages_and_kinds(self):
        """
        The summary line counts tasks per language and per kind. The exam
        plan needs a mix (Python, TypeScript, bugfix, feature) and this line
        is how a human checks the mix at a glance; a wrong count would hide a
        set that is all one language.
        """
        # GIVEN two valid tasks of different language and kind
        p = self.write([row(), row(id="t2", language="typescript", kind="feature",
                                   test_command=None)])
        # WHEN loaded and summarised
        data = manifest.load(p)
        # THEN the summary counts both, and neither was leak-checked (no cache)
        self.assertEqual(manifest.summary(data),
                         "demo: 2 tasks valid (1 python, 1 typescript; 1 bugfix, 1 feature); "
                         "leak-checked 0/2")

    def test_missing_field_names_the_task(self):
        """
        A row missing a field fails and names the task id. Downstream code
        (B18.2, B13) reads every field; a missing one would crash mid-exam
        with a KeyError far from the bad row.
        """
        # GIVEN a row with no workdir
        bad = row(id="t9")
        del bad["workdir"]
        # WHEN loaded THEN the error names t9 and workdir
        with self.assertRaisesRegex(ValueError, "t9.*workdir"):
            manifest.load(self.write([bad]))

    def test_missing_top_level_field_raises_value_error(self):
        """
        A manifest without `tasks` raises ValueError, not KeyError. Callers
        catch ValueError for "bad manifest"; a KeyError would escape that
        handler and look like a code bug.
        """
        # GIVEN a manifest file with no tasks key
        p = self.dir / "m.json"
        p.write_text(json.dumps({"set": "demo", "source": "x"}))
        # WHEN loaded THEN ValueError names the field
        with self.assertRaisesRegex(ValueError, "tasks"):
            manifest.load(p)

    def test_tasks_not_a_list_raises_value_error(self):
        """
        `tasks` must be a list. A dict there would iterate over its keys and
        fail with a confusing AttributeError instead of a clear message.
        """
        # GIVEN tasks given as an object
        p = self.write({"t1": row()})
        # WHEN loaded THEN ValueError mentions tasks
        with self.assertRaisesRegex(ValueError, "tasks"):
            manifest.load(p)

    def test_duplicate_id_rejected(self):
        """
        Two rows with one id are rejected. Results are keyed by task id; a
        duplicate would make one task's score overwrite the other's.
        """
        # GIVEN two rows both named t1
        p = self.write([row(), row()])
        # WHEN loaded THEN the duplicate is named
        with self.assertRaisesRegex(ValueError, "duplicate id t1"):
            manifest.load(p)

    def test_bad_kind_rejected(self):
        """
        kind must be bugfix or feature. The exam reports scores per kind; any
        other label would drop the task out of both buckets silently.
        """
        # GIVEN a row of kind chore
        p = self.write([row(kind="chore")])
        # WHEN loaded THEN it is rejected naming the task
        with self.assertRaisesRegex(ValueError, "t1.*kind"):
            manifest.load(p)

    def test_screen_must_be_bool(self):
        """
        screen must be a real bool. The string "false" is truthy, so a task
        typed that way would be treated as a screen task.
        """
        # GIVEN screen given as a string
        p = self.write([row(screen="false")])
        # WHEN loaded THEN it is rejected
        with self.assertRaisesRegex(ValueError, "t1.*screen"):
            manifest.load(p)

    def test_test_command_must_be_string_or_null(self):
        """
        test_command is a shell string or null. A list would be written into
        the generated CLAUDE.md as a Python repr, which no agent can run.
        """
        # GIVEN test_command given as a list
        p = self.write([row(test_command=["pytest", "-q"])])
        # WHEN loaded THEN it is rejected
        with self.assertRaisesRegex(ValueError, "t1.*test_command"):
            manifest.load(p)

    def test_pr_must_be_int_or_null(self):
        """
        pr is an integer or null. A string like "#12" breaks the link the
        bead builds to the source PR, and True would pass a plain int check.
        """
        # GIVEN pr given as a bool
        p = self.write([row(pr=True)])
        # WHEN loaded THEN it is rejected
        with self.assertRaisesRegex(ValueError, "t1.*pr"):
            manifest.load(p)

    def test_test_command_on_hidden_tests_mount_rejected(self):
        """
        A visible command that runs the container-root /tests/ mount is
        rejected: that mount holds the hidden verifier, and letting the agent
        run it turns the exam into fitting the grader.
        """
        # GIVEN a command that runs /tests/test_outputs.py
        p = self.write([row(test_command="pytest /tests/test_outputs.py")])
        # WHEN loaded THEN it is rejected as hidden
        with self.assertRaisesRegex(ValueError, "t1.*hidden"):
            manifest.load(p)

    def test_repo_subfolder_named_tests_accepted(self):
        """
        A repo's own tests folder (pkg/tests/, ./tests/) is not the hidden
        mount. Most Python repos keep visible tests there; rejecting them
        would leave those tasks with no visible test command at all.
        """
        # GIVEN commands that use a repo-relative tests folder
        p = self.write([row(test_command="pytest -q pkg/tests/test_x.py"),
                        row(id="t2", test_command="./tests/run.sh")])
        # WHEN loaded
        data = manifest.load(p)
        # THEN both rows are kept
        self.assertEqual([t["id"] for t in data["tasks"]], ["t1", "t2"])

    def test_test_command_naming_a_cached_hidden_test_file_rejected(self):
        """
        A visible command that names a hidden test file is rejected. If the
        agent can run the hidden test it can fit the grader instead of
        solving the task.
        """
        # GIVEN a cached task whose hidden tests include test_state.py
        self.cache_hidden("test_state.py")
        p = self.write([row(test_command="pytest -q tests/test_state.py")])
        # WHEN loaded THEN the hidden file is named in the error
        with self.assertRaisesRegex(ValueError, "t1.*test_state.py"):
            manifest.load(p)

    def test_test_command_naming_another_file_accepted(self):
        """
        A command naming a different file passes even when the hidden tests
        are cached. Without this, every task with a cache would be rejected.
        """
        # GIVEN cached hidden tests holding test_state.py
        self.cache_hidden("test_state.py")
        p = self.write([row(test_command="pytest -q tests/test_other.py")])
        # WHEN loaded
        data = manifest.load(p)
        # THEN the row is kept and counted as leak-checked
        self.assertEqual(manifest.summary(data),
                         "demo: 1 tasks valid (1 python; 1 bugfix); leak-checked 1/1")

    def test_hidden_name_as_substring_of_a_visible_file_accepted(self):
        """
        Matching is by whole path, not substring: hidden utils.py must not
        reject tests/test_utils.py, and the harness file test.sh must not
        reject ./run_test.sh. Substring matching threw out good commands.
        """
        # GIVEN hidden tests holding utils.py, test.sh and Dockerfile
        self.cache_hidden("utils.py", "Dockerfile", test_sh="#!/bin/sh\n")
        p = self.write([row(test_command="pytest tests/test_utils.py && ./run_test.sh")])
        # WHEN loaded
        data = manifest.load(p)
        # THEN the row is kept
        self.assertEqual(len(data["tasks"]), 1)

    def test_nested_hidden_path_matched_relative_to_tests_dir(self):
        """
        A hidden file in a subfolder (tests/baseline/types.py) is matched by
        its path under tests/, so a command naming baseline/types.py is
        rejected while app/types.py is not.
        """
        # GIVEN hidden tests holding baseline/types.py
        self.cache_hidden("baseline/types.py")
        # WHEN a command names app/types.py THEN it loads
        manifest.load(self.write([row(test_command="python -m mypy app/types.py")]))
        # AND WHEN it names baseline/types.py THEN it is rejected
        with self.assertRaisesRegex(ValueError, "baseline/types.py"):
            manifest.load(self.write([row(test_command="python baseline/types.py")]))

    def test_file_written_by_test_sh_heredoc_is_hidden(self):
        """
        Monthly-SWEBench verifiers write their test files from heredocs inside
        test.sh, so tests/ holds only test.sh. Those written paths are hidden
        too; a visible command naming one would run the grader's test.
        """
        # GIVEN a test.sh that writes $platform/src/a.task.test.ts
        self.cache_hidden(test_sh="cd /app\ncat >\"$platform/src/a.task.test.ts\" <<'EOF'\nx\nEOF\n")
        p = self.write([row(test_command="npx vitest run src/a.task.test.ts")])
        # WHEN loaded THEN it is rejected naming that path
        with self.assertRaisesRegex(ValueError, "src/a.task.test.ts"):
            manifest.load(p)

    def test_task_dir_is_absolute_and_relative_to_manifest_folder(self):
        """
        load() adds each row's absolute task_dir, resolved against the
        manifest's own folder. B18.2 and B13 run harbor on that folder; if it
        were resolved against the caller's cwd they would load the wrong task.
        """
        # GIVEN a manifest in a temp dir
        p = self.write([row()])
        # WHEN loaded
        data = manifest.load(p)
        # THEN task_dir is the manifest folder + harbor_path
        self.assertEqual(data["tasks"][0]["task_dir"], str(self.dir.resolve() / "cache/set/t1"))

    def test_cli_prints_summary_with_leak_count(self):
        """
        The CLI prints the summary plus how many tasks were leak-checked, so
        a run without the cache is visible instead of silently passing.
        """
        # GIVEN a manifest with no cache
        p = self.write([row()])
        # WHEN the CLI runs
        out = self.cli(str(p))
        # THEN it prints the summary with leak-checked 0/1
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(),
                         "demo: 1 tasks valid (1 python; 1 bugfix); leak-checked 0/1")

    def test_cli_require_cache_fails_when_a_task_folder_is_missing(self):
        """
        --require-cache fails when a task folder is absent. The real
        acceptance must prove every task was leak-checked; without the flag a
        missing download looks like a clean pass.
        """
        # GIVEN t1 cached and t2 not
        self.cache_hidden("test_x.py")
        p = self.write([row(), row(id="t2", harbor_path="cache/set/t2")])
        # WHEN the CLI runs with --require-cache
        out = self.cli("--require-cache", str(p))
        # THEN it exits non-zero naming t2
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("t2", out.stderr)

    def test_committed_tb4_manifest_is_valid(self):
        """
        The committed Terminal-Bench manifest loads and has 5-8 tasks, as the
        bead asks. Guards against a hand edit breaking the file.
        """
        # GIVEN the committed manifest WHEN loaded
        data = manifest.load(TASKS / "tb4-software.json")
        # THEN it has 5-8 tasks
        self.assertTrue(5 <= len(data["tasks"]) <= 8)

    def test_committed_shakedown_manifest_is_valid(self):
        """
        The committed Monthly-SWEBench shakedown manifest loads and has about
        10 tasks with both kinds, as the bead asks.
        """
        # GIVEN the committed manifest WHEN loaded
        data = manifest.load(TASKS / "shakedown-2026-05.json")
        # THEN it has 8-12 tasks covering both kinds
        self.assertTrue(8 <= len(data["tasks"]) <= 12)
        self.assertEqual({t["kind"] for t in data["tasks"]}, {"bugfix", "feature"})


if __name__ == "__main__":
    unittest.main()
