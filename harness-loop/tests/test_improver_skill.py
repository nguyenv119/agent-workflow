"""The improver skill and harness map: present, short, and pointing at files that exist."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMPROVER = ROOT / "harness-loop/improver"
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402


class ImproverSkill(unittest.TestCase):
    def test_skill_has_frontmatter_rules_and_proposal_fields(self):
        """SKILL.md has name/description frontmatter, stays under 120 lines, and names every proposal field."""
        # GIVEN the skill text
        text = (IMPROVER / "SKILL.md").read_text()
        # WHEN checked
        # THEN it loads as a skill, is short, and carries rule 8 and the five fields loop.py looks for
        self.assertEqual(loop.frontmatter_ok(text), set())
        self.assertLess(len(text.splitlines()), 120)
        self.assertIn("Never remove or weaken a quality gate", text)
        for field in ("Idea:", "Files:", "Evidence:", "Expected effect:", "Risk:"):
            self.assertIn(field, text)

    def test_skill_and_map_pass_the_leak_scan(self):
        """Neither file trips the loop's own leak scan (no absolute paths, tool names only on e.g. lines)."""
        # GIVEN both files' lines
        lines = [ln for f in ("SKILL.md", "harness-map.md") for ln in (IMPROVER / f).read_text().splitlines()]
        # WHEN scanned
        # THEN nothing hits
        self.assertEqual(loop.leak_hits(lines, None), [])


class HarnessMap(unittest.TestCase):
    def test_every_path_in_the_map_exists(self):
        """Each `.claude/...` or `AGENTS.md` path the map cites exists, so the map cannot drift silently."""
        # GIVEN the backticked paths in the map (a trailing / means a dir)
        text = (IMPROVER / "harness-map.md").read_text()
        paths = set(re.findall(r"`((?:\.claude/|AGENTS\.md)[^`\s]*)`", text))
        # WHEN each is looked up
        missing = sorted(p for p in paths if not (ROOT / p).exists())
        # THEN all exist
        self.assertGreater(len(paths), 15)
        self.assertEqual(missing, [])

    def test_every_section_the_map_names_is_a_heading(self):
        """Each quoted section name and each coordinator §N / §Nx in the map is a real heading."""
        # GIVEN the map's quoted names and § references, and every heading in the harness
        text = (IMPROVER / "harness-map.md").read_text()
        files = list((ROOT / ".claude").rglob("*.md")) + [ROOT / "AGENTS.md"]
        headings = [ln for f in files for ln in f.read_text().splitlines() if ln.startswith("#")]
        coord = (ROOT / ".claude/skills/coordinator/SKILL.md").read_text().splitlines()
        quoted = set(re.findall(r'"([^"`]+)"', text))
        refs = set(re.findall(r"§(\d)([a-z]\d?)?", text))
        # WHEN each is looked up
        missing = sorted(q for q in quoted if not any(q in h for h in headings))
        missing += [f"§{n}{x}" for n, x in refs
                    if not any(ln.startswith(f"#### {x}." if x else f"### {n}.") for ln in coord)]
        # THEN every one exists, so a renamed or renumbered section fails here
        self.assertGreater(len(quoted), 15)
        self.assertEqual(missing, [])

    def test_every_hook_and_reviewer_is_mapped(self):
        """Every hook file and every reviewer skill on disk is named in the map."""
        # GIVEN the hook and reviewer files on disk
        text = (IMPROVER / "harness-map.md").read_text()
        names = [p.name for p in (ROOT / ".claude/hooks").iterdir() if p.is_file()]
        names += [p.name for p in (ROOT / ".claude/skills").glob("reviewer-*")]
        # WHEN looked up in the map
        # THEN each appears (reviewer-plan is the planner's, not /work's, so it is excluded)
        self.assertEqual([n for n in names if n not in text and n != "reviewer-plan"], [])


if __name__ == "__main__":
    unittest.main()
