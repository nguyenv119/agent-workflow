"""Tests for the harness-edit-on-main rule: `agent-cli hook pretooluse` denies edits to harness files in a
checkout on the default branch (or detached, or outside git), run as a real subprocess on real git repos."""
import json
import sys
import time
import unittest

from helpers import GIT, HookCase, agent_cli, bash_event, edit_event, run

RULE = "harness-edit-on-main"
SKILL = ".claude/skills/review/SKILL.md"


class HarnessCase(HookCase):
    """A project on main with one committed harness file and one plain file, plus a feature-branch worktree."""

    def setUp(self):
        super().setUp()
        (self.p / SKILL).parent.mkdir(parents=True)
        (self.p / SKILL).write_text("# review\n")
        (self.p / "src.py").write_text("x = 1\n")
        run(GIT + ["add", SKILL, "src.py"], self.p)
        run(GIT + ["commit", "-qm", "init"], self.p)

    def worktree(self, branch="feature", wt=None):
        wt = wt or self.tmp / f"wt-{branch}"
        run(GIT + ["worktree", "add", "-q", "-b", branch, str(wt)], self.p)
        return wt

    def edit(self, path, tool="Edit", cwd=None, env=None):
        return self.hook(edit_event(path, tool, cwd or self.p), env)

    def bash(self, command, env=None, cwd=None):
        return self.hook(bash_event(command, cwd=cwd or self.p), env)

    def assert_denied(self, p, where="on main"):
        out = self.decision(p)
        self.assertIsNotNone(out, "expected a deny")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertTrue(out["permissionDecisionReason"].startswith(f"Harness file {where}: make a worktree"),
                        out["permissionDecisionReason"])
        return out["permissionDecisionReason"]


