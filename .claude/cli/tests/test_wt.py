"""Tests for `agent-cli wt new|list|audit|rm` and the hand-worktree-add hook rules, in a temp repo with a bare origin."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import wt  # noqa: E402

SETUP = '#!/bin/sh\necho "$@" > "$1/.setup-ran"\necho setup-streamed\n'


class WtCase(unittest.TestCase):
    """A project at tmp/proj with one commit pushed to a bare tmp/origin.git."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-wt-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        run(GIT + ["init", "-q", "--bare", str(self.tmp / "origin.git")], self.tmp)
        self.p = make_project(self.tmp / "proj")
        run(GIT + ["add", "-A"], self.p)
        run(GIT + ["commit", "-qm", "init"], self.p)
        run(GIT + ["remote", "add", "origin", str(self.tmp / "origin.git")], self.p)
        run(GIT + ["push", "-q", "-u", "origin", "main"], self.p)
        self.path = self.tmp / "proj-bd-42-add-user-login"
        self.branch = "feature/bd-42-add-user-login"

    def wt(self, *args, cwd=None, env=None):
        return agent_cli(self.p, "wt", *args, cwd=cwd, env=env)

    def seam(self, text):
        f = self.p / ".claude/agent-cli-hooks/wt-setup"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        f.chmod(0o755)
        return f

    def new(self, *extra):
        r = self.wt("new", "42", "add-user-login", *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def commit_in(self, path, name, date=None):
        (Path(path) / name).write_text(name)
        run(GIT + ["add", name], path)
        env = {**os.environ, **({"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {})}
        subprocess.run(GIT + ["commit", "-qm", name], cwd=path, env=env, check=True, capture_output=True)

    def worktree_paths(self):
        out = run(["git", "worktree", "list", "--porcelain"], self.p).stdout
        return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]

    def has_branch(self, b):
        return run(["git", "show-ref", "--verify", "-q", f"refs/heads/{b}"], self.p, check=False).returncode == 0


class New(WtCase):
    def test_new_creates_the_worktree_on_a_bead_branch(self):
        """`wt new <id> <slug> --json` adds <main>/../<main>-bd-<id>-<slug> on feature/bd-<id>-<slug> from origin/main.

        The naming is the coordinator's, so worktrees made by hand before this command are recognised.
        """
        # GIVEN a project with origin/main
        # WHEN a worktree is created
        r = self.new("--base", "origin/main", "--json")
        out = json.loads(r.stdout)
        # THEN git lists it, on the bead branch, and the JSON says so
        self.assertIn(str(self.path), self.worktree_paths())
        self.assertEqual(out["path"], str(self.path))
        self.assertEqual(out["branch"], self.branch)
        self.assertEqual((out["base"], out["created"], out["setup"]), ("origin/main", True, "none"))
        head = run(["git", "rev-parse", "--short", "HEAD"], self.path).stdout.strip()
        self.assertEqual(out["head"], head)
        self.assertEqual(run(["git", "branch", "--show-current"], self.path).stdout.strip(), self.branch)

    def test_second_new_prints_exists_and_exits_0(self):
        """R4: rerunning `wt new` for the same bead prints `exists: <path>` and succeeds, creating nothing."""
        # GIVEN an existing worktree for the bead
        self.new()
        # WHEN it is asked for again
        r = self.new()
        # THEN it reports exists and git still has exactly two worktrees
        self.assertEqual(r.stdout, f"exists: {self.path}\n")
        self.assertEqual(len(self.worktree_paths()), 2)

    def test_setup_seam_runs_with_path_bead_and_slug(self):
        """The project's wt-setup seam runs as `wt-setup <path> <bead-id> <slug>` after the worktree exists.

        It installs deps; without the args it cannot know where or for which bead.
        """
        # GIVEN a seam that writes its args into a marker in the worktree
        self.seam(SETUP)
        # WHEN a worktree is created
        r = self.new("--json")
        # THEN the marker holds the three args, and the JSON says setup ran
        self.assertEqual((self.path / ".setup-ran").read_text(), f"{self.path} 42 add-user-login\n")
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["setup"], "ran")

    def test_json_stdout_stays_one_object_when_the_seam_prints(self):
        """R7: with --json the seam's output streams to stderr, so stdout is exactly one JSON line."""
        # GIVEN a seam that prints a line
        self.seam(SETUP)
        # WHEN created with --json
        r = self.new("--json")
        # THEN stdout is one JSON object and the seam's line went to stderr
        self.assertEqual(len(r.stdout.splitlines()), 1)
        json.loads(r.stdout)
        self.assertIn("setup-streamed", r.stderr)

    def test_setup_seam_reruns_when_the_worktree_exists(self):
        """On `exists:` the seam still runs (it must be idempotent), so a half-set-up worktree gets finished."""
        # GIVEN a worktree made before the seam existed
        self.new()
        self.seam(SETUP)
        # WHEN wt new runs again
        self.new()
        # THEN the seam ran this time
        self.assertTrue((self.path / ".setup-ran").exists())

    def test_failing_seam_exits_3_naming_the_seam(self):
        """A setup hook that fails blocks the worktree (exit 3) and names the seam to fix."""
        # GIVEN a seam that exits 1
        seam = self.seam("#!/bin/sh\nexit 1\n")
        # WHEN a worktree is created
        r = self.wt("new", "42", "add-user-login")
        # THEN exit 3 with the seam path
        self.assertEqual(r.returncode, 3)
        self.assertIn(f"setup hook failed: {seam}", r.stderr)

    def test_path_on_another_branch_is_refused(self):
        """The bead's path already checked out on some other branch exits 2 with the fix, touching nothing."""
        # GIVEN the bead's path holding a worktree on a different branch
        run(["git", "worktree", "add", "-q", "-b", "other", str(self.path), "origin/main"], self.p)
        # WHEN wt new targets that path
        r = self.wt("new", "42", "add-user-login")
        # THEN refused, with a fix, and no worktree added
        self.assertEqual(r.returncode, 2)
        self.assertIn("is on branch other", r.stderr)
        self.assertIn("example:", r.stderr)
        self.assertEqual(len(self.worktree_paths()), 2)

    def test_outside_a_repo_exits_3_asking_for_repo(self):
        """Sessions often start in a non-git parent dir: no repo found is exit 3 naming --repo."""
        # GIVEN a cwd that is not inside any git repo
        # WHEN wt new runs there without --repo
        r = self.wt("new", "42", "add-user-login", cwd=self.tmp)
        # THEN blocked, with the fix
        self.assertEqual(r.returncode, 3)
        self.assertIn("run from inside the repo or pass --repo <path>", r.stderr)

    def test_repo_flag_works_from_outside(self):
        """--repo names the repo, so wt new works from a non-git cwd."""
        # GIVEN a non-git cwd
        # WHEN wt new is pointed at the project
        r = self.wt("new", "42", "add-user-login", "--repo", str(self.p), cwd=self.tmp)
        # THEN the worktree is created
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(self.path), self.worktree_paths())

    def test_existing_checks_out_an_existing_branch(self):
        """--existing <branch> checks that branch out (no new branch), for reviewing someone's work."""
        # GIVEN a branch with a commit of its own
        run(["git", "branch", "review-me", "origin/main"], self.p)
        # WHEN a worktree is made for it
        r = self.wt("new", "7", "review", "--existing", "review-me", "--json")
        out = json.loads(r.stdout)
        # THEN it sits on that branch
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["branch"], "review-me")
        self.assertEqual(run(["git", "branch", "--show-current"], out["path"]).stdout.strip(), "review-me")

    def test_existing_remote_ref_gets_a_local_tracking_branch_and_reruns(self):
        """--existing origin/<b> checks out local <b> tracking it (not a detached HEAD), so a rerun is `exists:`.

        R4: recording origin/<b> as the branch made the identical rerun exit 2.
        """
        # GIVEN a branch that exists only on origin
        run(["git", "push", "-q", "origin", "main:review-me"], self.p)
        run(["git", "fetch", "-q", "origin"], self.p)
        # WHEN a worktree is made for origin/review-me, twice
        r = self.wt("new", "7", "review", "--existing", "origin/review-me", "--json")
        again = self.wt("new", "7", "review", "--existing", "origin/review-me")
        # THEN it sits on local review-me tracking origin, and the rerun says exists
        self.assertEqual(r.returncode, 0, r.stderr)
        path = json.loads(r.stdout)["path"]
        self.assertEqual(json.loads(r.stdout)["branch"], "review-me")
        self.assertEqual(run(["git", "branch", "--show-current"], path).stdout.strip(), "review-me")
        self.assertEqual(run(["git", "rev-parse", "--abbrev-ref", "review-me@{upstream}"], self.p).stdout.strip(),
                         "origin/review-me")
        self.assertEqual((again.returncode, again.stdout), (0, f"exists: {path}\n"))

    def test_dry_run_creates_nothing(self):
        """R5: --dry-run says what it would create and writes nothing."""
        # GIVEN a seam that would leave a marker
        self.seam(SETUP)
        # WHEN dry-run
        r = self.new("--dry-run")
        # THEN no worktree, no branch
        self.assertIn(f"would create: {self.path}", r.stdout)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.has_branch(self.branch))

    def test_unsafe_slug_is_refused(self):
        """Bead ids and slugs become a path and a branch name: anything but [A-Za-z0-9._-] is refused (exit 2)."""
        # GIVEN slugs with a slash, a space and a leading dash
        for slug in ("../x", "a b", "-x"):
            # WHEN wt new is called
            r = self.wt("new", "42", slug)
            # THEN refused, nothing created
            with self.subTest(slug=slug):
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(len(self.worktree_paths()), 1)


