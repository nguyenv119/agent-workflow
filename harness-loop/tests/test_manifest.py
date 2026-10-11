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
    base = {"id": "t1", "harbor_path": "cache/set/t1", "repo": None, "pr": None,
            "merged_at": None, "language": "python", "kind": "bugfix", "workdir": "/app",
            "test_command": "pytest -q", "title": "Fix t1", "screen": False}
    base.update(over)
    return base


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, tasks):
        p = self.dir / "m.json"
        p.write_text(json.dumps({"set": "demo", "source": "x", "tasks": tasks}))
        return p

    def test_valid_manifest_summarises_languages_and_kinds(self):
        # GIVEN two valid tasks of different language and kind
        p = self.write([row(), row(id="t2", language="typescript", kind="feature", test_command=None)])
        # WHEN loaded
        data = manifest.load(p)
        # THEN the summary counts both
        self.assertEqual(manifest.summary(data),
                         "demo: 2 tasks valid (1 python, 1 typescript; 1 bugfix, 1 feature)")

    def test_missing_field_names_the_task(self):
        bad = row(id="t9")
        del bad["workdir"]
        with self.assertRaisesRegex(ValueError, "t9.*workdir"):
            manifest.load(self.write([bad]))

    def test_duplicate_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate id t1"):
            manifest.load(self.write([row(), row()]))

    def test_test_command_on_hidden_tests_mount_rejected(self):
        with self.assertRaisesRegex(ValueError, "t1.*hidden"):
            manifest.load(self.write([row(test_command="pytest /tests/test_outputs.py")]))

    def test_test_command_naming_a_cached_hidden_test_file_rejected(self):
        # GIVEN a cached task folder whose hidden tests include test_state.py
        hidden = self.dir / "cache/set/t1/tests"
        hidden.mkdir(parents=True)
        (hidden / "test_state.py").write_text("")
        # WHEN the visible test command names that file THEN it is rejected
        with self.assertRaisesRegex(ValueError, "t1.*test_state.py"):
            manifest.load(self.write([row(test_command="pytest -q tests/test_state.py")]))
        # AND a command not naming it passes
        manifest.load(self.write([row(test_command="pytest -q tests/test_other.py")]))

    def test_bad_kind_rejected(self):
        with self.assertRaisesRegex(ValueError, "t1.*kind"):
            manifest.load(self.write([row(kind="chore")]))

    def test_cli_prints_summary(self):
        p = self.write([row()])
        out = subprocess.run([sys.executable, str(TASKS / "manifest.py"), str(p)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "demo: 1 tasks valid (1 python; 1 bugfix)")

    def test_committed_tb4_manifest_is_valid(self):
        data = manifest.load(TASKS / "tb4-software.json")
        self.assertTrue(5 <= len(data["tasks"]) <= 8)


if __name__ == "__main__":
    unittest.main()
