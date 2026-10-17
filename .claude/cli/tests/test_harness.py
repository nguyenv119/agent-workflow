"""Tests for `agent-cli harness drift` and `agent-cli harness inventory`, on real files in temp dirs.

drift compares an install's .claude/HARNESS_FILES.tsv with the files on disk and sorts every difference into a
bucket; inventory lists what a session in a checkout loads and whether each hook's programs really exist.
"""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, TmpCase, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import harness  # noqa: E402

INSTALLER = CLAUDE.parent / "harness-loop/install_harness.py"
EMPTY = {"addon_edited": [], "base_edited": [], "missing": [], "owned": {}, "shadow": [], "unexplained": [],
         "unowned": []}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(root, addon=(), merged=()):
    """Write root/.claude/HARNESS_FILES.tsv as an install would: every file under .claude plus AGENTS.md, paths
    in addon marked `addon`, paths in merged marked `base+addon` (settings.json with settings.add.json merged in),
    the rest `base`."""
    root = Path(root)
    files = [p for p in (root / ".claude").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [root / "AGENTS.md"] if (root / "AGENTS.md").is_file() else []
    rows = sorted((p.relative_to(root).as_posix(), p) for p in files)
    (root / ".claude/HARNESS_FILES.tsv").write_text("path\torigin\tsha256\n" + "".join(
        f"{rel}\t{'addon' if rel in addon else 'base+addon' if rel in merged else 'base'}\t{sha(p)}\n"
        for rel, p in rows))


def write(root, rel, text):
    f = Path(root) / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


class Drift(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        write(self.p, "AGENTS.md", "agents\n")
        write(self.p, ".claude/skills/review/SKILL.md", "review v1\n")
        write(self.p, ".claude/project-rules.md", "rules\n")
        snapshot(self.p, addon={".claude/project-rules.md"})

    def drift(self, *args, env=None):
        r = agent_cli(self.p, "harness", "drift", *args, env={"PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
        return r, (json.loads(r.stdout) if "--json" in args and r.stdout.strip() else None)

    def test_untouched_install_reports_nothing_and_exits_0(self):
        """
        Right after an install nothing has drifted: every bucket is empty and the exit is 0. A false positive here
        would make a daily drift check cry wolf on every clean checkout.
        """
        # GIVEN a project exactly as installed
        # WHEN drift runs
        r, got = self.drift("--json")
        # THEN one JSON object, every bucket empty, exit 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        self.assertEqual(got, EMPTY)

    def test_edited_base_file_is_a_port_candidate_and_still_exits_0(self):
        """
        An edit to a base file is a candidate improvement to send upstream (`loop.py port`), not a mistake, so it
        is listed as base_edited and the exit stays 0; the text output says how to port it.
        """
        # GIVEN an edited base skill
        write(self.p, ".claude/skills/review/SKILL.md", "review v2\n")
        # WHEN drift runs, with and without --json
        r, got = self.drift("--json")
        text = self.drift()[0].stdout
        # THEN it is base_edited only, exit 0, with the port fix line
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got, {**EMPTY, "base_edited": [".claude/skills/review/SKILL.md"]})
        self.assertIn("base_edited: .claude/skills/review/SKILL.md", text)
        self.assertIn("fix: port upstream: loop.py port", text)

    def test_edited_addon_file_is_a_mistake_naming_the_addon_source(self):
        """
        An installed add-on file is a copy; editing it is lost on the next reinstall. drift lists it as addon_edited,
        exits 1 and names the add-on source file to edit instead (default `.claude/harness-addon` when
        HARNESS_VERSION has no add-on).
        """
        # GIVEN an edited add-on file and a HARNESS_VERSION naming the add-on
        write(self.p, ".claude/project-rules.md", "rules edited in place\n")
        write(self.p, ".claude/HARNESS_VERSION", json.dumps({"commit": "x", "addon": "addons/mine",
                                                             "addon_sha256": "y"}))
        # WHEN drift runs
        r, got = self.drift("--json")
        text = self.drift()[0].stdout
        # THEN addon_edited, exit 1, and the fix names <addon>/project-rules.md
        self.assertEqual(r.returncode, 1)
        self.assertEqual(got, {**EMPTY, "addon_edited": [".claude/project-rules.md"]})
        self.assertIn("fix: edit addons/mine/project-rules.md instead, then reinstall", text)

    def test_addon_fix_defaults_to_harness_addon_without_a_version_file(self):
        """With no HARNESS_VERSION addon, the fix line points at the conventional `.claude/harness-addon`."""
        # GIVEN an edited add-on file and no HARNESS_VERSION
        write(self.p, ".claude/project-rules.md", "edited\n")
        # WHEN drift runs as text
        text = self.drift()[0].stdout
        # THEN the default add-on dir is named
        self.assertIn("fix: edit .claude/harness-addon/project-rules.md instead, then reinstall", text)

    def test_deleted_installed_file_is_missing_and_exits_1(self):
        """
        A deleted installed file means the session runs without part of the harness; drift lists it as missing,
        exits 1 and says to reinstall.
        """
        # GIVEN an installed file removed
        (self.p / "AGENTS.md").unlink()
        # WHEN drift runs
        r, got = self.drift("--json")
        text = self.drift()[0].stdout
        # THEN missing, exit 1, reinstall fix
        self.assertEqual(r.returncode, 1)
        self.assertEqual(got, {**EMPTY, "missing": ["AGENTS.md"]})
        self.assertIn("fix: reinstall with install_harness.py --force", text)

    def test_stray_file_is_unowned_until_an_owner_line_covers_it(self):
        """
        A file nobody installed and nobody claims (e.g. a skill an agent wrote straight into .claude) is invisible
        to upgrades and ports. drift reports it unowned (exit 1) until harness-owners.txt names an owner; then it
        is counted under that owner and the exit is 0.
        """
        # GIVEN a stray skill
        write(self.p, ".claude/skills/stray/SKILL.md", "stray\n")
        # WHEN drift runs without an owner line
        r, got = self.drift("--json")
        # THEN it is unowned, exit 1, with the owner fix line in text
        self.assertEqual(r.returncode, 1)
        self.assertEqual(got, {**EMPTY, "unowned": [".claude/skills/stray/SKILL.md"]})
        self.assertIn("fix: add an owner line to .claude/harness-owners.txt", self.drift()[0].stdout)
        # WHEN an owner line covers it
        write(self.p, ".claude/harness-owners.txt", '# owners\n\n.claude/skills/stray/** project "ours"\n')
        r, got = self.drift("--json")
        # THEN it is owned by project and the exit is 0 (the owner file itself is unowned unless installed)
        self.assertEqual(got["unowned"], [".claude/harness-owners.txt"])
        write(self.p, ".claude/harness-owners.txt",
              '.claude/skills/stray/** project "ours"\n.claude/harness-owners.txt project x\n')
        r, got = self.drift("--json")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(got, {**EMPTY, "owned": {"project": 2}})

    def test_double_star_matches_any_depth_and_a_prefix_glob_prunes_its_dir(self):
        """
        `**/__pycache__/**` must match bytecode at any depth, and a `<prefix>/**` owner stops the walk at that dir
        (a data dir can hold thousands of files), counting it once. Without pruning a big data dir makes drift
        slow; without `**` every nested cache dir would need its own line.
        """
        # GIVEN bytecode at two depths and a data dir of three files
        write(self.p, ".claude/__pycache__/a.pyc", "x")
        write(self.p, ".claude/cli/agent_cli/__pycache__/b.pyc", "x")
        for i in range(3):
            write(self.p, f".claude/discover-kb/n{i}.md", "note")
        write(self.p, ".claude/harness-owners.txt", "**/__pycache__/** local-data bytecode\n"
              ".claude/discover-kb/** local-data notes\n.claude/harness-owners.txt project owners\n")
        # WHEN drift runs
        r, got = self.drift("--json")
        # THEN both pycache dirs and the data dir are owned, the data dir counted once
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(got["owned"], {"local-data": 3, "project": 1})

    def test_glob_segments(self):
        """`**` spans zero or more whole segments; `*` and `?` never cross a `/`."""
        # GIVEN/WHEN/THEN each (glob, path) pair matches as documented
        cases = [("**/x/**", "a/b/x/c", True), ("**/x/**", "x/c", True), ("**/x/**", "ax/c", False),
                 ("a/*", "a/b", True), ("a/*", "a/b/c", False), ("a/?", "a/bc", False),
                 (".claude/.env*", ".claude/.env.local", True), ("skills/document", "skills/document", True),
                 ("a/**", "a", True), ("a/**/z", "a/b/c/z", True)]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(harness.glob_match(pattern, path), want)

    def test_malformed_owner_line_exits_2_naming_file_and_line(self):
        """
        A typo in harness-owners.txt must fail loudly with its line number: silently skipping it would report
        the files it meant to claim as unowned, or worse hide a bad owner name.
        """
        for text, why in (("ok/** project fine\n.claude/x project\n", "line 2"),
                          ("# c\n.claude/x boss reason\n", "line 2")):
            with self.subTest(text=text):
                # GIVEN a malformed line
                write(self.p, ".claude/harness-owners.txt", text)
                # WHEN drift runs
                r, _ = self.drift("--json")
                # THEN exit 2 naming the file and line
                self.assertEqual(r.returncode, 2)
                self.assertIn("harness-owners.txt", r.stderr)
                self.assertIn(why, r.stderr)

    def test_unknown_owner_is_named_in_the_error(self):
        """The error for an unknown owner lists the four allowed owners."""
        # GIVEN an owner line with owner `boss`
        write(self.p, ".claude/harness-owners.txt", ".claude/x boss reason\n")
        # WHEN drift runs
        r, _ = self.drift()
        # THEN the allowed owners are listed
        self.assertEqual(r.returncode, 2)
        self.assertIn("local-data, user, addon-source, project", r.stderr)

    def test_symlink_under_claude_is_one_entry_and_not_followed(self):
        """
        A symlinked dir (e.g. a skill linked into a nested repo) is reported once at its own path; following it
        would list every file of another repo as this install's drift.
        """
        # GIVEN .claude/skills/linked -> a dir with two files
        other = self.tmp / "other"
        write(other, "SKILL.md", "a")
        write(other, "more.md", "b")
        (self.p / ".claude/skills/linked").symlink_to(other)
        # WHEN drift runs
        r, got = self.drift("--json")
        # THEN exactly the link is unowned
        self.assertEqual(got["unowned"], [".claude/skills/linked"])

    def test_no_files_tsv_exits_3_saying_to_reinstall(self):
        """A checkout installed before HARNESS_FILES.tsv existed cannot be checked; exit 3 says how to fix it."""
        # GIVEN no TSV
        (self.p / ".claude/HARNESS_FILES.tsv").unlink()
        # WHEN drift runs
        r, _ = self.drift()
        # THEN exit 3 naming the installer
        self.assertEqual(r.returncode, 3)
        self.assertIn("install_harness.py", r.stderr)

    def test_target_may_be_the_project_root_or_its_claude_dir(self):
        """`--target <repo>` and `--target <repo>/.claude` check the same install (a later check passes the latter)."""
        # GIVEN an edited base file
        write(self.p, ".claude/skills/review/SKILL.md", "v2\n")
        # WHEN drift runs from elsewhere with each target form
        outs = [agent_cli(self.p, "harness", "drift", "--target", str(t), "--json", cwd=self.tmp)
                for t in (self.p, self.p / ".claude")]
        # THEN both give the same answer
        self.assertEqual(outs[0].stdout, outs[1].stdout)
        self.assertEqual(json.loads(outs[0].stdout)["base_edited"], [".claude/skills/review/SKILL.md"])

    def test_malformed_files_tsv_row_exits_2(self):
        """A TSV row with a bad origin cannot be bucketed (the origin decides port vs mistake), so it refuses."""
        f = self.p / ".claude/HARNESS_FILES.tsv"
        good = f.read_text()
        for origin in ("other", "addon+base"):
            with self.subTest(origin=origin):
                # GIVEN a row whose origin is none of base, addon, base+addon
                f.write_text(good + f"x.md\t{origin}\tabc\n")
                # WHEN drift runs / THEN exit 2 naming the line
                r, _ = self.drift()
                self.assertEqual(r.returncode, 2)
                self.assertIn("is not `path<TAB>base|addon|base+addon<TAB>sha256`", r.stderr)

    def install_merged_settings(self):
        """Re-snapshot the project with a settings.json whose row is `base+addon`."""
        write(self.p, ".claude/settings.json", '{"hooks": {}}\n')
        (self.p / ".claude/HARNESS_FILES.tsv").unlink()
        snapshot(self.p, addon={".claude/project-rules.md"}, merged={".claude/settings.json"})

    def test_edited_merged_settings_is_addon_edited_never_base_edited(self):
        """
        settings.json with the add-on's settings.add.json merged in carries project hooks, so an edit to it must
        never be a port candidate (`loop.py port` copies base_edited files into a public PR). It is addon_edited,
        exit 1, and the fix says to edit settings.add.json (or .skipbase) instead.
        """
        # GIVEN an install whose settings.json row is base+addon, then the file edited in place
        self.install_merged_settings()
        write(self.p, ".claude/settings.json", '{"hooks": {}, "x": 1}\n')
        # WHEN drift runs, with and without --json
        r, got = self.drift("--json")
        text = self.drift()[0].stdout
        # THEN addon_edited only, base_edited empty, exit 1, with the settings.add.json fix line
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(got, {**EMPTY, "addon_edited": [".claude/settings.json"]})
        self.assertIn("addon_edited: .claude/settings.json\n"
                      "  fix: edit the add-on's settings.add.json (or .skipbase) instead, then reinstall\n", text)
        self.assertNotIn("port upstream", text)

    def test_unedited_merged_settings_reports_nothing(self):
        """A `base+addon` row whose file still matches its hash is clean, like any other row."""
        # GIVEN an install whose settings.json row is base+addon, untouched
        self.install_merged_settings()
        # WHEN drift runs / THEN every bucket is empty, exit 0
        r, got = self.drift("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got, EMPTY)

    def test_user_dir_that_is_not_a_directory_exits_2(self):
        """A mistyped --user-dir must not silently report no shadows."""
        # GIVEN/WHEN drift with a missing user dir
        r, _ = self.drift("--user-dir", str(self.tmp / "nope"))
        # THEN exit 2
        self.assertEqual(r.returncode, 2)
        self.assertIn("--user-dir", r.stderr)

    def test_target_that_is_not_a_directory_exits_2(self):
        """A wrong --target is a usage error with an example, never a traceback."""
        # GIVEN a path that does not exist
        # WHEN drift runs on it
        r, _ = self.drift("--target", str(self.tmp / "nope"))
        # THEN exit 2 with an example
        self.assertEqual(r.returncode, 2)
        self.assertIn("example: agent-cli harness drift", r.stderr)

    def test_user_skill_shadowing_a_project_skill_is_unexplained_until_owned(self):
        """
        A user-level skill or command with a project one's name replaces it in every session on that machine, so
        a laptop and a cloud session run different harnesses. drift lists each shadow; one no owner line explains
        is also unexplained (exit 1). A user line explains it (exit 0). A user skill with no project twin is no
        shadow.
        """
        # GIVEN a user dir shadowing the review skill and a project command, plus an unrelated user skill
        user = self.tmp / "user"
        write(user, "skills/review/SKILL.md", "my review\n")
        write(user, "skills/solo/SKILL.md", "solo\n")
        write(user, "commands/plan.md", "my plan\n")
        write(self.p, ".claude/commands/plan.md", "plan\n")
        snapshot(self.p, addon={".claude/project-rules.md"})
        # WHEN drift runs with --user-dir
        r, got = self.drift("--user-dir", str(user), "--json")
        # THEN both shadows are listed and unexplained, exit 1
        self.assertEqual(r.returncode, 1)
        self.assertEqual(got["shadow"], ["commands/plan.md", "skills/review"])
        self.assertEqual(got["unexplained"], ["commands/plan.md", "skills/review"])
        self.assertIn("fix: add a `user` owner line or remove the user copy", self.drift("--user-dir", str(user))[0].stdout)
        # WHEN owner lines explain them
        write(self.p, ".claude/harness-owners.txt",
              'skills/review user "newer copy"\ncommands/* user mine\n.claude/harness-owners.txt project x\n')
        r, got = self.drift("--user-dir", str(user), "--json")
        # THEN they stay shadows but are explained, exit 0
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(got["shadow"], ["commands/plan.md", "skills/review"])
        self.assertEqual(got["unexplained"], [])

    def test_json_is_deterministic(self):
        """Two runs over the same tree print byte-identical JSON with sorted keys, so outputs can be diffed."""
        # GIVEN several drifts
        write(self.p, ".claude/skills/zz/SKILL.md", "z")
        write(self.p, ".claude/skills/aa/SKILL.md", "a")
        write(self.p, ".claude/skills/review/SKILL.md", "v2\n")
        # WHEN drift runs twice
        a, b = self.drift("--json")[0].stdout, self.drift("--json")[0].stdout
        # THEN identical, keys sorted, lists sorted
        self.assertEqual(a, b)
        got = json.loads(a)
        self.assertEqual(list(got), sorted(got))
        self.assertEqual(got["unowned"], [".claude/skills/aa/SKILL.md", ".claude/skills/zz/SKILL.md"])


class DriftAfterRealInstall(TmpCase):
    def test_real_install_then_three_edits_land_in_their_buckets(self):
        """
        End to end with the real installer: install a harness holding agent-cli with an add-on inside the target,
        then edit one base file, one add-on file and add a stray skill. drift (run with the installed agent-cli)
        puts each in its bucket and exits 1; reverting the add-on edit and the stray leaves base_edited, exit 0.
        """
        # GIVEN a harness repo holding agent-cli, a skill and AGENTS.md, installed with an add-on inside the target
        repo = make_project(self.tmp / "aw")
        write(repo, ".claude/skills/review/SKILL.md", "review\n")
        write(repo, "AGENTS.md", "agents\n")
        run(GIT + ["add", "-A"], repo)
        run(GIT + ["commit", "-qm", "c"], repo)
        target = self.tmp / "t"
        addon = target / ".claude/harness-addon"
        write(addon, "project-rules.md", "rules\n")
        write(addon, "harness-owners.txt", ".claude/harness-addon/** addon-source the add-on\n")
        subprocess.run([sys.executable, str(INSTALLER), "--repo", str(repo), "--commit", "HEAD",
                        "--target", str(target), "--addon", str(addon)], check=True, capture_output=True)
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        # WHEN one base file, one add-on file change and a stray skill appears
        write(target, ".claude/skills/review/SKILL.md", "review edited\n")
        write(target, ".claude/project-rules.md", "rules edited\n")
        write(target, ".claude/skills/stray/SKILL.md", "stray\n")
        r = agent_cli(target, "harness", "drift", "--json", env=env)
        # THEN each is in its bucket and the exit is 1
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout), {**EMPTY, "base_edited": [".claude/skills/review/SKILL.md"],
                                                "addon_edited": [".claude/project-rules.md"],
                                                "unowned": [".claude/skills/stray/SKILL.md"],
                                                "owned": {"addon-source": 1}})
        # WHEN the add-on edit and the stray are reverted
        write(target, ".claude/project-rules.md", "rules\n")
        (target / ".claude/skills/stray/SKILL.md").unlink()
        (target / ".claude/skills/stray").rmdir()
        r = agent_cli(target, "harness", "drift", "--json", env=env)
        # THEN only base_edited is left and the exit is 0
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(json.loads(r.stdout)["base_edited"], [".claude/skills/review/SKILL.md"])

    def test_real_install_with_settings_add_then_edited_settings_is_addon_edited(self):
        """
        End to end with the real installer: the TSV row the installer writes for a settings.json merged with
        settings.add.json (`base+addon`) must be what drift sorts into addon_edited. Each half is pinned by a
        literal in its own suite; this proves the two literals meet, so a merged project hook can never be
        reported as base drift and ported upstream.
        """
        # GIVEN a harness with a base settings.json, installed with an add-on whose settings.add.json adds a hook group
        repo = make_project(self.tmp / "aw")
        write(repo, ".claude/settings.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                         "command": "base"}]}]}}))
        write(repo, "AGENTS.md", "agents\n")
        run(GIT + ["add", "-A"], repo)
        run(GIT + ["commit", "-qm", "c"], repo)
        target = self.tmp / "t"
        addon = target / ".claude/harness-addon"
        write(addon, "settings.add.json", json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                      "command": "mine"}]}]}}))
        write(addon, "harness-owners.txt", ".claude/harness-addon/** addon-source the add-on\n")
        subprocess.run([sys.executable, str(INSTALLER), "--repo", str(repo), "--commit", "HEAD",
                        "--target", str(target), "--addon", str(addon)], check=True, capture_output=True)
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        # WHEN the installed settings.json is hand-edited
        write(target, ".claude/settings.json", "{}\n")
        r = agent_cli(target, "harness", "drift", "--json", env=env)
        # THEN it is addon_edited, never base_edited, exit 1
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(json.loads(r.stdout), {**EMPTY, "addon_edited": [".claude/settings.json"],
                                                "owned": {"addon-source": 1}})


