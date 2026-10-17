"""Text checks over .github/workflows/ci.yml (stdlib only, no yaml parser)."""
import re
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
            if m[1].strip() in ("|", ">", "|-", ">-"):
                block = indent
            else:
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

    def test_missing_secret_fails_closed(self):
        """An empty LEAK_PATTERNS would make the scan silently weaker; the step must exit non-zero instead."""
        # GIVEN the leak job's run scripts
        script = "\n".join(run_lines(job(self.text, "leak")))
        # WHEN checking the empty-secret branch
        m = re.search(r'-z "\$LEAK_PATTERNS"(.*?)\bfi\b', script, re.S)
        # THEN it exists and exits 1 before the fi
        self.assertIsNotNone(m)
        self.assertIn("exit 1", m[1])

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
