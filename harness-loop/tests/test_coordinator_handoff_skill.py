"""The coordinator-handoff skill: present, short, run from the new coordinator, and clean of personal paths."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

SKILL = ROOT / ".claude/skills/coordinator-handoff/SKILL.md"


class CoordinatorHandoffSkill(unittest.TestCase):
    def test_skill_loads_is_short_and_keeps_one_coordinator(self):
        """SKILL.md has frontmatter, stays under 120 lines, asks the old coordinator before repointing lanes."""
        # GIVEN the skill text
        text = SKILL.read_text()
        # WHEN checked
        # THEN it loads, is short, and orders handoff before repointing
        self.assertEqual(loop.frontmatter_ok(text), set())
        self.assertLess(len(text.splitlines()), 120)
        self.assertIn("Exactly one session may coordinate", text)
        self.assertLess(text.index("## 2a. Give"), text.index("## 3. Repoint the lanes"))
        self.assertIn("argument-hint:", text)
        self.assertIn("is not the user's approval", text)

    def test_skill_passes_the_leak_scan(self):
        """No absolute personal paths or private names."""
        # GIVEN the skill's lines
        lines = SKILL.read_text().splitlines()
        # WHEN scanned with the loop's own leak scan
        # THEN nothing hits
        self.assertEqual(loop.leak_hits(lines, None), [])


if __name__ == "__main__":
    unittest.main()
