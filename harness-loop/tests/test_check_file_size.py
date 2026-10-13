"""Tests for .claude/skills/standards/check-file-size.py: flag files a branch pushes past 1,000 lines.

Each test builds a throwaway repo with a base commit on main, branches off, changes one file, and
runs the script as a subprocess (as the coordinator's quality gate does) with main as the base ref.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import GIT, make_repo, run

SCRIPT = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "standards" / "check-file-size.py"


def lines(n):
    """Return file content with exactly n lines."""
    return "".join(f"line {i}\n" for i in range(n))


class CheckFileSizeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="check-file-size-test-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def branch(self, base_files, branch_files):
        """Commit base_files on main, then branch_files on a feature branch; return the repo path."""
        repo, _ = make_repo(self.tmp / "repo", base_files)
        run(GIT + ["checkout", "-qb", "feature"], repo)
        make_repo(repo, branch_files)
        return repo

    def check(self, repo):
        """Run the script against base ref main; return the CompletedProcess."""
        return subprocess.run([sys.executable, str(SCRIPT), "main"], cwd=repo, text=True, capture_output=True)

    def test_file_growing_from_990_to_1010_lines_fails(self):
        """
        Verifies a file crossing from under 1,000 lines to 1,000 or more makes the check exit 1
        and names the file with its before and after counts.

        This is the whole point of the check: a reviewer should not have to notice by eye that a
        diff tipped a file over the size line. If it passes silently, oversized files slip through.
        """
        # GIVEN — big.py is 990 lines on main and 1010 on the branch
        repo = self.branch({"big.py": lines(990)}, {"big.py": lines(1010)})

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it fails and names the file with both counts
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("big.py: 990 -> 1010", result.stdout)

    def test_untracked_1000_line_file_fails(self):
        """
        Verifies a new 1,000-line file that is neither committed nor staged still trips the check.

        The implementer runs the check before committing, so untracked files must count or a
        freshly written oversized file passes unnoticed.
        """
        # GIVEN — a feature branch with no changes, plus an untracked 1000-line new.py
        repo = self.branch({"small.py": lines(10)}, {})
        (repo / "new.py").write_text(lines(1000))

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it fails and names new.py as a new file
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("new.py: 0 -> 1000", result.stdout)

    def test_file_staying_under_1000_lines_passes(self):
        """
        Verifies a file that grows but stays under 1,000 lines does not trip the check.

        A false alarm on every growing file would make the gate noise that people learn to ignore.
        """
        # GIVEN — small.py grows from 990 to 995 lines
        repo = self.branch({"small.py": lines(990)}, {"small.py": lines(995)})

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it passes
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_file_already_over_1000_lines_passes(self):
        """
        Verifies a file that was already 1,000+ lines before the branch does not trip the check.

        The rule is about a PR crossing the line, not about every later edit to an old large file;
        flagging those would block unrelated work on files this PR did not make big.
        """
        # GIVEN — legacy.py grows from 1200 to 1300 lines
        repo = self.branch({"legacy.py": lines(1200)}, {"legacy.py": lines(1300)})

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it passes
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_file_created_over_1000_lines_fails(self):
        """
        Verifies a brand-new file of 1,000+ lines counts as crossing (before = 0).

        Otherwise splitting work into a new oversized file would dodge the check entirely.
        """
        # GIVEN — new.py does not exist on main and has 1000 lines on the branch
        repo = self.branch({"keep.py": lines(1)}, {"new.py": lines(1000)})

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it fails and reports a before count of 0
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("new.py: 0 -> 1000", result.stdout)

    def test_renamed_large_file_passes(self):
        """
        Verifies renaming an already-large file is not reported as a new file crossing the line.

        A rename moves no lines; flagging it would block plain file moves.
        """
        # GIVEN — legacy.py (1200 lines) is renamed to moved.py on the branch
        repo = self.branch({"legacy.py": lines(1200)}, {"legacy.py": None, "moved.py": lines(1200)})

        # WHEN — the check runs against main
        result = self.check(repo)

        # THEN — it passes
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_base_ref_exits_2(self):
        """
        Verifies a base ref git cannot resolve is an error (exit 2), not a silent pass.

        A typo in the gate command must not read as "no files crossed the line".
        """
        # GIVEN — a repo with no branch named nope
        repo = self.branch({"a.py": lines(1)}, {"a.py": lines(2)})

        # WHEN — the check runs against a ref that does not exist
        result = subprocess.run([sys.executable, str(SCRIPT), "nope"], cwd=repo, text=True, capture_output=True)

        # THEN — it exits 2 with an error on stderr
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.strip())


if __name__ == "__main__":
    unittest.main()
