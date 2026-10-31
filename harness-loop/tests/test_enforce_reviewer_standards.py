"""enforce-reviewer-standards.sh: the PreToolUse hook that blocks a reviewer spawn missing its standards.

Runs the real script on Agent-call JSON from stdin, the way Claude Code calls it.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude/hooks/enforce-reviewer-standards.sh"
CLI_SKILL = ROOT / ".claude/skills/cli-for-agents"
# Enough standards headings for each reviewer type to pass the checklist check on its own.
CHECKLIST = {"Correctness": "Race/Select, Unbounded, Multi-Step, Retry Scope, Refactor Cleanup",
             "Architecture": "Retry Scope, Dual Code Paths, Derived Data, Review Discipline"}


def hook_var(name):
    """The value of a NAME="..." (or NAME='...') line at the top of the hook script."""
    return re.search(rf"^{name}=([\"'])(.+)\1$", HOOK.read_text(), re.M)[2]


def spawn(role="Correctness", cli=None, extra=""):
    """Agent-call JSON for a reviewer spawn as the coordinator builds it; cli=True/False adds `CLI BEAD: yes/no`."""
    lines = [f"ROLE: {role} Reviewer", f"SKILL: Read and follow .claude/skills/reviewer-{role.lower()}/SKILL.md",
             "## Review Checklist", CHECKLIST[role]]
    if cli is not None:
        lines.append(f"CLI BEAD: {'yes' if cli else 'no'}")
    lines += [extra, "WORKTREE: /tmp/wt", "BASE: origin/main"]
    return json.dumps({"tool_name": "Agent", "tool_input": {"prompt": "\n".join(lines)}})


def run_hook(payload):
    return subprocess.run(["bash", str(HOOK)], input=payload, text=True, capture_output=True)


@unittest.skipUnless(shutil.which("jq"), "the hook needs jq")
class CliBeadReviewers(unittest.TestCase):
    def test_cli_bead_prompt_without_house_rules_signature_is_blocked(self):
        """A `CLI BEAD: yes` reviewer prompt with no house-rules content is blocked, naming the missing signature.

        Without it a CLI bead is reviewed only against whatever the bead text copied, and the house rules
        (one exit-code table, --json, never prompt) go unchecked.
        """
        # GIVEN a correctness spawn marked as a CLI bead that carries a skill heading but no house rules
        payload = spawn(cli=True, extra="**Fail fast with actionable errors**")
        # WHEN the hook runs
        r = run_hook(payload)
        # THEN it blocks and names the house-rules signature
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(hook_var("HOUSE_RULES_SIG"), r.stderr)
        # AND stdout stays empty: on exit 2 the agent is shown only stderr
        self.assertEqual(r.stdout, "")

    def test_cli_bead_prompt_without_a_skill_heading_is_blocked(self):
        """House rules alone are not enough: a CLI-bead prompt must also carry a cli-for-agents section.

        Otherwise the upstream skill's rules (help examples, fail fast, idempotency) silently drop out.
        """
        # GIVEN a CLI-bead spawn with the house-rules phrase but no upstream skill section
        payload = spawn(cli=True, extra=hook_var("HOUSE_RULES_SIG"))
        # WHEN the hook runs
        r = run_hook(payload)
        # THEN blocked, naming the skill
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("cli-for-agents", r.stderr)

    def test_house_rules_heading_quoting_a_skill_section_does_not_count_as_that_section(self):
        """Each house-rules heading quotes a skill heading ("extends ..."), so only a bold `**<heading>**` counts.

        Otherwise pasting one house rule satisfies both halves of the check and the skill's own rules drop out.
        """
        # GIVEN a CLI-bead spawn carrying only house rule 1's full heading, which quotes a skill section
        house = (CLI_SKILL / "house-rules.md").read_text()
        heading = re.search(r"^## 1\. (.+)$", house, re.M)[1]
        # WHEN the hook runs
        r = run_hook(spawn(cli=True, extra=heading))
        # THEN blocked, naming the missing skill heading
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("cli-for-agents/SKILL.md", r.stderr)

    def test_cli_bead_prompt_with_signature_and_heading_passes(self):
        """A CLI-bead spawn carrying the house-rules phrase and a skill section passes, for either reviewer."""
        for role in ("Correctness", "Architecture"):
            # GIVEN a full CLI-bead prompt
            payload = spawn(role, cli=True, extra=f"{hook_var('HOUSE_RULES_SIG')}\n**`--help` that works**")
            # WHEN the hook runs
            r = run_hook(payload)
            # THEN allowed
            with self.subTest(role=role):
                self.assertEqual(r.returncode, 0, r.stdout)

    def test_reviewers_not_marked_cli_are_unchanged(self):
        """No `CLI BEAD: yes` line (absent or `no`) means no CLI check: the standards-only prompt still passes."""
        for cli in (None, False):
            # GIVEN a reviewer spawn without CLI content
            # WHEN the hook runs
            r = run_hook(spawn(cli=cli))
            # THEN allowed, as before this check existed
            with self.subTest(cli=cli):
                self.assertEqual(r.returncode, 0, r.stdout)

    def test_cli_bead_marker_mid_line_does_not_trigger_the_cli_check(self):
        """Only a line that starts with `CLI BEAD: yes` marks a CLI bead; quoting the phrase mid-sentence does not.

        Reviewer prompts often quote the bead goal ("keyed on a `CLI BEAD: yes` line"); if the marker matched
        anywhere, every such non-CLI review would be blocked.
        """
        # GIVEN a non-CLI reviewer spawn whose summary quotes the marker mid-line
        payload = spawn(extra="SUMMARY: the hook is keyed on a `CLI BEAD: yes` line")
        # WHEN the hook runs
        r = run_hook(payload)
        # THEN allowed: the CLI check did not fire
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_hook_signatures_exist_in_the_skill_files(self):
        """The phrase and headings the hook demands are really in house-rules.md and the skill's SKILL.md.

        If either file is reworded, the coordinator's freshly extracted checklist would stop matching and
        every CLI-bead review would be blocked; this fails first.
        """
        # GIVEN the hook's signature variables and the installed skill
        house, skill = (CLI_SKILL / "house-rules.md").read_text(), (CLI_SKILL / "SKILL.md").read_text()
        headings = hook_var("CLI_SKILL_HEADINGS").split("|")
        # WHEN looked up / THEN each is present
        self.assertIn(hook_var("HOUSE_RULES_SIG"), house)
        for h in headings:
            with self.subTest(heading=h):
                self.assertIn(f"## {h}\n", skill)


if __name__ == "__main__":
    unittest.main()