class Inventory(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        tool = self.bin / "tool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)

    def inventory(self, *args, target=None, env=None):
        e = {"PATH": str(self.bin), "HOME": str(self.tmp / "home"), **(env or {})}
        r = agent_cli(self.p, "harness", "inventory", "--target", str(target or self.p), *args, env=e)
        return r, (json.loads(r.stdout) if "--json" in args and r.stdout.strip() else None)

    def settings(self, *commands):
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": c}
                                                               for c in commands]}]}
        write(self.p, ".claude/settings.json", json.dumps({"hooks": hooks}))

    def test_lists_skill_command_and_agent_names(self):
        """
        Two checkouts are compared by the names a session loads: skills by their frontmatter name (dir name when
        none), commands by path with nested dirs joined by `:`, agents by frontmatter name (file stem when none).
        """
        # GIVEN skills, nested commands and agents
        write(self.p, ".claude/skills/rev/SKILL.md", "---\nname: review\ndescription: x\n---\nbody\n")
        write(self.p, ".claude/skills/plain/SKILL.md", "no frontmatter\n")
        write(self.p, ".claude/skills/notaskill/README.md", "x")
        write(self.p, ".claude/commands/plan.md", "x")
        write(self.p, ".claude/commands/team/sync.md", "x")
        write(self.p, ".claude/agents/impl.md", "---\nname: \"implementer\"\n---\n")
        write(self.p, ".claude/agents/judge.md", "x")
        # WHEN inventory runs
        r, got = self.inventory("--json")
        # THEN the sorted names
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got["skills"], ["plain", "review"])
        self.assertEqual(got["commands"], ["plan", "team:sync"])
        self.assertEqual(got["agents"], ["implementer", "judge"])
        self.assertEqual(got["hooks"], [])

    def test_instruction_files_are_hashed_and_missing_ones_null(self):
        """The instruction files a session reads are fingerprinted, so a differing AGENTS.md shows up; absent = null."""
        # GIVEN AGENTS.md and .claude/CLAUDE.md but no root CLAUDE.md
        a = write(self.p, "AGENTS.md", "agents\n")
        c = write(self.p, ".claude/CLAUDE.md", "inner\n")
        # WHEN inventory runs
        _, got = self.inventory("--json")
        # THEN their sha256 and a null
        self.assertEqual(got["instructions"], {".claude/CLAUDE.md": sha(c), "AGENTS.md": sha(a), "CLAUDE.md": None})

    def test_hook_programs_resolve_to_ok_inert_or_unresolved(self):
        """
        A hook whose program is missing does nothing, silently. inventory resolves every program a hook runs (not
        just the first word): a missing `[ -x X ]` guard target makes it inert, an unguarded program not on PATH
        makes it unresolved, and assignments and $CLAUDE_PROJECT_DIR expand. Any non-ok hook exits 1.
        """
        # GIVEN hooks: ok via PATH, ok via guard+expansion, inert (missing guard), unresolved (not on PATH),
        # unresolved (unknown variable)
        write(self.p, ".claude/hooks/x.sh", "#!/bin/sh\n").chmod(0o755)
        ok_path = "tool --flag | cat; exit 0"
        ok_guard = 'D="$CLAUDE_PROJECT_DIR/.claude"; [ -f "$D/hooks/x.sh" ] && "${D}/hooks/x.sh" 2>/dev/null; exit 0'
        inert = '[ -x "$CLAUDE_PROJECT_DIR/.claude/bin/gone" ] && "$CLAUDE_PROJECT_DIR/.claude/bin/gone" run; exit 0'
        unresolved = "echo hi && nosuchprog --x"
        unknown_var = "$NOPE/bin/run"
        unknown_guard = 'X=$NOPE/x; [ -x "$X" ] && tool'
        # the production shape: an executable guard target, and the `test -f` spelling of the same guard
        ok_x_guard = '[ -x "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh" ] && "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh"; exit 0'
        ok_test_f = 'test -f "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh" && tool'
        inert_test_f = 'test -f "$CLAUDE_PROJECT_DIR/.claude/hooks/gone.sh" && tool'
        self.settings(ok_path, ok_guard, inert, unresolved, unknown_var, unknown_guard, ok_x_guard, ok_test_f,
                      inert_test_f)
        # WHEN inventory runs with PATH holding only `tool`
        r, got = self.inventory("--json")
        # THEN each hook gets its status and resolved programs, and the exit is 1
        self.assertEqual(r.returncode, 1)
        by_cmd = {h["command"]: h for h in got["hooks"]}
        self.assertEqual(by_cmd[ok_path]["status"], "ok")
        self.assertEqual(by_cmd[ok_path]["programs"], [{"word": "tool", "path": str(self.bin / "tool")}])
        self.assertEqual(by_cmd[ok_guard]["status"], "ok")
        self.assertEqual(by_cmd[ok_guard]["programs"],
                         [{"word": "$D/hooks/x.sh", "path": str(self.p / ".claude/hooks/x.sh")},
                          {"word": "${D}/hooks/x.sh", "path": str(self.p / ".claude/hooks/x.sh")}])
        self.assertEqual(by_cmd[inert]["status"], "inert")
        self.assertEqual(by_cmd[unresolved]["status"], "unresolved")
        self.assertEqual(by_cmd[unresolved]["programs"], [{"word": "nosuchprog", "path": None}])
        self.assertEqual(by_cmd[unknown_var]["status"], "unresolved")
        self.assertEqual(by_cmd[unknown_guard]["status"], "unresolved")
        self.assertEqual(by_cmd[inert]["event"], "PreToolUse")
        self.assertEqual(by_cmd[inert]["matcher"], "Bash")
        self.assertEqual(by_cmd[ok_x_guard]["status"], "ok")
        self.assertEqual(by_cmd[ok_x_guard]["programs"],
                         [{"word": "$CLAUDE_PROJECT_DIR/.claude/hooks/x.sh", "path": str(self.p / ".claude/hooks/x.sh")}])
        self.assertEqual(by_cmd[ok_test_f]["status"], "ok")
        self.assertEqual(by_cmd[inert_test_f]["status"], "inert")

    def test_dangling_skill_symlink_falls_back_to_its_dir_name(self):
        """
        A skill dir linked to a target that went away is still one loadable-looking entry; inventory names it by
        its dir and goes on instead of crashing, so one broken link cannot hide the rest of the inventory.
        """
        # GIVEN a skill dir whose SKILL.md is a dangling symlink
        d = self.p / ".claude/skills/linked"
        d.mkdir(parents=True)
        (d / "SKILL.md").symlink_to(self.tmp / "nowhere/SKILL.md")
        # WHEN inventory runs
        r, got = self.inventory("--json")
        # THEN the dir name stands in and the command exits 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got["skills"], ["linked"])

    def test_all_ok_hooks_exit_0_and_home_expands(self):
        """Every hook resolving is exit 0; `~` and $HOME expand to the HOME the hook would see."""
        # GIVEN a hook running ~/bin/x and one guarded on $HOME/bin/x
        write(self.tmp / "home", "bin/x", "#!/bin/sh\n").chmod(0o755)
        self.settings("~/bin/x", '[ -e "$HOME/bin/x" ] && tool')
        # WHEN inventory runs
        r, got = self.inventory("--json")
        # THEN both ok, exit 0
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([h["status"] for h in got["hooks"]], ["ok", "ok"])

    def test_hooks_are_sorted_by_event_matcher_and_command(self):
        """Hook rows sort by (event, matcher or "", command) so two inventories diff cleanly."""
        # GIVEN hooks across events in a scrambled order, one with no matcher
        write(self.p, ".claude/settings.json", json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "tool b"}]}],
            "PreToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "tool z"}]},
                           {"hooks": [{"type": "command", "command": "tool y"}]}]}}))
        # WHEN inventory runs twice
        r, got = self.inventory("--json")
        again = self.inventory("--json")[0].stdout
        # THEN sorted rows, a null matcher, identical output
        self.assertEqual([(h["event"], h["matcher"], h["command"]) for h in got["hooks"]],
                         [("PreToolUse", None, "tool y"), ("PreToolUse", "Edit", "tool z"), ("Stop", None, "tool b")])
        self.assertEqual(r.stdout, again)
        self.assertEqual(list(got), sorted(got))

    def test_invalid_settings_reports_error_and_exits_1(self):
        """A settings.json Claude Code cannot parse loads no hooks at all; that is a failure, not an empty list."""
        # GIVEN broken JSON
        write(self.p, ".claude/settings.json", "{not json")
        # WHEN inventory runs
        r, got = self.inventory("--json")
        # THEN an error field and exit 1
        self.assertEqual(r.returncode, 1)
        self.assertIn("settings.json", got["error"])

    def test_target_may_be_a_claude_dir_or_a_home_dir(self):
        """
        `--target <x>/.claude` reads that dir, and `--target $HOME` reads ~/.claude (the user layer), so the user
        layer and a project can be listed with the same command.
        """
        # GIVEN a home dir with a user skill
        home = self.tmp / "home"
        write(home, ".claude/skills/mine/SKILL.md", "---\nname: mine\n---\n")
        # WHEN inventory runs on the home dir and on its .claude
        a = self.inventory("--json", target=home)
        b = self.inventory("--json", target=home / ".claude")
        # THEN both list the user skill
        self.assertEqual(a[1]["skills"], ["mine"])
        self.assertEqual(a[0].stdout, b[0].stdout)

    def test_text_output_is_key_value_lines(self):
        """Without --json each field prints as `key: value` lines, one per hook with its status."""
        # GIVEN one skill and one unresolved hook
        write(self.p, ".claude/skills/a/SKILL.md", "x")
        self.settings("nosuchprog")
        # WHEN inventory runs as text
        r, _ = self.inventory()
        # THEN key: value lines name the skill and the hook's status
        self.assertIn("skills: a", r.stdout)
        self.assertIn("hook: unresolved PreToolUse Bash nosuchprog", r.stdout)

    def test_target_that_is_not_a_directory_exits_2(self):
        """A wrong --target is a usage error with an example."""
        # GIVEN/WHEN inventory on a missing dir
        r, _ = self.inventory(target=self.tmp / "nope")
        # THEN exit 2
        self.assertEqual(r.returncode, 2)
        self.assertIn("example: agent-cli harness inventory", r.stderr)


if __name__ == "__main__":
    unittest.main()
