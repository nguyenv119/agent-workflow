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
        # THEN it loads as a skill, is short, and carries rule 8 and the five proposal fields
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

    def test_every_quoted_section_is_a_heading_of_the_file_it_follows(self):
        """Each quoted section name in the map is a heading of the file named just before it in its cell."""
        # GIVEN each table cell, read left to right: a file reference sets the current file
        alias = {"coordinator": ".claude/skills/coordinator/SKILL.md", "implementer": ".claude/skills/implementer/SKILL.md"}
        token = re.compile(r'`([^`]+)`|"([^"]+)"|\b(coordinator|implementer)\b')
        checked, missing = 0, []
        for cell in (IMPROVER / "harness-map.md").read_text().split("|"):
            current = None
            for path, quoted, word in token.findall(cell):
                ref = alias.get(word) or path
                if ref and (ROOT / ref).is_file():
                    current = ROOT / ref
                elif quoted:
                    # WHEN the quoted name is looked up among that file's headings
                    heads = [ln for ln in current.read_text().splitlines() if ln.startswith("#")] if current else []
                    checked += 1
                    if not any(quoted in h for h in heads):
                        missing.append(f"{quoted} ({current and current.name})")
        # THEN every one is there, so a renamed section or a name moved to another file fails here
        self.assertGreater(checked, 15)
        self.assertEqual(missing, [])

    def test_every_coordinator_section_number_is_a_heading(self):
        """Each coordinator §N is a `### N.` heading and each §Nx a `#### x.` heading inside §N."""
        # GIVEN the coordinator's headings (outside code fences) split into §N blocks
        blocks, n, fenced = {}, None, False
        for ln in (ROOT / ".claude/skills/coordinator/SKILL.md").read_text().splitlines():
            fenced ^= ln.startswith("```")
            if fenced:
                continue
            m = re.match(r"(#{2,4}) (\w+)\.", ln)
            if ln.startswith("## ") or ln.startswith("### "):
                n = m.group(2) if m and m.group(1) == "###" else None
                blocks.setdefault(n, set())
            elif m and m.group(1) == "####" and n:
                blocks[n].add(m.group(2))
        refs = set(re.findall(r"§(\d)([a-z]\d?)?", (IMPROVER / "harness-map.md").read_text()))
        # WHEN each reference is looked up
        missing = [f"§{n}{x}" for n, x in refs if n not in blocks or (x and x not in blocks[n])]
        # THEN all exist under the right parent, so renumbering fails here
        self.assertGreater(len(refs), 8)
        self.assertEqual(missing, [])

    def test_every_hook_script_and_reviewer_in_the_map_exists(self):
        """Each script file name and reviewer-* skill the map names exists, so a deleted one fails here."""
        # GIVEN the bare script names and reviewer dirs the map cites
        text = (IMPROVER / "harness-map.md").read_text()
        scripts = set(re.findall(r"`([\w.-]+\.(?:sh|py))`", text))
        reviewers = set(re.findall(r"\b(reviewer-[\w-]+)/", text))
        on_disk = {p.name for p in (ROOT / ".claude").rglob("*")}
        # WHEN looked up on disk
        missing = sorted(n for n in scripts | reviewers if n not in on_disk)
        # THEN all exist
        self.assertGreater(len(scripts), 5)
        self.assertEqual(missing, [])

    def test_skill_names_every_path_the_round_message_gives(self):
        """The message keys the skill lists are exactly the keys loop.py writes into message.txt."""
        # GIVEN the keys in loop.py's round message and the skill's message line
        source = (ROOT / "harness-loop/loop.py").read_text()
        keys = set(re.findall(r'f"(\w+): \{', source[source.index("Follow the improver skill"):][:400]))
        skill = (IMPROVER / "SKILL.md").read_text()
        # WHEN compared
        # THEN the skill names each one
        self.assertEqual(len(keys), 7)
        self.assertEqual(sorted(k for k in keys if f"`{k}`" not in skill), [])

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
