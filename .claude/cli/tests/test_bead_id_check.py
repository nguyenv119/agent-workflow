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
SHORT_A = "qz" + "7k" + ".23"
SHORT_B = "hx" + "3" + ".10"
SHORT_C = "kp" + "9m" + ".4"
KW_REL = "bead" + " .4"
KW_EPIC_REL = "Epic" + " .12"
KW_ID = "bead " + "am" + "y9"

RULES_TEXT = f"# Project rules\n\n## beads\n- bead-id-prefix: {PREFIX}\n- bead-id-allow: api, cli\n"
RULES = guard.make_rules(PREFIX, ["api", "cli"])
BYPASS = "  # agent-cli: allow bead-id-comment"
LOOKALIKE = "abc" + "1.2"
# commands whose bypass verdict must match agent-cli's grammar: two real bypasses, two that are not
BYPASS_FIXTURES = (BYPASS, "  #agent-cli:allow bead-id-comment", BYPASS + "-x",
                   "  agent-cli: allow bead-id-comment")


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

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return run([sys.executable, str(SCRIPT), *args], cwd or self.root, check=False)

    def hook(self, command: str, cwd: Path | None = None, **extra_env: str) -> subprocess.CompletedProcess:
        """Pipe a PreToolUse payload to the exact settings.json command, as Claude Code does.

        The test's own interpreter comes first on PATH, so each suite run tests the hook on it.
        """
        cwd = cwd or self.root
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})
        env = {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin:/usr/local/bin",
               "HOME": str(self.root), "CLAUDE_PROJECT_DIR": str(self.root), **extra_env}
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
                # WHEN scanned
                found = hits(path, text)
                # THEN exactly one hit
                self.assertEqual(len(found), 1, (path, found))

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
                     "see https://x.dev/" + LOOKALIKE, "user@" + LOOKALIKE, f"a ./{LOOKALIKE} path",
                     "go1.21", "php8.1", "ruby3.2", "java8.1", "perl5.36", "rust1.75",
                     "lua5.4", "tsc5.3",
                     "run " + PREFIX + "cli env", PREFIX + "api"):
            with self.subTest(text=text):
                # WHEN scanned
                found = hits("a.ts", f"// {text}\n")
                # THEN nothing
                self.assertEqual(found, [])

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
                # WHEN scanned
                found = hits("a.ts", line + "\n")
                # THEN nothing
                self.assertEqual(found, [])
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
                # WHEN scanned
                found = hits(path, text)
                # THEN nothing
                self.assertEqual(found, [])

    def test_excluded_and_unknown_files_are_not_scanned(self):
        """Snapshots, fixtures and markdown (the default exclude) and unknown types are out of scope."""
        # GIVEN an id comment in excluded or unknown file types
        text = f"// {FULL}\n# {FULL}\n-- {FULL}\n"
        for path in ("a/__snapshots__/x.ts", "a/x.snap", "a/fixtures/x.ts",
                     "README.md", "a.json", "a.txt"):
            with self.subTest(path=path):
                # WHEN scanned
                found = hits(path, text)
                # THEN nothing
                self.assertEqual(found, [])


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
                # WHEN loaded
                rules = guard.load_rules(repo)
                # THEN None
                self.assertIsNone(rules)
                if text is not None:
                    (self.tmp / ".claude/project-rules.md").unlink()
                    (self.tmp / ".claude").rmdir()

    def test_bad_exclude_regex_is_an_error_naming_the_line(self):
        """A broken exclude must be reported, not silently widen or narrow what gets scanned."""
        # GIVEN an exclude that does not compile
        repo = self.write(RULES_TEXT + "- bead-id-exclude: (\n")
        # WHEN loaded
        # THEN ValueError naming the key
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
        """Pre-commit refuses a newly added id comment and names it.

        Why: the hook branches on exit 9 and shows stdout to the agent; without file:line:id the
        agent cannot find the comment to fix, and without 9 the commit goes through.
        """
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
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN it passes
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_staged_reads_the_index_not_the_working_tree(self):
        """The commit is what gets checked, so unstaged edits neither block nor pass it."""
        # GIVEN a clean staged file whose working copy later gains an id comment
        self.repo.write("new.ts", "export const b = 2; // plain\n")
        self.repo.git("add", "new.ts")
        self.repo.write("new.ts", f"export const b = 2; // {SHORT_A}\n")
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN it passes
        self.assertEqual(r.returncode, 0, r.stdout)

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
        """A PR with no id comments passes.

        Why: CI runs this on every PR; a false failure on clean code would block every merge
        and teach people to skip the check.
        """
        # GIVEN a clean commit
        base = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.write("new.py", "x = 1  # plain\n")
        self.repo.commit("add")
        # WHEN checked
        r = self.repo.run("--base", base)
        # THEN exit 0
        self.assertEqual(r.returncode, 0, r.stdout)

    ODD_NAMES = ("plain.ts", "a b.ts", "caf" + "é" + ".ts")

    def stage_odd_names(self) -> str:
        """Stage the same id comment under each of ODD_NAMES; return the commit before them."""
        base = self.repo.git("rev-parse", "HEAD").strip()
        for name in self.ODD_NAMES:
            self.repo.write(name, f"// {FULL}\n")
        self.repo.git("add", "-A")
        return base

    def assert_all_odd_names(self, r):
        self.assertEqual(r.returncode, 9, r.stderr)
        for name in self.ODD_NAMES:
            self.assertIn(f"{name}:1: {FULL}", r.stdout)

    def test_staged_reports_files_with_spaces_and_non_ascii_names(self):
        """git tab-terminates a header path with a space and C-quotes a non-ASCII one; both are read.

        Why: a header parsed wrong drops that file's hunks, so its id comments pass silently.
        """
        # GIVEN the same id comment in a plain name, a name with a space and a non-ASCII name
        self.stage_odd_names()
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN all three files are reported
        self.assert_all_odd_names(r)

    def test_base_reports_files_with_spaces_and_non_ascii_names(self):
        """CI reads the same quoted and tab-terminated headers; a dropped file is an unchecked file."""
        # GIVEN a commit adding the id comment under all three names
        base = self.stage_odd_names()
        self.repo.git("commit", "-q", "-m", "add")
        # WHEN checked against the base
        r = self.repo.run("--base", base)
        # THEN all three files are reported
        self.assert_all_odd_names(r)

    def test_staged_still_checks_utf8_files_next_to_a_non_utf8_one(self):
        """A Latin-1 file in the same commit is skipped, not a reason to skip the whole diff.

        Why: decoding the whole diff strictly would raise on that one file and fail the check open.
        """
        # GIVEN a staged Latin-1 file and a staged UTF-8 file with an id comment
        (self.repo.root / "latin.ts").write_bytes(b"// caf\xe9\n")
        self.repo.write("new.ts", f"// {FULL}\n")
        self.repo.git("add", "-A")
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN the UTF-8 file is reported
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn(f"new.ts:1: {FULL}", r.stdout)

    def test_strict_scan_raises_when_git_cannot_show_a_file(self):
        """--base scans strictly: a git error reading a file must surface, not drop that file.

        Why: CI that skips a file it could not read passes a PR it never checked. This proves
        scan() only; that the --base call passes strict=True is read from run_mode, not tested.
        """
        # GIVEN a reader that fails like git show does
        def read(path):
            raise subprocess.CalledProcessError(128, ["git", "show", path])
        # WHEN scanned strictly
        # THEN the error propagates
        with self.assertRaises(subprocess.CalledProcessError):
            guard.scan({"a.ts": None}, read, RULES, strict=True)

    def test_lenient_scan_skips_a_file_git_cannot_show(self):
        """--staged scans leniently: one unreadable file must not wedge a commit.

        Why: the pre-commit check fails open by design; a raise here would surface as exit 0 for
        every file, so skipping just the one keeps the rest checked.
        """
        # GIVEN a reader that fails like git show does
        def read(path):
            raise subprocess.CalledProcessError(128, ["git", "show", path])
        # WHEN scanned leniently
        result = guard.scan({"a.ts": None}, read, RULES)
        # THEN nothing is reported and nothing raises
        self.assertEqual(result, [])

    def test_base_fails_closed_on_an_unknown_ref(self):
        """In CI, a base it cannot resolve must fail the job, not pass it."""
        # GIVEN a ref that does not exist
        # WHEN checked
        r = self.repo.run("--base", "no-such-ref")
        # THEN it exits 2, names the ref and shows a correct invocation
        self.assertEqual(r.returncode, 2)
        self.assertIn("no-such-ref", r.stderr)
        self.assertIn("--base origin/main", r.stderr)

    def test_staged_ignores_diff_prefix_config(self):
        """A user's diff.noprefix or diff.mnemonicPrefix must not switch the check off.

        Why: those settings change the `+++ b/` header the parser keys on; unmatched headers drop
        every hunk and the commit passes unchecked.
        """
        # GIVEN a staged id comment
        self.repo.write("new.ts", f"// {FULL}\n")
        self.repo.git("add", "new.ts")
        for key in ("diff.noprefix", "diff.mnemonicPrefix"):
            with self.subTest(key=key):
                # AND that diff setting on
                self.repo.git("config", key, "true")
                # WHEN the staged check runs
                r = self.repo.run("--staged")
                # THEN it still exits 9 naming the file
                self.assertEqual(r.returncode, 9, r.stderr)
                self.assertIn(f"new.ts:1: {FULL}", r.stdout)
                self.repo.git("config", "--unset", key)

    def test_staged_from_a_subdirectory_ignores_diff_relative(self):
        """diff.relative=true from a subdirectory still reports, with the root-relative path."""
        # GIVEN diff.relative on and a staged id comment in sub/x.py
        self.repo.git("config", "diff.relative", "true")
        self.repo.write("sub/x.py", f"# {FULL}\n")
        self.repo.git("add", "-A")
        # WHEN the staged check runs from sub/
        r = self.repo.run("--staged", cwd=self.repo.root / "sub")
        # THEN exit 9 naming sub/x.py
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("sub/x.py:1:", r.stdout)

    def test_base_ignores_lines_the_base_removed_after_the_branch_point(self):
        """--base judges the branch's own additions, not what main dropped since the branch began.

        Why: a two-dot diff shows a line main deleted as added by a branch that never touched it,
        failing CI for a PR that is not rebased.
        """
        # GIVEN a branch with a clean commit, and main then deleting the legacy id line
        main = self.repo.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.repo.git("checkout", "-q", "-b", "feat")
        self.repo.write("new.py", "x = 1  # plain\n")
        self.repo.commit("feat")
        self.repo.git("checkout", "-q", main)
        self.repo.write("old.ts", "export const a = 1;\n")
        self.repo.commit("drop legacy")
        self.repo.git("checkout", "-q", "feat")
        # WHEN the branch is checked against main
        r = self.repo.run("--base", main)
        # THEN it passes
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_all_with_a_path_matching_nothing_is_a_usage_error(self):
        """A mistyped path must not read as a clean scan."""
        # WHEN --all is given a path with no tracked files
        r = self.repo.run("--all", "nosuchdir")
        # THEN exit 2 naming the path
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no tracked files match nosuchdir", r.stderr)

    def add_more(self):
        self.repo.write("pkg/b.sql", f"-- {FULL}\nSELECT 1;\n")
        self.repo.write("a/fixtures/x.ts", f"// {FULL}\n")
        self.repo.commit("more")

    def test_all_reports_every_tracked_id_comment(self):
        """--all is the full-tree scan: old ids anywhere are reported, excluded paths stay out."""
        # GIVEN a second tracked file with an id comment and an excluded fixture
        self.add_more()
        # WHEN scanning everything
        r = self.repo.run("--all")
        # THEN both files are found and the fixture is not
        self.assertEqual(r.returncode, 9)
        self.assertIn("old.ts:1:", r.stdout)
        self.assertIn("pkg/b.sql:1:", r.stdout)
        self.assertNotIn("fixtures", r.stdout)

    def test_all_with_a_path_scans_only_that_path(self):
        """Paths narrow --all, so a cleanup can go one directory at a time."""
        # GIVEN id comments in old.ts and pkg/b.sql
        self.add_more()
        # WHEN scanning only pkg
        r = self.repo.run("--all", "pkg")
        # THEN only pkg/b.sql is reported
        self.assertNotIn("old.ts", r.stdout)
        self.assertIn("pkg/b.sql:1:", r.stdout)

    def test_all_from_a_subdirectory_reports_root_relative_paths(self):
        """--all names files from the repo root, as --staged and --base do.

        Why: bead-id-exclude is matched against the path; a cwd-relative path would make the
        same exclude skip a file in one mode and not in another.
        """
        # GIVEN an id comment in sub/x.py
        self.repo.write("sub/x.py", f"# {FULL}\n")
        self.repo.commit("sub")
        # WHEN --all runs from sub/
        r = self.repo.run("--all", cwd=self.repo.root / "sub")
        # THEN the path is root-relative
        self.assertEqual(r.returncode, 9, r.stderr)
        self.assertIn("sub/x.py:1:", r.stdout)

    def test_all_from_a_subdirectory_honours_a_root_anchored_exclude(self):
        """An exclude of ^sub/ skips sub/ when --all runs from inside it.

        Why: with cwd-relative paths the anchor never matched there and excluded files got reported.
        """
        # GIVEN an exclude of ^sub/ and an id comment in sub/x.py
        self.repo.write(".claude/project-rules.md", RULES_TEXT + "- bead-id-exclude: ^sub/\n")
        self.repo.write("sub/x.py", f"# {FULL}\n")
        self.repo.commit("sub")
        # WHEN --all runs from sub/
        r = self.repo.run("--all", cwd=self.repo.root / "sub")
        # THEN nothing is reported
        self.assertEqual(r.returncode, 0, r.stdout)

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

    def stage_bad_exclude(self):
        self.repo.write(".claude/project-rules.md", RULES_TEXT + "- bead-id-exclude: (\n")
        self.repo.write("new.py", f"# {FULL}\n")
        self.repo.git("add", "-A")

    def test_bad_exclude_fails_open_in_staged_mode(self):
        """A broken rules line never wedges a commit, and the skip names the line to fix."""
        # GIVEN an exclude that does not compile and a staged violation
        self.stage_bad_exclude()
        # WHEN the staged check runs
        r = self.repo.run("--staged")
        # THEN exit 0, naming the bad line
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("bead-id-exclude", r.stderr)

    def test_bad_exclude_fails_closed_in_base_mode(self):
        """CI must not pass a scan it could not run, and must say which line is wrong."""
        # GIVEN an exclude that does not compile
        self.stage_bad_exclude()
        # WHEN the base check runs
        r = self.repo.run("--base", "HEAD")
        # THEN exit 2, naming the bad line
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("bead-id-exclude", r.stderr)

    def bare_copy(self) -> tuple[Path, Path]:
        """A repo with only the hook dir installed (no .claude/cli) and a staged violation."""
        bare = make_project(self.tmp / "bare", install=False, hooks=("bead-id-check",))
        (bare / ".claude/project-rules.md").write_text(RULES_TEXT)
        (bare / "new.py").write_text(f"# {FULL}\n")
        run(GIT + ["add", "-A"], bare)
        return bare, bare / ".claude/hooks/bead-id-check/check_comment_bead_ids.py"

    def test_without_the_rules_parser_staged_passes(self):
        """A copy of the hook installed without agent-cli cannot read rules, so commits pass."""
        # GIVEN the hook copied without agent-cli
        bare, script = self.bare_copy()
        # WHEN the staged check runs with that copy
        r = run([sys.executable, str(script), "--staged"], bare, check=False)
        # THEN exit 0
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_without_the_rules_parser_base_fails(self):
        """Without agent-cli, CI fails loudly naming the missing parser rather than passing unchecked."""
        # GIVEN the hook copied without agent-cli
        bare, script = self.bare_copy()
        # WHEN the base check runs with that copy
        r = run([sys.executable, str(script), "--base", "HEAD"], bare, check=False)
        # THEN exit 2 naming the parser
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("agent_cli", r.stderr)