class EditTools(HarnessCase):
    def test_edit_of_a_harness_file_on_main_is_denied_naming_the_branch(self):
        """An Edit of a skill in a checkout on main is denied, and the reason names main and the worktree command.

        Harness files must change through a branch and a PR; an edit on main skips review and CI.
        """
        # GIVEN the main checkout's skill file
        # WHEN the hook reads an Edit of it
        reason = self.assert_denied(self.edit(self.p / SKILL))
        # THEN the reason names the replacement and the env bypass
        self.assertIn("wt new <bead-id> <slug> --base origin/main", reason)
        self.assertIn("AGENT_CLI_HOOKS=off", reason)

    def test_write_of_a_new_harness_file_on_main_is_denied(self):
        """A Write creating a harness file that does not exist yet is denied too.

        A new skill added on main is the same unreviewed change as an edit; the missing path must still resolve.
        """
        # GIVEN a path under a directory that does not exist yet
        new = self.p / ".claude/skills/brand-new/SKILL.md"
        # WHEN the hook reads a Write of it
        # THEN it is denied
        self.assert_denied(self.edit(new, tool="Write"))

    def test_multiedit_with_a_relative_path_resolves_against_the_session_cwd(self):
        """A relative file_path is resolved against the payload's cwd, not the hook's own cwd."""
        # GIVEN a relative path and the session cwd at the project
        # WHEN the hook reads a MultiEdit of it
        # THEN it is denied
        self.assert_denied(self.edit(SKILL, tool="MultiEdit"))

    def test_edit_in_a_feature_branch_worktree_is_allowed(self):
        """The same file inside a feature-branch worktree passes with no output.

        That is the path the deny points to; blocking it would leave no way to change the harness.
        """
        # GIVEN a feature-branch worktree
        wt = self.worktree()
        # WHEN the hook reads an Edit of its skill
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.edit(wt / SKILL, cwd=wt)))

    def test_edit_of_a_non_harness_file_on_main_is_allowed(self):
        """Files outside harness-paths stay writable on main (code, plans, loop logs)."""
        # GIVEN a plain source file in the main checkout
        # WHEN the hook reads an Edit of it
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.edit(self.p / "src.py")))

    def test_edit_through_a_symlink_into_the_main_checkout_is_denied(self):
        """A dir whose skills symlink into a main checkout resolves to that checkout and is denied.

        A laptop's user-level skills often link into a main checkout; the link must not hide the branch.
        """
        # GIVEN a non-git dir whose skills dir is a symlink to the main checkout's
        outside = self.tmp / "laptop"
        outside.mkdir()
        (outside / "skills").symlink_to(self.p / ".claude/skills")
        # WHEN the hook reads an Edit through the link
        # THEN it is denied as on main
        self.assert_denied(self.edit(outside / "skills/review/SKILL.md", cwd=outside))

    def test_detached_head_is_denied(self):
        """A checkout on a detached HEAD has no branch to open a PR from, so its harness edits are denied."""
        # GIVEN the main checkout detached
        run(GIT + ["checkout", "-q", "--detach"], self.p)
        # WHEN the hook reads an Edit of the skill
        # THEN it is denied as detached
        self.assert_denied(self.edit(self.p / SKILL), where="on a detached HEAD")

    def test_default_branch_comes_from_origin_head(self):
        """When origin/HEAD names trunk, an edit on trunk is denied and an edit on main is allowed.

        Repos whose default branch is not main must get the same protection, and main there is a branch.
        """
        # GIVEN origin/HEAD -> origin/trunk, a trunk worktree, and the project still on main
        run(GIT + ["update-ref", "refs/remotes/origin/trunk", "HEAD"], self.p)
        run(GIT + ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"], self.p)
        trunk = self.worktree("trunk")
        # WHEN the hook reads an Edit in each
        on_trunk, on_main = self.edit(trunk / SKILL, cwd=trunk), self.edit(self.p / SKILL)
        # THEN trunk is denied and main is allowed
        self.assert_denied(on_trunk, where="on trunk")
        self.assertIsNone(self.decision(on_main))

    def test_notebook_edit_uses_notebook_path(self):
        """NotebookEdit carries notebook_path, not file_path; a harness notebook on main is denied."""
        # GIVEN a NotebookEdit payload for a notebook under .claude/skills
        # WHEN the hook reads it
        # THEN it is denied
        self.assert_denied(self.edit(self.p / ".claude/skills/review/demo.ipynb", tool="NotebookEdit"))

    def test_read_tool_is_never_inspected(self):
        """A Read of a harness file is never denied: reads cannot change the harness."""
        # GIVEN a Read payload for the skill
        # WHEN the hook reads it
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.edit(self.p / SKILL, tool="Read")))

    def test_kill_switch_allows_the_edit(self):
        """AGENT_CLI_HOOKS=off lets the edit through: the documented escape for a broken hook."""
        # GIVEN the kill switch
        # WHEN the hook reads an Edit of the skill on main
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.edit(self.p / SKILL, env={"AGENT_CLI_HOOKS": "off"})))

    def test_project_rules_harness_paths_replace_the_defaults(self):
        """A `harness-paths:` line under `## harness` replaces the default globs.

        A project with its harness elsewhere must be able to say so, and then .claude/skills is not special.
        """
        # GIVEN project-rules.md naming docs/** only
        (self.p / ".claude/project-rules.md").write_text("## harness\nharness-paths: `docs/**`, ops.md\n")
        # WHEN the hook reads Edits of docs/a.md and of the skill
        docs, skill = self.edit(self.p / "docs/a.md"), self.edit(self.p / SKILL)
        # THEN docs is denied and the skill is allowed
        self.assert_denied(docs)
        self.assertIsNone(self.decision(skill))

    def test_git_missing_prints_nothing(self):
        """With git not on PATH the check fails open: no output, exit 0, never a deny on a guess."""
        # GIVEN a PATH with no git
        # WHEN the hook reads an Edit of the skill on main
        p = self.edit(self.p / SKILL, env={"PATH": str(self.tmp / "no-bin")})
        # THEN it prints nothing
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)


