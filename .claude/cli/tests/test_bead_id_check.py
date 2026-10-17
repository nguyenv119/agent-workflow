"""Tests for .claude/hooks/bead-id-check: the comment checker and its PreToolUse commit hook.

Every planted id is assembled at runtime from fragments, so this file never holds a literal id
for the check (or a later repo-wide scan) to trip on.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, TmpCase, make_project, run, set_env

HOOK_DIR = CLAUDE / "hooks/bead-id-check"
SCRIPT = HOOK_DIR / "check_comment_bead_ids.py"
spec = importlib.util.spec_from_file_location("guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

PREFIX = "proj" + "-"
FULL = PREFIX + "ab" + "c12"
FULL_CHILD = FULL + ".3" + ".2"
SHORT_A = "dj" + "b8" + ".17"
SHORT_B = "wt" + "4" + ".10"
SHORT_C = "uw" + "4e" + ".23"
KW_REL = "bead" + " .4"
KW_EPIC_REL = "Epic" + " .12"
KW_ID = "bead " + "am" + "y9"

RULES_TEXT = f"# Project rules\n\n## beads\n- bead-id-prefix: {PREFIX}\n- bead-id-allow: api, cli\n"
RULES = guard.make_rules(PREFIX, ["api", "cli"])
BYPASS = "  # agent-cli: allow bead-id-comment"


def hits(path: str, text: str, rules=RULES) -> list[str]:
    return [m for _, m, _ in guard.find_hits(path, text, rules)]


def settings_command() -> str:
    """The exact bead-id-check command line from this checkout's settings.json."""
    groups = json.loads((CLAUDE / "settings.json").read_text())["hooks"]["PreToolUse"]
    cmds = [h["command"] for g in groups for h in g["hooks"] if "bead-id-check" in h["command"]]
    assert len(cmds) == 1, cmds
    return cmds[0]


class Repo:
    """A throwaway git repo (agent-cli and the bead-id-check hook installed) for the mode tests."""

    def __init__(self, root: Path, rules: str | None = RULES_TEXT):
        self.root = make_project(root, hooks=("bead-id-check",))
        if rules is not None:
            self.write(".claude/project-rules.md", rules)

    def git(self, *args: str) -> str:
        return run(GIT + ["-c", "core.hooksPath=/dev/null", *args], self.root).stdout

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg: str = "c") -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def run(self, *args: str, script: Path = SCRIPT) -> subprocess.CompletedProcess:
        return run([sys.executable, str(script), *args], self.root, check=False)

    def hook(self, command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        """Pipe a PreToolUse payload to the exact settings.json command, as Claude Code does."""
        cwd = cwd or self.root
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin:" + str(Path(sys.executable).parent),
               "HOME": str(self.root), "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(["sh", "-c", settings_command()], cwd=cwd, input=payload, env=env,
                              text=True, capture_output=True)