class List(WtCase):
    def test_list_shows_bead_dirty_and_ahead(self):
        """`wt list --json` gives one row per worktree with the bead id from the branch, dirty and ahead counts."""
        # GIVEN a bead worktree with one commit and an untracked file
        self.new()
        self.commit_in(self.path, "a.txt")
        (self.path / "scratch").write_text("x")
        # WHEN listed
        r = self.wt("list", "--json")
        rows = {json.loads(ln)["path"]: json.loads(ln) for ln in r.stdout.splitlines()}
        # THEN the bead row is right, and the main checkout is listed too
        row = rows[str(self.path)]
        self.assertEqual((row["bead"], row["branch"], row["dirty"], row["ahead"], row["behind"]),
                         ("42", self.branch, True, 1, 0))
        self.assertIsInstance(row["size_kb"], int)
        self.assertIn(str(self.p), rows)

    def test_bead_id_parse_strips_the_prefix_then_takes_one_token(self):
        """The id is [<prefix>-]<token>[.n...]: all-letter hash ids and dotted child ids survive after the prefix.

        Taking words up to the first digit turned `proj-abcde-fix-thing` into `proj`, the wrong bead.
        """
        # GIVEN the issue prefix `my-proj` and branch names in the coordinator's shape
        cases = {"feature/bd-my-proj-abcde-fix-thing": "my-proj-abcde",
                 "feature/bd-my-proj-abcde.3.1-fix-thing": "my-proj-abcde.3.1",
                 "feature/bd-42-add-user-login": "42",
                 "feature/bd-7au.13.2-agent-cli-wt-new": "7au.13.2",
                 "feature/bd-other-abcde-fix": None, "main": None, None: None}
        # WHEN each is parsed  THEN the id comes out, or None when unsure
        for branch, want in cases.items():
            self.assertEqual(wt.bead_of(branch, "my-proj"), want, branch)

    def test_bead_prefix_comes_from_beads_config_else_the_dir_name(self):
        """The prefix is .beads/config.yaml `issue-prefix`; without one, the main checkout's dir name."""
        # GIVEN the project with no beads config, then with one
        before = wt.bead_prefix(self.p)
        (self.p / ".beads").mkdir()
        (self.p / ".beads/config.yaml").write_text('# issue-prefix: "x"\nissue-prefix: "my-proj"\n')
        # WHEN read  THEN dir name first, the config's value after
        self.assertEqual((before, wt.bead_prefix(self.p)), ("proj", "my-proj"))


