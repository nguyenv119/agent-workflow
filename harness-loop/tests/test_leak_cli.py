"""Tests for `loop.py leak`, the free pre-push leak scan, run against a real temp git repo."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import make_repo

LOOP = Path(__file__).resolve().parents[1] / "loop.py"


class LeakCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="leak-cli-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "loop-home"
        self.home.mkdir()
        (self.home / "leak-patterns.txt").write_text("# private names\nsecretco\n")
        self.repo, _ = make_repo(self.tmp / "repo", {"a.txt": "secretco was here before\n"})
        subprocess.run(["git", "-C", str(self.repo), "branch", "base"], check=True)

    def leak(self, *args):
        return subprocess.run([sys.executable, str(LOOP), "leak", *args], cwd=self.repo, text=True,
                              capture_output=True, env={**os.environ, "HARNESS_LOOP_HOME": str(self.home)})

    def test_planted_private_name_in_an_added_line_exits_1(self):
        """A private name added on the branch is printed and fails the scan before a push makes it public."""
        # GIVEN a commit adding a line with the planted private name
        make_repo(self.repo, {"b.txt": "ok line\nwe ship for SecretCo\n"})
        # WHEN scanned against the base
        r = self.leak("--diff", "base")
        # THEN exit 1 naming that line only
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("we ship for SecretCo", r.stdout)
        self.assertNotIn("ok line", r.stdout)

    def test_unchanged_and_removed_lines_are_not_scanned(self):
        """Only added lines count: a pre-existing hit on the base is not the branch's leak."""
        # GIVEN a branch commit that removes the old hit and adds a clean line
        make_repo(self.repo, {"a.txt": "clean now\n"})
        # WHEN scanned
        r = self.leak("--diff", "base")
        # THEN clean, exit 0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_body_file_is_scanned(self):
        """The PR body is public too: a home-dir path in it is a hit."""
        # GIVEN a body file with an absolute user path
        body = self.tmp / "body.md"
        home_path = "/" + "Users/someone/x"  # split so this file does not trip the scan it tests
        body.write_text(f"## Evidence\nran {home_path}\n")
        # WHEN scanned
        r = self.leak("--body", str(body))
        # THEN exit 1
        self.assertEqual(r.returncode, 1)
        self.assertIn(home_path, r.stdout)

    def test_missing_patterns_file_exits_3(self):
        """Without the private patterns file the scan would be weaker; it is blocked (3), never clean."""
        # GIVEN no leak-patterns.txt
        (self.home / "leak-patterns.txt").unlink()
        # WHEN scanned
        r = self.leak("--diff", "base")
        # THEN exit 3 naming the file
        self.assertEqual(r.returncode, 3)
        self.assertIn("leak-patterns.txt", r.stderr)

    def test_no_input_is_a_usage_error(self):
        """With neither --diff nor --body there is nothing to scan; saying `clean` would be a lie."""
        # GIVEN / WHEN no args
        r = self.leak()
        # THEN exit 2
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