class OutsideGit(HarnessCase):
    def setUp(self):
        super().setUp()
        self.plain = self.tmp / "plain"
        (self.plain / ".claude").mkdir(parents=True)

    def test_harness_path_outside_git_is_denied(self):
        """A harness-relative path in a non-git project dir is denied: nothing there can become a PR."""
        # GIVEN CLAUDE_PROJECT_DIR at a non-git dir
        env = {"CLAUDE_PROJECT_DIR": str(self.plain)}
        # WHEN the hook reads a Write of its AGENTS.md
        # THEN it is denied as outside git
        self.assert_denied(self.edit(self.plain / "AGENTS.md", "Write", self.plain, env), where="outside git")

    def test_non_harness_path_outside_git_is_allowed(self):
        """A non-git file outside harness-paths stays writable (scratch notes, downloads)."""
        # GIVEN CLAUDE_PROJECT_DIR at a non-git dir
        env = {"CLAUDE_PROJECT_DIR": str(self.plain)}
        # WHEN the hook reads a Write of notes.txt there
        # THEN nothing is decided
        self.assertIsNone(self.decision(self.edit(self.plain / "notes.txt", "Write", self.plain, env)))


class BashWrites(HarnessCase):
    def test_append_redirect_to_a_harness_file_on_main_is_denied(self):
        """`echo x >> <skill>` on main is denied with the rule's bypass comment named.

        Shell writes are the easy way around the Edit check; leaving them open makes the rule decorative.
        """
        # GIVEN the main checkout
        # WHEN the hook reads the append
        reason = self.assert_denied(self.bash(f"echo x >> {SKILL}"))
        # THEN the bash bypass comment is named
        self.assertIn(f"# agent-cli: allow {RULE}", reason)

    def test_every_write_form_is_denied(self):
        """tee, tee -a, sed -i, cp, mv, ln and rm onto a harness path, and `>` / `2>`, are each denied."""
        # GIVEN the write forms
        cmds = [f"echo x | tee {SKILL}", f"echo x | tee -a {SKILL}", f"sed -i '' 's/a/b/' {SKILL}",
                f"cp /tmp/x {SKILL}", f"mv /tmp/x {SKILL}", f"ln -sf /tmp/x {SKILL}", f"rm -f {SKILL}",
                f"cat > {SKILL} <<'EOF'\nhi\nEOF", f"make 2> {SKILL}", f"mv {SKILL} /tmp/x"]
        for c in cmds:
            with self.subTest(cmd=c):
                # WHEN the hook reads it
                # THEN it is denied
                self.assert_denied(self.bash(c))

    def test_reads_and_quoted_mentions_are_allowed(self):
        """cat, cp from a harness file, quoted text and heredoc bodies naming the path all pass.

        Reads never change the harness; a commit message or note that mentions the path is not a write.
        """
        # GIVEN commands that mention the skill without writing it
        cmds = [f"cat {SKILL}", f"cp {SKILL} /tmp/copy.md", f'git commit -m "echo x >> {SKILL}"',
                f"cat > notes.txt <<'EOF'\necho x >> {SKILL}\nEOF", "make 2>&1 | tee build.log",
                f"grep -n '<promise>WIN</promise>' {SKILL}"]  # the kept sentinel token's `>` is no redirect
        for c in cmds:
            with self.subTest(cmd=c):
                # WHEN the hook reads it
                # THEN nothing is decided
                self.assertIsNone(self.decision(self.bash(c)))

    def test_removing_or_replacing_a_dir_holding_harness_files_is_denied(self):
        """`rm -rf .claude`, `mv .claude x` and copies onto `.` on main are denied like a write to a skill.

        A dir that holds harness files is itself a harness write target: deleting or overwriting it changes
        every file under it, so allowing it while `rm -rf .claude/skills` is denied leaves the rule decorative.
        """
        # GIVEN commands whose target is .claude or the checkout root
        cmds = ["rm -rf .claude", "mv .claude .claude.bak", "cp -r /tmp/other/.claude .claude",
                "cp -r /tmp/other/. ."]
        for c in cmds:
            with self.subTest(cmd=c):
                # WHEN the hook reads it on main
                # THEN it is denied
                self.assert_denied(self.bash(c))

    def test_dirs_that_hold_no_harness_files_stay_writable(self):
        """A non-harness dir under .claude on main, and `cp -r src/. .` in a feature worktree, both pass.

        Only an ancestor of a harness glob counts; .claude/loop-evals is a sibling, and a feature branch is allowed.
        """
        # GIVEN a feature worktree
        wt = self.worktree()
        # WHEN the hook reads rm of a non-harness dir on main, and a copy onto the worktree root
        rm, cp = self.bash("rm -rf .claude/loop-evals"), self.bash("cp -r src/. .", cwd=wt)
        # THEN nothing is decided for either
        self.assertIsNone(self.decision(rm))
        self.assertIsNone(self.decision(cp))

    def test_redirects_and_command_prefixes_do_not_hide_the_target(self):
        """A trailing `2>/dev/null`, `timeout N`, `sudo`, a command-prefixes seam word, or no space before
        `>>` still leaves the harness path as the write target.

        `cp x <skill> 2>/dev/null` once picked `2>/dev/null` as cp's destination, and `rm -rf 2>&1 .claude`
        once read `.claude` as the redirect's word; both allowed the write.
        """
        # GIVEN a project command-prefixes seam naming `wrapx`
        seam = self.p / ".claude/agent-cli-hooks/command-prefixes"
        seam.parent.mkdir(parents=True, exist_ok=True)
        seam.write_text("wrapx\n")
        cmds = [f"cp /tmp/x {SKILL} 2>/dev/null", f"ln -sf /tmp/x {SKILL} 2>/dev/null",
                "timeout 5 sed -i s/a/b/ AGENTS.md", f"echo x | wrapx tee {SKILL}", f"sudo tee {SKILL} </tmp/x",
                f"echo x>>{SKILL}", "rm -rf 2>&1 .claude", "tee 2>&1 AGENTS.md", f"cat /tmp/x >&2 >{SKILL}", f"echo x >& {SKILL}"]
        for c in cmds:
            with self.subTest(cmd=c):
                # WHEN the hook reads it on main
                # THEN it is denied
                self.assert_denied(self.bash(c))

    def test_many_operands_finish_fast_and_still_find_the_harness_path(self):
        """300 non-harness operands then AGENTS.md are denied well inside the hook's 5 s timeout.

        A hook timeout is a silent allow, so a long `rm -f` list must not cost one git call per operand.
        """
        # GIVEN an rm of 300 distinct plain files, AGENTS.md last
        cmd = "rm -f " + " ".join(f"d{i % 30}/f{i}.txt" for i in range(300)) + " AGENTS.md"
        # WHEN the hook reads it on main
        t0 = time.monotonic()
        p = self.bash(cmd)
        took = time.monotonic() - t0
        # THEN it is denied in under 2 s
        self.assert_denied(p)
        self.assertLess(took, 2.0)

    def test_hook_test_without_git_prints_its_normal_decision(self):
        """`hook test` with git missing from PATH exits 0 with an allow, no traceback.

        The harness check fails open when git cannot run; `hook test` must report that, not crash.
        """
        # GIVEN a PATH with no git
        env = {"PATH": str(self.tmp / "no-bin")}
        # WHEN a harness append is tested
        t = agent_cli(self.p, "hook", "test", "--cmd", "echo x >> AGENTS.md", env=env)
        # THEN it exits 0 with no traceback
        self.assertEqual(t.returncode, 0, t.stderr)
        self.assertNotIn("Traceback", t.stderr)

    def test_bypass_comment_and_kill_switch_allow_the_write(self):
        """`# agent-cli: allow harness-edit-on-main` and AGENT_CLI_HOOKS=off each let the write through."""
        # GIVEN the append with a bypass comment, and the plain append with the kill switch
        # WHEN the hook reads each
        bypassed = self.bash(f"echo x >> {SKILL}  # agent-cli: allow {RULE}")
        off = self.bash(f"echo x >> {SKILL}", env={"AGENT_CLI_HOOKS": "off"})
        # THEN neither is decided
        self.assertIsNone(self.decision(bypassed))
        self.assertIsNone(self.decision(off))

    def test_write_in_a_feature_worktree_is_allowed(self):
        """The same append run from a feature-branch worktree passes."""
        # GIVEN a feature worktree as the session cwd
        wt = self.worktree()
        # WHEN the hook reads the append there
        p = self.hook(bash_event(f"echo x >> {SKILL}", cwd=wt))
        # THEN nothing is decided
        self.assertIsNone(self.decision(p))

    def test_hook_test_and_rules_report_the_rule(self):
        """`hook test` denies with the rule id, and `hook rules` lists it as a builtin deny rule.

        AGENTS.md's table is checked against `hook rules`; an unlisted rule would be invisible to agents.
        """
        # GIVEN the main checkout
        # WHEN the append is tested and the rules are listed
        t = agent_cli(self.p, "hook", "test", "--cmd", f"echo x >> {SKILL}", "--json")
        rows = {d["id"]: d for d in map(json.loads, agent_cli(self.p, "hook", "rules", "--json").stdout.splitlines())}
        # THEN both name the rule
        self.assertEqual(t.returncode, 2, t.stderr)
        self.assertEqual([r["id"] for r in json.loads(t.stdout)["rules"]], [RULE])
        self.assertEqual((rows[RULE]["mode"], rows[RULE]["source"], rows[RULE]["active"]), ("deny", "builtin", True))