class Audit(WtCase):
    def test_audit_buckets_each_worktree_and_deletes_nothing(self):
        """`wt audit` suggests merged / stale / active per worktree (main for the checkout) and never deletes.

        It is the read-only half of pruning: a wrong bucket is a suggestion, a deletion would lose work.
        """
        # GIVEN a merged-by-PR branch, an old clean branch, a fresh branch with work, and a fresh empty one
        paths = {}
        for bead, slug in (("1", "squashed"), ("2", "old"), ("3", "busy"), ("4", "empty")):
            r = self.wt("new", bead, slug, "--json")
            paths[slug] = json.loads(r.stdout)["path"]
        self.commit_in(paths["squashed"], "s.txt")
        self.commit_in(paths["old"], "o.txt", date="2020-01-01T00:00:00")
        self.commit_in(paths["busy"], "b.txt")
        # a stand-in gh: GitHub has no local test mode; it reports a merged PR only for bead 1's branch
        fake = self.tmp / "fakebin"
        fake.mkdir()
        (fake / "gh").write_text('#!/bin/sh\ncase "$*" in *bd-1-squashed*) echo \'[{"number":1}]\';; '
                                 '*) echo \'[]\';; esac\n')
        (fake / "gh").chmod(0o755)
        before = self.worktree_paths()
        # WHEN audited
        r = self.wt("audit", "--json", env={"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"})
        rows = [json.loads(ln) for ln in r.stdout.splitlines()]
        got = {row["path"]: row["bucket"] for row in rows}
        # THEN each bucket is right, every worktree is listed, sorted by size, and none were removed
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(got, {str(self.p): "main", paths["squashed"]: "merged", paths["old"]: "stale",
                               paths["busy"]: "active", paths["empty"]: "empty"})
        self.assertEqual([x["size_kb"] for x in rows], sorted((x["size_kb"] for x in rows), reverse=True))
        self.assertEqual(self.worktree_paths(), before)


    def test_fresh_worktree_is_empty_not_merged(self):
        """Right after `wt new` (no commits, main not moved) the bucket is `empty`: `merged` would invite deleting
        a worktree someone just made."""
        # GIVEN a fresh worktree
        self.new()
        # WHEN audited
        rows = {json.loads(ln)["path"]: json.loads(ln) for ln in self.wt("audit", "--json").stdout.splitlines()}
        # THEN empty
        self.assertEqual(rows[str(self.path)]["bucket"], "empty")

    def test_merged_needs_main_to_have_moved_past_the_branch(self):
        """No commits of its own while origin/main moved on: everything it has is in main, so `merged`."""
        # GIVEN a fresh worktree, then a new commit on origin/main
        self.new()
        self.commit_in(self.p, "m.txt")
        run(["git", "push", "-q", "origin", "main"], self.p)
        # WHEN audited
        rows = {json.loads(ln)["path"]: json.loads(ln) for ln in self.wt("audit", "--json").stdout.splitlines()}
        # THEN merged
        self.assertEqual(rows[str(self.path)]["bucket"], "merged")

    def test_worktree_whose_dir_is_gone_is_missing(self):
        """A worktree git still lists but whose dir was deleted (porcelain `prunable`) is `missing`, not active."""
        # GIVEN a worktree whose dir was deleted behind git's back
        self.new()
        shutil.rmtree(self.path)
        # WHEN audited
        rows = {json.loads(ln)["path"]: json.loads(ln) for ln in self.wt("audit", "--json").stdout.splitlines()}
        # THEN missing
        self.assertEqual(rows[str(self.path)]["bucket"], "missing")


