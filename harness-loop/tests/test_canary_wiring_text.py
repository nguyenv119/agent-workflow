"""The canary wiring in the workflow text: the coordinator and /pr start the laptop canary on a harness PR (never
in a cloud session), /merged fast-forwards the main checkout, and AGENTS.md says both."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness-loop"))
import loop  # noqa: E402

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
        # THEN it locates the main checkout and fast-forwards it only
        self.assertIn("git worktree list --porcelain", text)
        self.assertIn("pull --ff-only", text)

    def test_agents_md_names_the_canary_and_the_fast_forward(self):
        """AGENTS.md's /work and /merged paragraphs say the canary starts and the main checkout fast-forwards.

        AGENTS.md is what every agent reads first. Breaks: an agent reads the flow and misses both steps.
        """
        # GIVEN / WHEN AGENTS.md's text / THEN both are named
        text = AGENTS.read_text()
        self.assertIn("agent-cli harness canary", text)
        self.assertIn("fast-forward", text)

    def test_coordinator_skill_still_loads(self):
        """The coordinator SKILL.md frontmatter still carries name and description (loop.py's load check).

        Breaks: a skill without them does not load, and /work has no coordinator.
        """
        # GIVEN / WHEN the coordinator skill's frontmatter / THEN nothing is missing
        self.assertEqual(loop.frontmatter_ok(COORDINATOR.read_text()), set())


if __name__ == "__main__":
    unittest.main()
