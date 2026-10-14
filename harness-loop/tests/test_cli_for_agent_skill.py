"""cursor's cli-for-agents skill, installed whole: byte-identical to upstream at the pinned commit, licensed,
extended only by house-rules.md, and the one place the CLI code points to for its rules."""
import hashlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

SKILL_DIR = ROOT / ".claude/skills/cli-for-agents"
FIXTURE = Path(__file__).resolve().parent / "fixtures/cli_for_agents/upstream.sha256"


def fixture():
    """(commit, {upstream path: sha256}) from the hashes captured when the files were fetched."""
    commit, hashes = None, {}
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("# commit:"):
            commit = line.split(":", 1)[1].strip()
        elif line.strip() and not line.startswith("#"):
            digest, path = line.split(None, 1)
            hashes[path.strip()] = digest
    return commit, hashes


def upstream():
    """The UPSTREAM file's `key: value` lines as a dict."""
    return dict(line.split(": ", 1) for line in (SKILL_DIR / "UPSTREAM").read_text().splitlines() if ": " in line)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CliForAgentsSkill(unittest.TestCase):
    def test_skill_is_upstream_byte_for_byte_at_the_pinned_commit(self):
        """SKILL.md's sha256 equals the fixture's hash of upstream SKILL.md, captured at the commit UPSTREAM pins."""
        # GIVEN the pinned commit and path in UPSTREAM and the hashes captured at fetch time
        pin, (commit, hashes) = upstream(), fixture()
        # WHEN the installed file is hashed
        got = sha256(SKILL_DIR / "SKILL.md")
        # THEN the fixture was captured at the pinned commit and the bytes are unchanged
        self.assertEqual(pin["repo"], "https://github.com/cursor/plugins")
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(commit, pin["commit"])
        self.assertEqual(got, hashes[pin["path"]])

    def test_license_is_upstream_mit_unedited(self):
        """LICENSE says MIT and Cursor's copyright, and is byte-identical to upstream's LICENSE at the pin."""
        # GIVEN the installed LICENSE and the fixture hashes
        text = (SKILL_DIR / "LICENSE").read_text()
        _, hashes = fixture()
        # WHEN checked
        # THEN it is the MIT text, unedited
        self.assertIn("MIT", text)
        self.assertIn("Copyright (c) 2026 Cursor", text)
        self.assertEqual(sha256(SKILL_DIR / "LICENSE"), hashes[upstream()["license"]])

    def test_frontmatter_loads_and_dir_matches_its_name(self):
        """SKILL.md has name and description, and its dir is named after the frontmatter name (so it never needs an edit)."""
        # GIVEN the skill text
        text = (SKILL_DIR / "SKILL.md").read_text()
        # WHEN its frontmatter is read
        name = re.search(r"^name:\s*(\S+)", text, re.M)[1]
        # THEN it loads as a skill and the dir name matches
        self.assertEqual(loop.frontmatter_ok(text), set())
        self.assertEqual(name, SKILL_DIR.name)

    def test_house_rules_are_short_name_all_three_and_cite_real_upstream_sections(self):
        """house-rules.md is under 30 lines, has the exit-code, --json and never-prompt rules, each naming an upstream section that exists."""
        # GIVEN house-rules.md and upstream's section titles
        text = (SKILL_DIR / "house-rules.md").read_text()
        sections = set(re.findall(r"^## (.+)$", (SKILL_DIR / "SKILL.md").read_text(), re.M))
        rules = re.findall(r"^## \d\. .*$", text, re.M)
        # WHEN checked
        cited = [re.search(r'"([^"]+)"', r)[1] for r in rules]
        # THEN it is short, has exactly the three rules, and each extends a real upstream section
        self.assertLess(len(text.splitlines()), 30)
        self.assertEqual(len(rules), 3)
        for phrase in ("exit-code table", "--json", "Never prompt"):
            self.assertIn(phrase, text)
        self.assertTrue(set(cited) <= sections, f"cited {cited}, upstream has {sorted(sections)}")

    def test_house_rules_pass_the_leak_scan(self):
        """No personal paths, private tries folders or stack names in the part of the skill that is ours."""
        # GIVEN house-rules.md and UPSTREAM
        lines = [*(SKILL_DIR / "house-rules.md").read_text().splitlines(),
                 *(SKILL_DIR / "UPSTREAM").read_text().splitlines()]
        # WHEN scanned with the loop's own leak scan
        # THEN nothing hits
        self.assertEqual(loop.leak_hits(lines, None), [])

    def test_cli_code_cites_rules_by_skill_section_not_by_number(self):
        """No .claude/cli file cites an R1-R8 rule number; eval_cmd.py's R1 is the win-condition rule and stays."""
        # GIVEN every Python file under .claude/cli but eval_cmd.py
        files = [p for p in (ROOT / ".claude/cli").rglob("*.py") if p.name != "eval_cmd.py"]
        # WHEN grepped for a bare rule number
        hits = [f"{p.relative_to(ROOT)}:{i}" for p in files
                for i, line in enumerate(p.read_text().splitlines(), 1) if re.search(r"\bR[1-8]\b", line)]
        # THEN there are none, and the code points at the skill instead
        self.assertTrue(files)
        self.assertEqual(hits, [])
        self.assertIn("cli-for-agents", (ROOT / ".claude/cli/agent_cli/core.py").read_text())


if __name__ == "__main__":
    unittest.main()