class Rm(WtCase):
    def test_rm_without_yes_is_a_dry_run(self):
        """R5: `wt rm` without --yes prints what it would remove and removes nothing."""
        # GIVEN a worktree
        self.new()
        # WHEN rm runs without --yes
        r = self.wt("rm", "--branch", self.branch, "--delete-branch")
        # THEN it says would remove, and both worktree and branch remain
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worktree: would remove\n", r.stdout)
        self.assertIn(f"path: {self.path}\n", r.stdout)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.has_branch(self.branch))

    def test_dry_run_says_an_unmerged_branch_would_be_kept(self):
        """The dry run matches the real run: an unmerged branch without --force is kept, so it must not say delete."""
        # GIVEN a clean worktree whose branch has an unmerged commit
        self.new()
        self.commit_in(self.path, "a.txt")
        # WHEN rm dry-runs with and without --force
        plain = json.loads(self.wt("rm", "--branch", self.branch, "--delete-branch", "--json").stdout)
        forced = json.loads(self.wt("rm", "--branch", self.branch, "--delete-branch", "--force", "--json").stdout)
        # THEN only --force would delete
        self.assertEqual(plain["branch_deleted"], "would keep (unmerged; needs --force)")
        self.assertEqual(forced["branch_deleted"], "would delete")

    def test_rm_yes_removes_the_worktree_and_keeps_the_branch(self):
        """`wt rm --yes` removes the worktree; the branch stays unless --delete-branch."""
        # GIVEN a worktree
        self.new()
        # WHEN removed by path
        r = self.wt("rm", "--path", str(self.path), "--yes", "--json")
        # THEN gone from git and disk, branch kept
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["worktree"], "removed")
        self.assertNotIn(str(self.path), self.worktree_paths())
        self.assertFalse(self.path.exists())
        self.assertTrue(self.has_branch(self.branch))

    def test_dirty_worktree_without_force_is_refused(self):
        """An untracked file without --force exits 2 and removes nothing: it may be unsaved work."""
        # GIVEN a worktree with an untracked file
        self.new()
        (self.path / "notes.txt").write_text("wip")
        # WHEN removed without --force
        r = self.wt("rm", "--branch", self.branch, "--delete-branch", "--yes")
        # THEN refused, all still there
        self.assertEqual(r.returncode, 2)
        self.assertIn("--force", r.stderr)
        self.assertTrue((self.path / "notes.txt").exists())
        self.assertTrue(self.has_branch(self.branch))

    def test_force_removes_a_dirty_squash_merged_worktree_and_branch(self):
        """A squash-merged branch (commits not in main) plus an untracked file goes with --force: -D fallback."""
        # GIVEN a branch with its own commit and an untracked file
        self.new()
        self.commit_in(self.path, "a.txt")
        (self.path / "notes.txt").write_text("wip")
        # WHEN removed with --delete-branch --force --yes
        r = self.wt("rm", "--branch", self.branch, "--delete-branch", "--force", "--yes")
        # THEN both gone, exit 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.has_branch(self.branch))

    def test_unmerged_branch_without_force_is_partial(self):
        """Without --force an unmerged branch is kept after the worktree goes: exit 5 naming --force."""
        # GIVEN a clean worktree whose branch has an unmerged commit
        self.new()
        self.commit_in(self.path, "a.txt")
        # WHEN removed with --delete-branch but no --force
        r = self.wt("rm", "--branch", self.branch, "--delete-branch", "--yes")
        # THEN worktree gone, branch kept, exit 5
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.has_branch(self.branch))
        self.assertIn("--force", r.stderr)

    def test_branch_with_no_worktree_is_still_deleted(self):
        """R4: no worktree is `already done`, but --delete-branch still deletes the local branch (as /merged did)."""
        # GIVEN a branch and no worktree
        run(["git", "branch", self.branch, "origin/main"], self.p)
        # WHEN rm runs for it
        r = self.wt("rm", "--branch", self.branch, "--delete-branch", "--yes", "--json")
        out = json.loads(r.stdout)
        # THEN exit 0, worktree already done, branch gone
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out["worktree"], out["branch_deleted"]), ("already done", "deleted"))
        self.assertFalse(self.has_branch(self.branch))

    def test_main_checkout_is_never_removed(self):
        """The main checkout holds the repo: rm refuses it with exit 2."""
        # GIVEN the main checkout
        # WHEN rm targets it
        r = self.wt("rm", "--path", str(self.p), "--force", "--yes")
        # THEN refused, still there
        self.assertEqual(r.returncode, 2)
        self.assertTrue((self.p / ".git").exists())


