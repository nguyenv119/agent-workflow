"""Tests for the harness-edit-on-main rule: `agent-cli hook pretooluse` denies edits to harness files in a
checkout on the default branch (or detached, or outside git), run as a real subprocess on real git repos."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import GIT, agent_cli, bash_event, edit_event, make_project, run

RULE = "harness-edit-on-main"
SKILL = ".claude/skills/review/SKILL.md"


class HarnessCase(unittest.TestCase):
    """A project on main with one committed harness file and one plain file, plus a feature-branch worktree."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-harness-edit-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = make_project(self.tmp / "proj")
        (self.p / SKILL).parent.mkdir(parents=True)
        (self.p / SKILL).write_text("# review\n")
        (self.p / "src.py").write_text("x = 1\n")
        run(GIT + ["add", SKILL, "src.py"], self.p)
        run(GIT + ["commit", "-qm", "init"], self.p)
        self.bin = str(self.p / ".claude/bin")

    def worktree(self, branch="feature"):
        wt = self.tmp / f"wt-{branch}"
        run(GIT + ["worktree", "add", "-q", "-b", branch, str(wt)], self.p)
        return wt

    def hook(self, stdin, env=None):
        return agent_cli(self.p, "hook", "pretooluse", stdin=stdin,
                         env={"PATH": self.bin + os.pathsep + os.environ["PATH"], **(env or {})})

    def edit(self, path, tool="Edit", cwd=None, env=None):
        return self.hook(edit_event(path, tool, cwd or self.p), env)

    def bash(self, command, env=None):
        return self.hook(bash_event(command, cwd=self.p), env)

    def decision(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

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


if __name__ == "__main__":
    unittest.main()
