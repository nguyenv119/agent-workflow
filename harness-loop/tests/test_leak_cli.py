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

    def test_redact_prints_file_line_and_rule_but_not_the_text(self):
        """CI logs are public: with --redact a private-name hit names file, line and rule only, else the log leaks it."""
        # GIVEN a commit adding a private name on line 2 of b.txt
        make_repo(self.repo, {"b.txt": "ok line\nwe ship for SecretCo\n"})
        # WHEN scanned with --redact
        r = self.leak("--redact", "--diff", "base")
        # THEN exit 1, the location and rule are printed, the planted text is not
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("leak: b.txt:2 (private)", r.stdout)
        self.assertNotIn("SecretCo", r.stdout)

    def test_redact_body_hit_names_body_file_and_users_path_rule(self):
        """A home path in the PR body is reported by body file name and line, without the path itself."""
        # GIVEN a body file with an absolute user path on line 2
        body = self.tmp / "body.md"
        home_path = "/" + "Users/someone/x"  # split so this file does not trip the scan it tests
        body.write_text(f"## Evidence\nran {home_path}\n")
        # WHEN scanned with --redact
        r = self.leak("--redact", "--body", str(body))
        # THEN exit 1 with body.md:2 (users-path) and no path text
        self.assertEqual(r.returncode, 1)
        self.assertIn("leak: body.md:2 (users-path)", r.stdout)
        self.assertNotIn("someone", r.stdout)

    def test_redact_clean_diff_still_reports_clean(self):
        """--redact must not change a clean verdict: CI passes on a clean PR only if the clean line is printed and exit is 0."""
        # GIVEN a branch commit adding two clean lines and a three-line clean body (the shape CI runs)
        make_repo(self.repo, {"c.txt": "fine\nalso fine\n"})
        body = self.tmp / "body.md"
        body.write_text("## Summary\nclean\nstill clean\n")
        # WHEN scanned with --redact over both
        r = self.leak("--redact", "--diff", "base", "--body", str(body))
        # THEN exit 0 and the clean line counts diff and body lines together
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("leak: clean (5 lines)", r.stdout)

    def test_redact_tool_word_without_eg_reports_example_word(self):
        """A tool word without "e.g." is named by its rule, so a CI reader knows which rule fired without the line."""
        # GIVEN a commit adding a tool word with no "e.g." on line 1
        word = "pn" + "pm"  # split so this file does not trip the scan it tests
        make_repo(self.repo, {"d.txt": f"run {word} install\n"})
        # WHEN scanned with --redact
        r = self.leak("--redact", "--diff", "base")
        # THEN exit 1 with the example-word rule
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("leak: d.txt:1 (example-word)", r.stdout)

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