class HookRule(unittest.TestCase):
    def setUp(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-wt-hook-")))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.p = make_project(tmp / "proj")

    def test_hand_worktree_add_on_a_new_branch_is_denied(self):
        """A hand-typed `git worktree add ... -b|-B` is denied, pointing at `agent-cli wt new`.

        By hand the project's setup hook never runs, so the worktree starts without deps.
        """
        # GIVEN the forms agents type, including the old coordinator block's line
        cmds = ['git -C "$MAIN_ROOT" worktree add "$WORKTREE_PATH" -b "$BRANCH" origin/main',
                "git worktree add ../x -b feat origin/main",
                "cd repo && git worktree add -B feat ../x origin/main",
                "git worktree add -b feat ../x",
                "git worktree add --detach -b x ../p",
                "git worktree add -bx ../p",
                "git worktree add -b=x ../p"]
        for c in cmds:
            # WHEN checked
            r = agent_cli(self.p, "hook", "test", "--cmd", c, "--json")
            got = json.loads(r.stdout)
            # THEN denied by hand-worktree-add alone, with the replacement
            with self.subTest(cmd=c):
                self.assertEqual(r.returncode, 2)
                self.assertEqual([x["id"] for x in got["rules"]], ["hand-worktree-add"])
                self.assertIn("agent-cli wt new <bead-id> <slug> --base origin/main", got["rules"][0]["message"])

    def test_bypass_comment_allows_a_one_off(self):
        """`# agent-cli: allow hand-worktree-add` lets a deliberate hand-made worktree through."""
        # GIVEN the bypass comment
        c = "git worktree add -b feat ../x  # agent-cli: allow hand-worktree-add"
        # WHEN checked
        r = agent_cli(self.p, "hook", "test", "--cmd", c, "--json")
        # THEN allowed
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["decision"], "allow")


if __name__ == "__main__":
    unittest.main()
