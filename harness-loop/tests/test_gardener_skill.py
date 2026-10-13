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
