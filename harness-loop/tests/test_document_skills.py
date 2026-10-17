"""The document and anti-slop-writing skills: they load, read the project-rules seam, stay clean of
personal paths, and the document rules file checks this repo's own template."""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

SKILLS = ROOT / ".claude/skills"
DOC = SKILLS / "document"
RULES = DOC / "page-rules.json"
CLI = ROOT / ".claude/bin/agent-cli"


def page(*args):
    return subprocess.run([sys.executable, str(CLI), "page", *args], capture_output=True, text=True)


class DocumentSkills(unittest.TestCase):
    def test_both_skills_load_under_their_folder_name(self):
        """Each SKILL.md has name and description frontmatter (the parse load_check runs), and its name is
        its folder's name; otherwise the skill never loads and an installing project silently lacks it."""
        for name in ("document", "anti-slop-writing"):
            with self.subTest(name):
                # GIVEN the skill text
                text = (SKILLS / name / "SKILL.md").read_text()
                # WHEN its frontmatter is checked
                # THEN nothing is missing and the name matches the folder
                self.assertEqual(loop.frontmatter_ok(text), set())
                self.assertIn(f"\nname: {name}\n", text)

    def test_anti_slop_keeps_its_license(self):
        """The third-party skill keeps its MIT license line."""
        # GIVEN / WHEN the anti-slop skill text / THEN the license stays
        self.assertIn("\nlicense: MIT\n", (SKILLS / "anti-slop-writing/SKILL.md").read_text())

    def test_document_reads_the_project_rules_seam_first(self):
        """The seam names the project's rules file before the user's, under a `## document` section."""
        # GIVEN the document skill text
        text = (DOC / "SKILL.md").read_text()
        # WHEN the seam line is found
        seam = next(line for line in text.splitlines() if "project-rules.md" in line)
        # THEN it names the section and the project file before the user file
        self.assertIn("## document", seam)
        self.assertLess(seam.index("`.claude/project-rules.md`"), seam.index("`~/.claude/project-rules.md`"))

    def test_document_edits_pages_only_through_agent_cli_with_its_rules(self):
        """No `<pages-dir>` placeholder (nothing ever filled it in) and no page command outside
        `agent-cli page ... --rules <skill-dir>/page-rules.json`: without the flag every page passes on the
        built-in defaults, so the rules file would never bite (absolute paths: the leak-scan test)."""
        # GIVEN the document skill text
        text = (DOC / "SKILL.md").read_text()
        # WHEN every code span that runs a page command is read
        commands = [s for s in re.findall(r"`([^`]*)`", text) if re.search(r"\bpage \w", s)]
        # THEN the placeholder is gone, every such command is agent-cli's and carries the rules file
        self.assertNotIn("<pages-dir>", text)
        self.assertGreaterEqual(len(commands), 4)
        for span in commands:
            self.assertTrue(span.startswith("agent-cli page"), span)
            if span != "agent-cli page --help":
                self.assertIn("--rules <skill-dir>/page-rules.json", span, span)

    def test_every_skill_file_passes_the_leak_scan(self):
        """Every text file in both skill folders passes the loop's own leak scan."""
        for folder in (DOC, SKILLS / "anti-slop-writing"):
            for f in sorted(folder.rglob("*")):
                if f.suffix not in (".md", ".json", ".html", ".py"):
                    continue
                # GIVEN a file's lines / WHEN scanned / THEN nothing hits
                self.assertEqual(loop.leak_hits(f.read_text().splitlines(), None), [], str(f))

    def test_rules_name_a_template_next_to_them(self):
        """page-rules.json parses and its template file sits beside it."""
        # GIVEN / WHEN the rules parse / THEN the named template exists
        self.assertTrue((DOC / json.loads(RULES.read_text())["template"]).is_file())

    def test_template_passes_its_own_rules(self):
        """The repo template checks PASS under the rules file shipped next to it; if it did not, every page
        started from it would fail check before the agent wrote a word."""
        # GIVEN the rules / WHEN the template is checked / THEN PASS
        p = page("check", str(DOC / "template.html"), "--rules", str(RULES))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("PASS", p.stdout)

    def test_a_fresh_page_from_the_rules_passes_check(self):
        """`page new` with only the rules file finds the template through it and the page checks PASS, so a
        project needs no --template flag."""
        with tempfile.TemporaryDirectory() as tmp:
            # WHEN a new page is started from the rules
            made = page("new", "demo", "--dir", tmp, "--rules", str(RULES), "--json")
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            new = next(Path(tmp).glob("demo-*.html"))
            # THEN it checks PASS too
            p = page("check", str(new), "--rules", str(RULES))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("PASS", p.stdout)

    def test_forbidden_heading_fails_so_the_rules_are_loaded(self):
        """Fail control: a Backlog h2 outside For agents fails the check with exit 1."""
        with tempfile.TemporaryDirectory() as tmp:
            # GIVEN a template copy with a forbidden heading before For agents
            bad = Path(tmp) / "bad-2026-01-01.html"
            shutil.copy(DOC / "template.html", bad)
            html = bad.read_text()
            cut = html.index('<details class="agents"')
            bad.write_text(html[:cut] + "<h2>Backlog</h2>\n" + html[cut:])
            # WHEN checked / THEN it fails and names the heading
            p = page("check", str(bad), "--rules", str(RULES))
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("Backlog", p.stdout)


if __name__ == "__main__":
    unittest.main()
