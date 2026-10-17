"""Checks over .github/workflows/ci.yml: text rules (stdlib only, no yaml parser) and the secret step run under bash."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def run_lines(text):
    """Every line that is part of a `run:` script (the inline value or the indented block under `run: |`)."""
    out, block = [], None
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if block is not None:
            if line.strip() and indent <= block:
                block = None
            else:
                out.append(line)
                continue
        m = re.match(r"\s*(?:- )?run:\s*(.*)$", line)
        if m:
            block = indent  # an inline value may continue on deeper-indented lines; sweep those too
            if m[1].strip() not in ("|", ">", "|-", ">-"):
                out.append(m[1])
    return out


def job(text, name):
    """The text of one job: from `  <name>:` to the next two-space-indented key."""
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    if not m:
        raise AssertionError(f"job {name} not found in {CI}")
    return m[1]


class CIWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = CI.read_text()

    def test_no_expression_inside_a_run_script(self):
        """A `${{ }}` inside run: is pasted into the shell before it runs, so a PR body could inject commands."""
        # GIVEN the workflow's run scripts
        lines = run_lines(self.text)
        # WHEN looking for GitHub expressions in them
        bad = [line for line in lines if "${{" in line]
        # THEN there are run lines, and none holds an expression (values come through env:)
        self.assertTrue(lines)
        self.assertEqual(bad, [])

    def test_leak_step_runs_redacted(self):
        """Without --redact the scan prints the matched line, publishing the private term in a public log."""
        # GIVEN the leak job
        leak = job(self.text, "leak")
        # WHEN finding the scan command
        cmds = [line for line in run_lines(leak) if "loop.py leak" in line]
        # THEN it exists and every invocation is redacted
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn("--redact", c)

    def secret_step(self):
        """The leak job's secret-writing script, from its `if [ -z` line to the printf that writes the file."""
        lines = run_lines(job(self.text, "leak"))
        start = next(i for i, l in enumerate(lines) if '-z "$LEAK_PATTERNS"' in l)
        end = next(i for i, l in enumerate(lines) if "leak-patterns.txt" in l and i > start)
        return "\n".join(lines[start:end + 1])

    def run_secret_step(self, value):
        """Run that script under bash with LEAK_PATTERNS=value; returns (exit code, patterns file or None)."""
        home = Path(tempfile.mkdtemp(prefix="ci-wf-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {"PATH": os.environ["PATH"], "LEAK_PATTERNS": value, "HARNESS_LOOP_HOME": str(home)}
        r = subprocess.run(["bash", "-e", "-c", self.secret_step()], env=env, text=True, capture_output=True)
        f = home / "leak-patterns.txt"
        return r.returncode, f.read_text() if f.is_file() else None

    def test_missing_secret_fails_closed(self):
        """An empty LEAK_PATTERNS would make the scan silently weaker; the step must exit non-zero and write no file."""
        # GIVEN the real secret step run with an empty secret
        # WHEN it runs under bash
        code, written = self.run_secret_step("")
        # THEN it fails and the scan has no patterns file to fall back on
        self.assertEqual(code, 1)
        self.assertIsNone(written)

    def test_present_secret_is_written_as_the_patterns_file(self):
        """The secret is the only source of private patterns; if the step dropped it the scan would run with none."""
        # GIVEN the real secret step run with a two-line secret
        # WHEN it runs under bash
        code, written = self.run_secret_step("alpha\nbeta")
        # THEN it succeeds and the file holds the secret, one pattern per line
        self.assertEqual(code, 0)
        self.assertEqual(written, "alpha\nbeta\n")

    def test_leak_job_runs_on_pull_request_only(self):
        """On push github.base_ref is empty, so the leak diff ref would be `origin/` and the job would break."""
        # GIVEN / WHEN reading the leak job
        leak = job(self.text, "leak")
        # THEN it is restricted to pull_request events
        self.assertRegex(leak, r"if:\s*github\.event_name == 'pull_request'")

    def test_both_unittest_suites_run(self):
        """A suite dropped from CI could break on main with every PR green."""
        # GIVEN the unit job's run scripts
        script = "\n".join(run_lines(job(self.text, "unit")))
        # THEN both discover commands are there
        self.assertIn("python3 -m unittest discover -s harness-loop/tests", script)
        self.assertIn("python3 -m unittest discover -s .claude/cli/tests", script)


if __name__ == "__main__":
    unittest.main()