class BashCwd(HarnessCase):
    """cd/pushd move later relative targets; a dir above a checkout root counts as that checkout's harness."""

    def test_cd_into_the_main_checkout_judges_later_writes_there(self):
        """`cd <main> && echo x >> AGENTS.md` from a feature worktree is denied as on main.

        The append lands in the main checkout; judging it against the session's worktree let it through.
        """
        # GIVEN a feature worktree as the session cwd
        wt = self.worktree()
        # WHEN the hook reads a cd into main followed by the append
        p = self.bash(f"cd {self.p} && echo x >> AGENTS.md", cwd=wt)
        # THEN it is denied as on main
        self.assert_denied(p)

    def test_cd_into_a_feature_worktree_allows_later_writes(self):
        """`cd <worktree> && echo x >> AGENTS.md` from the main checkout passes.

        That is the documented way to edit the harness; denying it on the session cwd's branch blocks it.
        """
        # GIVEN a feature worktree and the session cwd at main
        wt = self.worktree()
        # WHEN the hook reads a cd into the worktree followed by the append
        p = self.bash(f"cd {wt} && echo x >> AGENTS.md")
        # THEN nothing is decided
        self.assertIsNone(self.decision(p))

    def test_cd_to_an_unreadable_dir_skips_later_relative_targets(self):
        """After `cd "$X"` (or `cd ~`, `cd -`, bare `cd`) a relative AGENTS.md is not judged: its checkout is
        unknown, so it is not guessed from the session cwd."""
        for c in ['cd "$X"', "cd ~/x", "cd -", "cd", "pushd $X"]:
            with self.subTest(cd=c):
                # GIVEN the session cwd at main
                # WHEN the hook reads an unreadable cd followed by a relative append
                p = self.bash(f"{c} && echo x >> AGENTS.md")
                # THEN nothing is decided
                self.assertIsNone(self.decision(p))

    def test_cd_to_an_unreadable_dir_still_judges_absolute_targets(self):
        """After `cd "$X"` an absolute path to main's AGENTS.md is still denied: no cd moves it."""
        # GIVEN the session cwd at main
        # WHEN the hook reads a cd to a variable dir followed by an absolute append
        p = self.bash(f'cd "$X" && echo x >> {self.p}/AGENTS.md')
        # THEN it is denied
        self.assert_denied(p)

    def test_a_later_absolute_cd_restores_a_known_dir(self):
        """`cd "$X" && cd <main> && echo x >> AGENTS.md` from a worktree is denied: the second cd is readable."""
        # GIVEN a feature worktree as the session cwd
        wt = self.worktree()
        # WHEN the hook reads an unreadable cd, then a cd into main, then the append
        p = self.bash(f'cd "$X" && cd {self.p} && echo x >> AGENTS.md', cwd=wt)
        # THEN it is denied as on main
        self.assert_denied(p)

    def test_relative_cd_chain_joins_onto_the_target(self):
        """`cd .claude && echo x >> skills/review/SKILL.md` and `pushd .claude; tee skills/...` on main are denied.

        The written file is .claude/skills/...; read against the session cwd it looked like a plain path.
        """
        for c in ["cd .claude && echo x >> skills/review/SKILL.md", "pushd .claude; cd skills; tee review/SKILL.md"]:
            with self.subTest(cmd=c):
                # GIVEN the session cwd at main
                # WHEN the hook reads it
                # THEN it is denied
                self.assert_denied(self.bash(c))

    def test_removing_the_dir_above_the_main_checkout_is_denied(self):
        """`rm -rf ..` from the main checkout root is denied: it deletes the checkout and its harness."""
        # GIVEN the session cwd at the main checkout root
        # WHEN the hook reads rm of its parent
        # THEN it is denied as on main
        self.assert_denied(self.bash("rm -rf .."))

    def test_removing_the_dir_above_a_lone_feature_worktree_is_allowed(self):
        """`rm -rf ..` from a feature worktree whose parent holds no main checkout passes.

        The worktree is nested one level (tmp/nest/wt) so its parent holds only it; the fixture's default
        worktree sits next to the main checkout, whose parent would also hold main.
        """
        # GIVEN a feature worktree alone under its parent dir
        wt = self.worktree(wt=self.tmp / "nest/wt")
        # WHEN the hook reads rm of its parent from there
        p = self.bash("rm -rf ..", cwd=wt)
        # THEN nothing is decided
        self.assertIsNone(self.decision(p))

    def test_removing_a_dir_above_the_project_dir_checkout_is_denied(self):
        """From a lone worktree, rm of a dir above the main checkout named by $CLAUDE_PROJECT_DIR is denied.

        The session's project checkout is the one an agent most likely deletes by accident.
        """
        # GIVEN a lone feature worktree as cwd and CLAUDE_PROJECT_DIR at main
        wt = self.worktree(wt=self.tmp / "nest/wt")
        env = {"CLAUDE_PROJECT_DIR": str(self.p)}
        # WHEN the hook reads rm of the dir holding main
        p = self.bash(f"rm -rf {self.tmp}", env=env, cwd=wt)
        # THEN it is denied as on main
        self.assert_denied(p)


@unittest.skipUnless(sys.platform == "darwin", "macOS file systems are case-insensitive by default")
class CaseFolding(HarnessCase):
    def test_case_variant_names_are_denied_on_main(self):
        """`echo x >> agents.md` and `rm -rf .CLAUDE` on main are denied.

        On a case-insensitive file system these write AGENTS.md and .claude; an exact-case compare let them by.
        """
        for c in ["echo x >> agents.md", "rm -rf .CLAUDE"]:
            with self.subTest(cmd=c):
                # GIVEN the session cwd at main
                # WHEN the hook reads it
                # THEN it is denied
                self.assert_denied(self.bash(c))


if __name__ == "__main__":
    unittest.main()
