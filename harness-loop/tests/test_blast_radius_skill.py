"""The blast-radius skill and the measured-number checklist: present, short, wired in, and pointing at real files."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

SKILL = ROOT / ".claude/skills/blast-radius/SKILL.md"
WIN = ROOT / ".claude/skills/win-condition/SKILL.md"
CHECKLIST = "## Before reporting a measured number"
CREDIT = "cursor/plugins (pstack, MIT License"


class BlastRadiusSkill(unittest.TestCase):
    def test_skill_loads_is_short_and_credits_its_source(self):
        """SKILL.md has frontmatter, stays under 80 lines, names all 4 proof levels, the output lines and the MIT credit."""
        # GIVEN the skill text
        text = SKILL.read_text()
        # WHEN checked
        # THEN it loads as a skill, is short, and carries the levels, output shape and credit
        self.assertEqual(loop.frontmatter_ok(text), set())
        self.assertLess(len(text.splitlines()), 80)
        for phrase in ("**Asserted.**", "**Cited code.**", "**Test.**", "**Ran real code.**",
                       "Risky beads need level 4", "Safe because:", "Proved by:", CREDIT):
            self.assertIn(phrase, text)

    def test_skill_passes_the_leak_scan(self):
        """No absolute personal paths, private tries folders or stack names."""
        # GIVEN the skill's lines / WHEN scanned with the loop's own leak scan / THEN nothing hits
        self.assertEqual(loop.leak_hits(SKILL.read_text().splitlines(), None), [])

    def test_every_repo_path_the_skill_names_exists(self):
        """Each `.claude/...` path in the skill points at a file in this repo."""
        # GIVEN the backticked repo paths in the skill
        paths = re.findall(r"`(\.claude/[^`\s]+)`", SKILL.read_text())
        # WHEN each is resolved / THEN there is at least one and all exist
        self.assertTrue(paths)
        for rel in paths:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_coordinator_acceptance_step_runs_it_for_risky_beads(self):
        """The coordinator's real-acceptance step tells risky beads to paste the 'Safe because' line."""
        # GIVEN the coordinator skill / WHEN read / THEN the c2 step carries the line
        text = (ROOT / ".claude/skills/coordinator/SKILL.md").read_text()
        c2 = text[text.index("#### c2."):]
        self.assertIn("Risky beads: also run the blast-radius skill", c2[:c2.index("\n#### ")])


class MeasuredNumberChecklist(unittest.TestCase):
    def test_win_condition_carries_all_seven_questions_and_credit(self):
        """The section lists exactly 7 numbered questions, the not-checked rule and the MIT credit."""
        # GIVEN the win-condition section
        sec = loop.section(WIN.read_text(), CHECKLIST)
        # WHEN its numbered lines are counted
        questions = [line for line in sec.splitlines() if re.match(r"\d\. \*\*", line)]
        # THEN there are 7, plus the honesty rule and the credit
        self.assertEqual(len(questions), 7)
        self.assertIn("`not checked`", sec)
        self.assertIn(CREDIT, sec)
        self.assertEqual(loop.leak_hits(sec.splitlines(), None), [])

    def test_agents_table_lists_both_rules_as_skill_text_only(self):
        """AGENTS.md's Rule -> enforcer table has a row per new rule, honestly marked as unenforced."""
        # GIVEN the AGENTS.md table section
        sec = loop.section((ROOT / "AGENTS.md").read_text(), "## Rule -> enforcer")
        rows = {line.split("|")[1].strip(): line for line in sec.splitlines() if line.startswith("| `")}
        # WHEN the two new rows are looked up / THEN both exist and say skill text only
        for rule in ("`blast-radius-proof`", "`measured-number-checklist`"):
            self.assertIn(rule, rows)
            self.assertTrue(rows[rule].rstrip().endswith("| skill text only |"), rows[rule])

    def test_gardener_table_starts_from_agents_md(self):
        """Gardener §6 reads the AGENTS.md table first and only proposes rows."""
        # GIVEN gardener §6 / WHEN read / THEN it names the AGENTS.md table and forbids editing it
        text = (ROOT / ".claude/skills/gardener/SKILL.md").read_text()
        sec = text[text.index("### 6. Rule-to-enforcer table and retirements"):text.index("## report.md shape")]
        self.assertIn("`## Rule -> enforcer` table in the repo's `AGENTS.md`", sec)
        self.assertIn("never edit `AGENTS.md` yourself", sec)


if __name__ == "__main__":
    unittest.main()