class Positives(unittest.TestCase):
    def test_each_id_format_in_a_line_comment_is_found(self):
        """Every bead-id format is caught in a // comment, so no shape slips past the commit hook."""
        # GIVEN one TS comment per format
        for planted in (FULL, FULL_CHILD, SHORT_A, SHORT_B, SHORT_C, KW_REL, KW_EPIC_REL, KW_ID):
            with self.subTest(planted=planted):
                # WHEN the file is scanned
                found = hits("a.ts", f"const x = 1; // see {planted} for why\n")
                # THEN the id is reported
                self.assertEqual(len(found), 1, found)

    def test_ids_are_found_in_every_comment_syntax(self):
        """Block, JSDoc, hash, SQL and CSS comments are all scanned; a missed syntax is a silent hole."""
        # GIVEN the same id in each language's comment syntax
        cases = {
            "a.ts": f"/**\n * Added in {FULL}.\n */\nexport {{}};\n",
            "b.tsx": f"/*\n  plain block line {SHORT_A}\n*/\n",
            "c.py": f"x = 1  # from {SHORT_B}\n",
            "d.sh": f"#!/bin/sh\n# from {FULL}\necho hi\n",
            "e.yml": f"on: push  # from {SHORT_C}\n",
            "f.sql": f"SELECT 1; -- from {FULL}\n",
            "g.css": f".a {{ color: red; /* from {SHORT_A} */ }}\n",
            ".githooks/pre-push": f"#!/usr/bin/env bash\n# from {FULL}\n",
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                # WHEN scanned / THEN exactly one hit
                self.assertEqual(len(hits(path, text)), 1, (path, hits(path, text)))

    def test_overlapping_patterns_report_one_hit(self):
        """A keyword plus a child id is one reference, so it is counted once (no doubled report)."""
        # GIVEN a comment that both the keyword and the short-child pattern match
        text = f"// see bead {SHORT_C}\n"
        # WHEN scanned
        found = hits("a.ts", text)
        # THEN one hit, not two
        self.assertEqual(len(found), 1, found)

    def test_hit_reports_the_right_line_number(self):
        """The reported line is where the comment sits, so the author can find and fix it."""
        # GIVEN an id on line 3 inside a block comment
        text = f"/*\n intro\n see {FULL}\n*/\n"
        # WHEN scanned
        result = guard.find_hits("a.ts", text, RULES)
        # THEN line 3 is reported
        self.assertEqual([n for n, _, _ in result], [3])

    def test_a_word_missing_from_the_allow_list_is_an_id(self):
        """The allow list is what spares a tool name: a word not on it is still caught after the prefix."""
        # GIVEN the allow list api, cli and a comment naming another word after the prefix
        text = f"// run {PREFIX}mcp logs\n"
        # WHEN scanned
        found = hits("a.ts", text)
        # THEN it is reported
        self.assertEqual(found, [PREFIX + "mcp"])


class Negatives(unittest.TestCase):
    def test_english_and_version_lookalikes_are_not_ids(self):
        """Words, versions, property paths and allow-listed tool names are left alone (no false blocks)."""
        # GIVEN comments with lookalikes only
        for text in ("all four", "v1.2", "v12.3", "node20.1", "python3.11", "es2015.1",
                     "utf8.0", "web3.0", "items.2.qty", "data.0.name", "rows.3", "args.0", "nodes.1",
                     "bead would", "epic win", "Epic mtu", "the 2026.10 release",
                     "see https://x.dev/abc1.2", "user@abc1.2", "a ./abc1.2 path",
                     "run " + PREFIX + "cli env", PREFIX + "api"):
            with self.subTest(text=text):
                # WHEN scanned / THEN nothing
                self.assertEqual(hits("a.ts", f"// {text}\n"), [])

    def test_tool_directive_lines_are_skipped(self):
        """Lint and type directives may carry any reason text; they are not prose."""
        # GIVEN directive comments that also contain an id
        for line in (f"// eslint-disable-next-line no-console -- {FULL}",
                     f"/* eslint-disable {FULL} */",
                     f"// @ts-expect-error {FULL}",
                     f"// @ts-ignore {FULL}",
                     f"// biome-ignore lint: {FULL}",
                     f"/* istanbul ignore next {FULL} */"):
            with self.subTest(line=line):
                # WHEN scanned / THEN nothing
                self.assertEqual(hits("a.ts", line + "\n"), [])
        self.assertEqual(hits("a.py", f"import x  # noqa: F401 {FULL}\n"), [])

    def test_ids_in_code_and_strings_are_not_comments(self):
        """Only comments are checked; ids in strings and test titles are code and may stay."""
        # GIVEN ids inside strings and template literals, including // and # inside them
        cases = {
            "a.ts": f'it("{FULL} works", () => {{}});\nconst u = "http://x.dev // {FULL}";\n',
            "b.ts": f"const t = `line one\n// {FULL} still in the template\n`;\n",
            "c.py": f'x = "# {FULL}"\ny = """\n# {FULL} inside a docstring\n"""\n',
            "d.sh": f'echo "# {FULL}"\necho ${{#arr}} {FULL}\n',
            "f.ts": f'const s = "a \\" // {FULL}";\n/* x */ const y = "{FULL}";\n',
            "e.sql": f"SELECT '-- {FULL}';\n",
        }
        for path, text in cases.items():
            with self.subTest(path=path):
                # WHEN scanned / THEN nothing
                self.assertEqual(hits(path, text), [])

    def test_excluded_and_unknown_files_are_not_scanned(self):
        """Snapshots, fixtures and markdown (the default exclude) and unknown types are out of scope."""
        # GIVEN an id comment in excluded or unknown file types
        text = f"// {FULL}\n# {FULL}\n-- {FULL}\n"
        for path in ("a/__snapshots__/x.ts", "a/x.snap", "a/fixtures/x.ts",
                     "README.md", "a.json", "a.txt"):
            with self.subTest(path=path):
                # WHEN scanned / THEN nothing
                self.assertEqual(hits(path, text), [])


class RulesFile(TmpCase):
    def write(self, text: str) -> Path:
        (self.tmp / ".claude").mkdir()
        (self.tmp / ".claude/project-rules.md").write_text(text)
        return self.tmp

    def test_rules_come_from_the_beads_section(self):
        """Prefix and allow list are read from ## beads, so each project names its own tracker prefix."""
        # GIVEN a rules file with prefix and allow list
        repo = self.write(RULES_TEXT)
        # WHEN loaded
        rules = guard.load_rules(repo)
        # THEN an id with that prefix hits and an allowed word does not
        self.assertEqual(hits("a.py", f"# {FULL}\n# {PREFIX}cli\n", rules), [FULL])

    def test_no_prefix_line_means_no_rules(self):
        """A project that never set a prefix gets no check at all, so the hook cannot misfire there."""
        # GIVEN rules files without a prefix line, without the section, and no file at all
        for text in ("## beads\n- bead-id-allow: api\n", "## repeats\n- commands: x\n", None):
            with self.subTest(text=text):
                repo = self.write(text) if text is not None else self.tmp
                # WHEN loaded / THEN None
                self.assertIsNone(guard.load_rules(repo))
                if text is not None:
                    (self.tmp / ".claude/project-rules.md").unlink()
                    (self.tmp / ".claude").rmdir()

    def test_bad_exclude_regex_is_an_error_naming_the_line(self):
        """A broken exclude must be reported, not silently widen or narrow what gets scanned."""
        # GIVEN an exclude that does not compile
        repo = self.write(RULES_TEXT + "- bead-id-exclude: (\n")
        # WHEN loaded / THEN ValueError naming the key
        with self.assertRaisesRegex(ValueError, "bead-id-exclude"):
            guard.load_rules(repo)


class Modes(TmpCase):
    def setUp(self):
        super().setUp()
        set_env(self, GIT_DIR=None, GIT_INDEX_FILE=None)
        self.repo = Repo(self.tmp / "repo")
        # an id that predates the check, on a line nobody touches
        self.repo.write("old.ts", f"// legacy {FULL}\nexport const a = 1;\n")
        self.repo.commit("base")

    def test_staged_blocks_an_added_comment_with_exit_9(self):
        """Pre-commit refuses a newly added id comment and names it."""
        # GIVEN a staged new comment line carrying an id
        self.repo.write("new.ts", f"export const b = 2; // {SHORT_A}\n")
        self.repo.git("add", "new.ts")
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN it exits 9 and names file, line and id
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn(f"new.ts:1: {SHORT_A}", r.stdout)

    def test_staged_ignores_untouched_old_ids_and_clean_additions(self):
        """Only added lines count, so an old id elsewhere in an edited file passes."""
        # GIVEN an edit to old.ts that adds a clean comment below the legacy one
        self.repo.write("old.ts", f"// legacy {FULL}\nexport const a = 1;\n// tidy\n")
        self.repo.git("add", "old.ts")
        # WHEN the staged check runs / THEN it passes
        self.assertEqual(self.repo.run("--staged").returncode, 0)

    def test_staged_reads_the_index_not_the_working_tree(self):
        """The commit is what gets checked, so unstaged edits neither block nor pass it."""
        # GIVEN a clean staged file whose working copy later gains an id comment
        self.repo.write("new.ts", "export const b = 2; // plain\n")
        self.repo.git("add", "new.ts")
        self.repo.write("new.ts", f"export const b = 2; // {SHORT_A}\n")
        # WHEN the staged check runs / THEN it passes
        self.assertEqual(self.repo.run("--staged").returncode, 0)

    def test_staged_fails_open_outside_a_repo(self):
        """A broken environment must never wedge commits."""
        # GIVEN a directory that is not a git repo
        d = self.tmp / "plain"
        d.mkdir()
        # WHEN the staged check runs there
        r = run([sys.executable, str(SCRIPT), "--staged"], d, check=False)
        # THEN it exits 0
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_base_flags_only_lines_added_since_the_base(self):
        """CI mode judges the PR's added lines, not the history it inherits."""
        # GIVEN a commit on top of the base that adds one id comment
        base = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.write("new.py", f"x = 1  # {KW_ID}\n")
        self.repo.commit("add")
        # WHEN checked against the base
        r = self.repo.run("--base", base)
        # THEN only the new line is reported, with exit 9
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("new.py:1:", r.stdout)
        self.assertNotIn("old.ts", r.stdout)

    def test_base_passes_a_clean_change(self):
        """A PR with no id comments passes."""
        # GIVEN a clean commit
        base = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.write("new.py", "x = 1  # plain\n")
        self.repo.commit("add")
        # WHEN checked / THEN exit 0
        self.assertEqual(self.repo.run("--base", base).returncode, 0)

    def test_base_fails_closed_on_an_unknown_ref(self):
        """In CI, a base it cannot resolve must fail the job, not pass it."""
        # GIVEN a ref that does not exist
        # WHEN checked
        r = self.repo.run("--base", "no-such-ref")
        # THEN it exits non-zero and says why
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no-such-ref", r.stderr)

    def test_all_reports_every_tracked_id_comment_and_honours_paths(self):
        """--all is the full-tree scan; paths narrow it; excluded paths stay out."""
        # GIVEN a second tracked file with an id comment and an excluded fixture
        self.repo.write("pkg/b.sql", f"-- {FULL}\nSELECT 1;\n")
        self.repo.write("a/fixtures/x.ts", f"// {FULL}\n")
        self.repo.commit("more")
        # WHEN scanning everything / one path
        everything = self.repo.run("--all")
        narrowed = self.repo.run("--all", "pkg")
        # THEN both files are found in the full scan, only one in the narrowed scan
        self.assertEqual(everything.returncode, 9)
        self.assertIn("old.ts:1:", everything.stdout)
        self.assertIn("pkg/b.sql:1:", everything.stdout)
        self.assertNotIn("fixtures", everything.stdout)
        self.assertNotIn("old.ts", narrowed.stdout)
        self.assertIn("pkg/b.sql:1:", narrowed.stdout)

    def test_exclude_line_replaces_the_default_paths(self):
        """bead-id-exclude lets a project skip its own generated dirs while the rest stays checked."""
        # GIVEN an exclude of ^generated/ and the same id comment staged inside and outside it
        self.repo.write(".claude/project-rules.md", RULES_TEXT + "- bead-id-exclude: ^generated/\n")
        self.repo.commit("rules")
        self.repo.write("generated/x.py", f"# {FULL}\n")
        self.repo.write("src/x.py", f"# {FULL}\n")
        self.repo.git("add", "-A")
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN only the file outside generated/ is reported
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("src/x.py:1:", r.stdout)
        self.assertNotIn("generated/", r.stdout)

    def test_bad_exclude_fails_open_staged_and_closed_base(self):
        """A broken rules line never wedges a commit, but CI must not pass a scan it could not run."""
        # GIVEN an exclude that does not compile and a staged violation
        self.repo.write(".claude/project-rules.md", RULES_TEXT + "- bead-id-exclude: (\n")
        self.repo.write("new.py", f"# {FULL}\n")
        self.repo.git("add", "-A")
        # WHEN both modes run
        staged, base = self.repo.run("--staged"), self.repo.run("--base", "HEAD")
        # THEN staged exits 0, base exits 2, both say which line is wrong
        self.assertEqual((staged.returncode, base.returncode), (0, 2), (staged.stderr, base.stderr))
        self.assertIn("bead-id-exclude", staged.stderr)
        self.assertIn("bead-id-exclude", base.stderr)

    def test_without_the_rules_parser_staged_passes_and_base_fails(self):
        """A copy of the hook installed without agent-cli cannot read rules: commits pass, CI fails loudly."""
        # GIVEN a repo with only the hook dir installed (no .claude/cli) and a staged violation
        bare = make_project(self.tmp / "bare", install=False, hooks=("bead-id-check",))
        (bare / ".claude/project-rules.md").write_text(RULES_TEXT)
        (bare / "new.py").write_text(f"# {FULL}\n")
        run(GIT + ["add", "-A"], bare)
        script = bare / ".claude/hooks/bead-id-check/check_comment_bead_ids.py"
        # WHEN both modes run with that copy
        staged = run([sys.executable, str(script), "--staged"], bare, check=False)
        base = run([sys.executable, str(script), "--base", "HEAD"], bare, check=False)
        # THEN staged exits 0 and base exits 2 naming the missing parser
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(base.returncode, 2, base.stderr)
        self.assertIn("agent_cli", base.stderr)


class NoRules(TmpCase):
    def setUp(self):
        super().setUp()
        set_env(self, GIT_DIR=None, GIT_INDEX_FILE=None)

    def check_off(self, rules: str | None):
        repo = Repo(self.tmp / "repo", rules=rules)
        repo.write("new.py", f"# see {FULL}\n")
        repo.git("add", "-A")
        return repo.run("--staged"), repo.hook("git commit -m x")

    def test_no_beads_section_turns_the_check_off(self):
        """A project with rules but no ## beads section never asked for the check, so commits pass."""
        # GIVEN project rules without ## beads and a staged id comment
        # WHEN the checker and the hook run
        staged, hook = self.check_off("## repeats\n- commands: x\n")
        # THEN both exit 0 and the checker says why
        self.assertEqual((staged.returncode, hook.returncode), (0, 0), (staged.stderr, hook.stderr))
        self.assertIn("no bead-id-prefix", staged.stderr)

    def test_no_rules_file_turns_the_check_off(self):
        """A project with no project-rules.md at all is untouched by the hook."""
        # GIVEN no rules file and a staged id comment
        # WHEN the checker and the hook run
        staged, hook = self.check_off(None)
        # THEN both exit 0
        self.assertEqual((staged.returncode, hook.returncode), (0, 0), (staged.stderr, hook.stderr))


class Hook(TmpCase):
    """The exact settings.json command, fed a PreToolUse payload on stdin."""

    def setUp(self):
        super().setUp()
        set_env(self, GIT_DIR=None, GIT_INDEX_FILE=None)
        self.repo = Repo(self.tmp / "repo")
        self.repo.write("new.py", f"x = 1\n# see {FULL}\n")
        self.repo.git("add", "-A")

    def test_commit_with_a_staged_id_comment_is_blocked(self):
        """The hook is what stops an agent's commit, so a violation must exit 2 and name file:line."""
        # WHEN git commit goes through the hook
        r = self.repo.hook("git commit -m x")
        # THEN exit 2, with the location and the fix
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("new.py:2:", r.stderr)
        self.assertIn("allow bead-id-comment", r.stderr)

    def test_bypass_comment_lets_the_commit_through(self):
        """An agent with a real reason can say so in the command instead of being wedged."""
        # WHEN the commit carries the bypass comment / THEN exit 0
        self.assertEqual(self.repo.hook("git commit -m x" + BYPASS).returncode, 0)

    def test_non_commit_commands_are_ignored(self):
        """Only git commit is gated; every other command runs untouched."""
        # WHEN git status goes through the hook / THEN exit 0, nothing said
        r = self.repo.hook("git status")
        self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_git_dash_c_commit_from_another_dir_is_checked_in_that_repo(self):
        """git -C <repo> commit run from elsewhere must be judged by the repo it commits to."""
        # GIVEN a cwd outside the repo
        other = self.tmp / "elsewhere"
        other.mkdir()
        # WHEN git -C <repo> commit goes through the hook
        r = self.repo.hook(f"git -C {self.repo.root} commit -m x", cwd=other)
        # THEN exit 2 naming the file
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("new.py:2:", r.stderr)


if __name__ == "__main__":
    unittest.main()