class NoRules(TmpCase):
    def setUp(self):
        super().setUp()
        set_env(self, GIT_DIR=None, GIT_INDEX_FILE=None, CLAUDE_PROJECT_DIR=None)

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
        """A project with no project-rules.md at all is untouched by the hook.

        Why: the hook ships in the shared settings.json of every installed repo; blocking where
        no prefix was set would refuse commits in projects that never opted in.
        """
        # GIVEN no rules file and a staged id comment
        # WHEN the checker and the hook run
        staged, hook = self.check_off(None)
        # THEN both exit 0
        self.assertEqual((staged.returncode, hook.returncode), (0, 0), (staged.stderr, hook.stderr))


class ProjectDirRules(TmpCase):
    """A commit in a repo without its own rules file, from a session whose harness dir has one."""

    def setUp(self):
        super().setUp()
        set_env(self, GIT_DIR=None, GIT_INDEX_FILE=None)
        self.work = self.tmp / "work"
        self.work.mkdir()
        run(GIT + ["init", "-q"], self.work)

    def check(self, harness_rules: str | None, work_rules: str | None = None):
        """Stage an id comment in the work repo; run the checker and the hook with the harness as project dir."""
        harness = Repo(self.tmp / "harness", rules=harness_rules)
        if work_rules is not None:
            (self.work / ".claude").mkdir()
            (self.work / ".claude/project-rules.md").write_text(work_rules)
        (self.work / "new.py").write_text(f"# see {FULL} for why\n")
        run(GIT + ["add", "-A"], self.work)
        set_env(self, CLAUDE_PROJECT_DIR=str(harness.root))
        return harness.run("--staged", cwd=self.work), harness.hook("git commit -m x", cwd=self.work)

    def test_rules_fall_back_to_the_project_dir(self):
        """A repo with no rules file is checked with the session's rules, so the hook is not silently off.

        Why: when the harness lives in another dir (named by CLAUDE_PROJECT_DIR), the repo being
        committed to has no .claude of its own, and reading rules only from its top level let ids through.
        """
        # GIVEN prefix rules in the project dir only and a staged id comment in the work repo
        # WHEN the checker and the hook run from the work repo
        staged, hook = self.check(RULES_TEXT)
        # THEN the checker exits 9 and the hook blocks with 2, naming the file
        self.assertEqual((staged.returncode, hook.returncode), (9, 2), (staged.stderr, hook.stderr))
        self.assertIn("new.py:1:", hook.stderr)

    def test_no_prefix_anywhere_turns_the_check_off(self):
        """With no prefix in the repo or the project dir, nobody opted in, so commits pass."""
        # GIVEN no rules file in either place and a staged id comment
        # WHEN the checker and the hook run
        staged, hook = self.check(None)
        # THEN both exit 0
        self.assertEqual((staged.returncode, hook.returncode), (0, 0), (staged.stderr, hook.stderr))
        self.assertIn("no bead-id-prefix", staged.stderr)

    def test_the_repos_own_rules_win_over_the_project_dir(self):
        """A repo that has its own rules file owns the decision, even when it sets no prefix.

        Why: the repo being committed to may belong to another project with another tracker;
        the session's prefix must not be forced on it.
        """
        # GIVEN a work repo rules file without a prefix and prefix rules in the project dir
        # WHEN the checker and the hook run
        staged, hook = self.check(RULES_TEXT, work_rules="## beads\n- bead-id-allow: api\n")
        # THEN both exit 0
        self.assertEqual((staged.returncode, hook.returncode), (0, 0), (staged.stderr, hook.stderr))
        self.assertIn("no bead-id-prefix", staged.stderr)


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
        # WHEN the commit carries the bypass comment, spaced or not
        for bypass in (BYPASS, "  #agent-cli:allow bead-id-comment"):
            with self.subTest(bypass=bypass):
                r = self.repo.hook("git commit -m x" + bypass)
                # THEN exit 0
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_bypass_for_a_longer_rule_id_is_not_this_bypass(self):
        """A bypass naming another rule (bead-id-comment-x) must not switch this one off."""
        # WHEN the commit carries a bypass for a different, longer rule id
        r = self.repo.hook("git commit -m x" + BYPASS + "-x")
        # THEN the commit is still blocked
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_bypass_grammar_matches_agent_cli(self):
        """hook.py copies agent-cli's bypass grammar; the two must agree on every fixture."""
        # GIVEN both bypass patterns
        sys.path.insert(0, str(CLAUDE / "cli"))
        from agent_cli import hook as cli_hook
        hspec = importlib.util.spec_from_file_location("bead_hook", HOOK_DIR / "hook.py")
        bead_hook = importlib.util.module_from_spec(hspec)
        hspec.loader.exec_module(bead_hook)
        for fixture in BYPASS_FIXTURES:
            cmd = "git commit -m x" + fixture
            with self.subTest(cmd=cmd):
                # WHEN each judges the command
                # THEN they agree
                self.assertEqual(bead_hook.BYPASS.search(cmd) is not None,
                                 "bead-id-comment" in cli_hook.BYPASS.findall(cmd))

    def test_bypass_without_a_hash_is_not_a_bypass(self):
        """The bypass is a shell comment, as for every agent-cli rule; bare text in a message is not.

        Why: a commit message quoting the bypass words would otherwise switch the check off.
        """
        # WHEN the bypass words appear without a leading #
        r = self.repo.hook("git commit -m 'agent-cli: allow bead-id-comment'")
        # THEN the commit is still blocked
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_hooks_off_lets_the_commit_through(self):
        """AGENT_CLI_HOOKS=off turns every agent-cli rule off, this one included, as AGENTS.md says."""
        # WHEN git commit goes through the hook with hooks off
        r = self.repo.hook("git commit -m x", AGENT_CLI_HOOKS="off")
        # THEN exit 0
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_writes_no_bytecode_into_the_repo(self):
        """The check runs inside the repo being committed to; a __pycache__ there is an untracked mess.

        Why: importing agent_cli from that repo's .claude/cli would leave compiled files behind on
        every commit, which then show up in git status and trip stale-artifact gates.
        """
        # WHEN the hook runs a check that imports agent_cli
        self.repo.hook("git commit -m x")
        # THEN no __pycache__ exists under the repo's .claude/
        self.assertEqual(list((self.repo.root / ".claude").rglob("__pycache__")), [])

    def test_non_commit_commands_are_ignored(self):
        """Only git commit is gated; every other command runs untouched."""
        for cmd in ("git status", "git commit-tree HEAD^{tree} -m x"):
            with self.subTest(cmd=cmd):
                # WHEN the command goes through the hook
                r = self.repo.hook(cmd)
                # THEN exit 0, nothing said
                self.assertEqual((r.returncode, r.stderr), (0, ""))

    def test_commit_after_global_options_is_checked(self):
        """git -c / --no-pager before commit is still a commit; skipping it lets a violation through."""
        for cmd in ("git -c commit.gpgsign=false commit -m x", "git --no-pager commit -m x"):
            with self.subTest(cmd=cmd):
                # WHEN the commit goes through the hook
                r = self.repo.hook(cmd)
                # THEN exit 2 naming the file
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("new.py:2:", r.stderr)

    def test_git_dash_c_commit_from_another_dir_is_checked_in_that_repo(self):
        """git -C <repo> commit run from elsewhere must be judged by the repo it commits to."""
        # GIVEN a cwd outside the repo
        other = self.tmp / "elsewhere"
        other.mkdir()
        # WHEN git -C <repo> commit goes through the hook
        for cmd in (f"git -C {self.repo.root} commit -m x", f"git -C {self.repo.root} -c x.y=z commit -m x"):
            with self.subTest(cmd=cmd):
                r = self.repo.hook(cmd, cwd=other)
                # THEN exit 2 naming the file
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("new.py:2:", r.stderr)


if __name__ == "__main__":
    unittest.main()
