"""The gardener skill: present, short, proposals-only, and clean of personal paths."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

SKILL = ROOT / ".claude/skills/gardener/SKILL.md"


class GardenerSkill(unittest.TestCase):
    def test_skill_loads_is_short_and_names_its_rungs(self):
        """SKILL.md has frontmatter, stays under 150 lines, ranks architecture above doc text, and keeps the twice-seen rule."""
        # GIVEN the skill text
        text = SKILL.read_text()
        # WHEN checked
        # THEN it loads as a skill, is short, and carries the ladder and the evidence bar
        self.assertEqual(loop.frontmatter_ok(text), set())
        self.assertLess(len(text.splitlines()), 150)
        for phrase in ("types/lint/hook/script", "seen twice"):
            self.assertIn(phrase, text)
        self.assertLess(text.index("1. **architecture**"), text.index("4. **doc text**"))

    def test_skill_passes_the_leak_scan(self):
        """No absolute personal paths, private tries folders or stack names outside e.g. lines."""
        # GIVEN the skill's lines
        lines = SKILL.read_text().splitlines()
        # WHEN scanned with the loop's own leak scan
        # THEN nothing hits
        self.assertEqual(loop.leak_hits(lines, None), [])

    def test_window_is_a_caller_parameter_dated_by_record_or_birth_time(self):
        """Pass 2 gathers a caller-chosen window (default the 7 days ending now), dates inputs by record
        timestamp or birth date, never mtime alone, and ignores anything after the window end."""
        # GIVEN the skill text
        text = SKILL.read_text()
        # WHEN checked / THEN the window clause, its dating rule and the end-date report dir are there
        for phrase in ("### 1. Gather the window", "window: YYYY-MM-DD..YYYY-MM-DD", "7 days ending now",
                       "st_birthtime", "never mtime alone", "after the window end is ignored",
                       "named after the window end date", "transcripts: <dir>", "user turns only"):
            self.assertIn(phrase, text)
        self.assertNotIn("Gather the week", text)

    def test_proof_contract_and_report_root_let_a_caller_score_a_run(self):
        """A proof is a report-dir file run as `<proof> <input path>` (non-zero on the mistake), listed per
        class with the input it fired on; report-root moves replays away from the weekly reports."""
        # GIVEN the skill text
        text = SKILL.read_text()
        # WHEN checked / THEN the contract and the report-root parameter are there
        for phrase in ("`<proof> <input path>`", "non-zero", "the input it fired on", "report-root: <dir>"):
            self.assertIn(phrase, text)

    def test_pass_3_scans_the_window_not_the_last_7_days(self):
        """Pass 3 hands the window to `hook scan --from <start> --until <end>`, so a past week scans that week."""
        # GIVEN Pass 3
        pass3 = loop.section(SKILL.read_text(), "## Pass 3: chores still hand-rolled")
        # WHEN checked / THEN it uses the window form and no longer the fixed duration
        self.assertIn("agent-cli hook scan --from <start> --until <end> --json", pass3)
        self.assertNotIn("--since 7d", pass3)

    def test_filing_is_only_what_the_caller_names(self):
        """Proposals only, except beads.md proposals filed into a bead db the caller's prompt names, ids in filed.txt."""
        # GIVEN the Output and limits section
        limits = loop.section(SKILL.read_text(), "## Output and limits")
        # WHEN checked / THEN proposals-only stays and the one filing exception is spelled out
        self.assertIn("files nothing itself", limits)
        for phrase in ("explicitly names a bead db", "filed.txt", "nothing else is ever filed"):
            self.assertIn(phrase, limits)
        # AND Pass 2's Route step points at that exception instead of a flat "do not file"
        self.assertIn("Do not file it, unless the caller names a bead db", SKILL.read_text())

    def test_prune_points_at_gardener_as_weekly_driver(self):
        """prune's Phase 5 names the gardener skill, so the weekly rescan has one driver."""
        # GIVEN prune's Phase 5 section
        phase5 = loop.section((ROOT / ".claude/skills/prune/SKILL.md").read_text(),
                              "## Phase 5 — Keep it pruned (no CI, so a rescan)")
        # WHEN looked up / THEN the section exists and points at gardener
        self.assertTrue(phase5.strip())
        self.assertIn("gardener", phase5)


if __name__ == "__main__":
    unittest.main()
