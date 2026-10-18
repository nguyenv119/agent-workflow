"""The canary wiring in the workflow text: the coordinator and /pr start the laptop canary on a harness PR (never
in a cloud session), /merged fast-forwards the main checkout, and AGENTS.md says both."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402
from helpers import GIT, make_repo, run  # noqa: E402

COORDINATOR = ROOT / ".claude/skills/coordinator/SKILL.md"
PR = ROOT / ".claude/commands/pr.md"
MERGED = ROOT / ".claude/commands/merged.md"
AGENTS = ROOT / "AGENTS.md"
CLOUD_LINE = "waits for a laptop canary run"


class CanaryWiringText(unittest.TestCase):
    def test_coordinator_and_pr_start_the_canary(self):
        """The coordinator's CI-green gate and /pr say to start the canary command for the PR.

        `agent-cli pr wait` keeps a harness PR WAITING until harness-canary is posted; if no flow starts the
        canary, every harness PR waits forever. Breaks: harness PRs never land.
        """
        for f in (COORDINATOR, PR):
            with self.subTest(file=f.name):
                # GIVEN / WHEN the file's text / THEN it says to start the canary command for a PR
                self.assertIn("start `agent-cli harness canary --pr", f.read_text())

    def test_coordinator_and_pr_skip_the_canary_in_a_cloud_session(self):
        """The coordinator and /pr gate the canary on CLAUDE_CODE_REMOTE and print the waits-for-a-laptop line.

        A cloud session has no Docker and its `gh pr` GraphQL calls return 403. Breaks: a cloud /pr starts a
        canary that refuses, or polls a status call that fails, instead of saying the PR waits for a laptop.
        """
        for f in (COORDINATOR, PR):
            with self.subTest(file=f.name):
                # GIVEN / WHEN the file's text / THEN it names the env var and the exact line
                text = f.read_text()
                self.assertIn("CLAUDE_CODE_REMOTE", text)
                self.assertIn(CLOUD_LINE, text)

    def test_merged_fast_forwards_the_main_checkout(self):
        """/merged finds the main checkout (first `git worktree list --porcelain` entry) and pulls --ff-only.

        A laptop whose harness is the main checkout's working tree keeps the pre-merge harness otherwise.
        Breaks: after a merge, the next session runs the old harness.
        """
        # GIVEN / WHEN merged.md's text
        text = MERGED.read_text()
        # THEN it locates the main checkout and fast-forwards it only, before the worktree (maybe the cwd) goes
        self.assertIn("git worktree list --porcelain", text)
        self.assertIn("pull --ff-only", text)
        self.assertLess(text.index("## 5. Fast-forward the main checkout"),
                        text.index("## 6. Remove the worktree and local branch"))

    def test_agents_md_names_the_canary_and_the_fast_forward(self):
        """AGENTS.md's /work and /merged paragraphs say the canary starts and the main checkout fast-forwards.

        AGENTS.md is what every agent reads first. Breaks: an agent reads the flow and misses both steps.
        """
        # GIVEN / WHEN AGENTS.md's text / THEN both sentences are there (main already names the canary command)
        text = AGENTS.read_text()
        self.assertIn("the coordinator starts the laptop canary", text)
        self.assertIn("fast-forwards the main checkout", text)

    def test_pr_step_8_names_the_stale_pending_rule_and_cites_the_default_paths(self):
        """/pr step 8 restarts a pending canary older than 2 hours and cites harness_edit.py's DEFAULT_PATHS.

        A killed run leaves pending "running since <time>" on the head; with no restart rule the PR waits forever.
        A copied path list drifts from the code. Breaks: a killed canary blocks its PR, or the cloud line fires on
        the wrong files.
        """
        # GIVEN / WHEN pr.md's text / THEN both are there and the coordinator points to it
        text = PR.read_text()
        self.assertIn("more than 2 hours old", text)
        self.assertIn("`DEFAULT_PATHS` in agent-cli's `harness_edit.py`", text)
        self.assertIn("`canary.state` is `error`", text)
        self.assertIn("follow `/pr` step 8", COORDINATOR.read_text())

    def test_coordinator_skill_still_loads(self):
        """The coordinator SKILL.md frontmatter still carries name and description (loop.py's load check).

        Breaks: a skill without them does not load, and /work has no coordinator.
        """
        # GIVEN / WHEN the coordinator skill's frontmatter / THEN nothing is missing
        self.assertEqual(loop.frontmatter_ok(COORDINATOR.read_text()), set())


def fast_forward_block():
    """The bash block of merged.md's "Fast-forward the main checkout" step."""
    step = MERGED.read_text().split("Fast-forward the main checkout", 1)[1]
    return re.search(r"```bash\n(.*?)```", step, re.S)[1]


class MergedFastForward(unittest.TestCase):
    """merged.md's fast-forward block run with bash from a linked worktree of a main clone of a bare origin."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="merged-ff-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        origin = self.tmp / "origin.git"
        run(GIT + ["init", "-q", "--bare", str(origin)], self.tmp)
        seed, _ = make_repo(self.tmp / "seed", {"README.md": "one\n"})
        run(GIT + ["push", "-q", str(origin), "HEAD:main"], seed)
        self.main = self.tmp / "main"
        run(GIT + ["clone", "-q", str(origin), str(self.main)], self.tmp)
        self.wt = self.tmp / "wt"
        run(GIT + ["worktree", "add", "-q", "-b", "feature", str(self.wt)], self.main)
        _, self.merged = make_repo(seed, {"README.md": "two\n"})  # the merge lands on origin's main
        run(GIT + ["push", "-q", str(origin), "HEAD:main"], seed)
        self.before = self.head()

    def head(self):
        return run(["git", "rev-parse", "HEAD"], self.main).stdout.strip()

    def ff(self, cwd=None):
        """Run the block from cwd (default the linked worktree), with no user or system git config; its stdout."""
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        r = subprocess.run(["bash", "-c", fast_forward_block()], cwd=cwd or self.wt, env=env, text=True,
                           capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_clean_main_checkout_is_fast_forwarded_to_origin(self):
        """A clean main checkout on main moves to origin's main and says so.

        A laptop's harness is the main checkout's working tree. Breaks: after a merge it runs the old harness.
        """
        # GIVEN a clean main checkout one commit behind origin (setUp)
        # WHEN the block runs from the branch's worktree
        out = self.ff()
        # THEN main is at origin's main, and the line says so
        self.assertEqual(self.head(), self.merged)
        self.assertIn(f"main checkout: {self.main} fast-forwarded to {self.merged[:7]}", out)

    def test_uncommitted_tracked_change_skips_and_keeps_head(self):
        """A tracked change in the main checkout skips the pull and leaves HEAD and the change alone.

        The block never resets, stashes or discards. Breaks: a user's edit lost, or a pull over it fails midway.
        """
        # GIVEN a tracked file edited in the main checkout
        (self.main / "README.md").write_text("mine\n")
        # WHEN the block runs
        out = self.ff()
        # THEN skipped with the reason; HEAD and the edit unchanged
        self.assertIn(f"main checkout: skipped fast-forward: {self.main} has uncommitted changes", out)
        self.assertEqual((self.head(), (self.main / "README.md").read_text()), (self.before, "mine\n"))

    def test_main_checkout_on_another_branch_skips(self):
        """A main checkout on another branch is not moved, and the line names the branch.

        Pulling there would merge main into someone's branch. Breaks: a user's branch rewritten by /merged.
        """
        # GIVEN the main checkout on branch other
        run(GIT + ["checkout", "-q", "-b", "other"], self.main)
        # WHEN the block runs
        out = self.ff()
        # THEN skipped, naming the branch; HEAD unchanged
        self.assertIn(f"main checkout: skipped fast-forward: {self.main} is on other, not main", out)
        self.assertEqual(self.head(), self.before)

    def test_no_main_checkout_found_skips_with_that_reason(self):
        """Outside any git checkout the block says it could not find the main checkout, and moves nothing.

        An empty path would make every `git -C ""` act on the cwd. Breaks: a false "detached HEAD" reason.
        """
        # GIVEN a directory that is no git checkout
        bare = self.tmp / "not-a-repo"
        bare.mkdir()
        # WHEN the block runs there
        out = self.ff(bare)
        # THEN the could-not-find line, and main untouched
        self.assertIn("main checkout: skipped fast-forward: could not find the main checkout", out)
        self.assertEqual(self.head(), self.before)


if __name__ == "__main__":
    unittest.main()
