"""Tests for the harness-canary rule in `agent-cli pr status|wait|land`.

A PR whose changed files match the harness-paths globs is green only once the `harness-canary` commit status
(posted by `agent-cli harness canary`) says success; other PRs never wait for it. config() and verdict() run
in-process; the commands run as subprocesses against the fake gh in helpers.py.
"""
import json
import sys
import unittest

from helpers import CLAUDE, GhCase, check, set_env, view

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import pr  # noqa: E402
from agent_cli.core import Exit  # noqa: E402

CFG = {"required": None, "advisory": [], "preview": None}
SKILL = ".claude/skills/x/SKILL.md"
NOT_POSTED = "harness-canary not posted: run agent-cli harness canary --pr 42"


def canary(state=None, description=None, required=True):
    return {"context": "harness-canary", "required": required, "paths": [SKILL] if required else [],
            "state": state, "description": description, "reason": None if required else "no harness path changed"}


class CanaryConfig(unittest.TestCase):
    """config(): the optional `canary-check:` line under `## coordinator`."""

    def test_none_off_or_an_empty_value_turns_the_canary_off(self):
        """`canary-check: none`, `off` (any case) or an empty value means no PR waits for a canary status.

        A repo that never runs the canary would otherwise hold every harness PR in WAITING until the cap.
        """
        for value in ("none", "OFF", ""):
            # GIVEN a rules file turning the canary off
            text = f"## coordinator\ncanary-check: {value}\n"
            # WHEN parsed
            cfg = pr.config(text)
            # THEN no status is required, and nothing is left to auto-detect
            with self.subTest(value=value):
                self.assertEqual((cfg["canary"], cfg["canary_auto"]), (None, False))

    def test_a_named_line_requires_that_status_without_asking_the_repo(self):
        """`canary-check: <name>` requires that status context on every harness PR, with no harness-repo probe."""
        # GIVEN a rules file naming a status
        # WHEN parsed
        cfg = pr.config("## coordinator\ncanary-check: my-canary\n")
        # THEN that name, not auto
        self.assertEqual((cfg["canary"], cfg["canary_auto"]), ("my-canary", False))

    def test_a_keyed_line_wins_for_its_repo_only(self):
        """`canary-check[acme/web]:` replaces the plain line for acme/web PRs, as the other coordinator keys do."""
        # GIVEN the canary off by default but on for acme/web
        text = "## coordinator\ncanary-check: none\ncanary-check[acme/web]: harness-canary\n"
        # WHEN parsed for acme/web and acme/other
        web, other = pr.config(text, "acme/web"), pr.config(text, "acme/other")
        # THEN only acme/web requires the canary
        self.assertEqual(web["canary"], "harness-canary")
        self.assertIsNone(other["canary"])


class CanaryVerdict(unittest.TestCase):
    """verdict() with a canary dict: how the status combines with CI and the merge state."""

    def test_a_missing_canary_overrides_the_merge_state_blocked_on(self):
        """A green, BLOCKED PR with no canary names the canary command in blocked_on, not BLOCKED.

        The canary is the step the agent can take now; the merge state may only clear after it.
        """
        # GIVEN CI green, merge state BLOCKED, canary not posted
        # WHEN judged
        s = pr.verdict(42, view(merge="BLOCKED"), [check("lint", "pass")], CFG, canary())
        # THEN WAITING on the canary
        self.assertEqual((s["verdict"], s["blocked_on"]), ("WAITING", NOT_POSTED))

    def test_a_missing_canary_is_named_while_ci_still_runs(self):
        """blocked_on names the canary even while CI is pending: it can run in parallel with CI."""
        # GIVEN a pending check and no canary
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pending")], CFG, canary())
        # THEN WAITING, canary named
        self.assertEqual((s["verdict"], s["blocked_on"]), ("WAITING", NOT_POSTED))

    def test_a_failed_canary_is_failed_while_ci_still_runs(self):
        """A canary failure is FAILED at once, like a failed required check, so wait stops on red."""
        # GIVEN a pending check and a failed canary
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pending")], CFG, canary("failure", "2/3 tasks failed"))
        # THEN FAILED
        self.assertEqual(s["verdict"], "FAILED")

    def test_a_pending_canary_without_a_description_names_only_its_state(self):
        """A pending canary with no description reads "harness-canary pending", with no dangling colon."""
        # GIVEN a green PR, canary pending with an empty description
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pass")], CFG, canary("pending", ""))
        # THEN WAITING on the pending canary
        self.assertEqual((s["verdict"], s["blocked_on"]), ("WAITING", "harness-canary pending"))

    def test_no_checks_stays_no_checks_whatever_the_canary(self):
        """With no CI checks the verdict stays NO_CHECKS, so wait's appear-grace still applies."""
        # GIVEN no checks and no canary
        # WHEN judged
        s = pr.verdict(42, view(), [], CFG, canary())
        # THEN NO_CHECKS
        self.assertEqual(s["verdict"], "NO_CHECKS")

    def test_a_not_required_canary_changes_nothing(self):
        """A canary dict with required False leaves a green PR READY and is reported as is."""
        # GIVEN a green PR whose files miss every harness path
        c = canary(required=False)
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pass")], CFG, c)
        # THEN READY with the dict in the output
        self.assertEqual((s["verdict"], s["canary"]), ("READY", c))


class HarnessRepoCache(GhCase):
    def test_the_harness_repo_answer_is_asked_once_per_repo_and_base(self):
        """harness_repo() asks GitHub once per (repo, base) per process, so `pr wait` does not re-ask each poll.

        Every poll already costs three API calls from a budget shared by every session.
        """
        # GIVEN o/r with .claude/HARNESS_VERSION, the fake gh on this process's PATH
        set_env(self, **self.env)
        (self.gh / "contents.txt").write_text(".claude/HARNESS_VERSION\n")
        # WHEN asked twice for the same base
        got = [pr.harness_repo("o/r", "cache-test-base", 30) for _ in range(2)]
        # THEN True both times, one contents call made
        self.assertEqual(got, [True, True])
        self.assertEqual(sum("/contents/" in " ".join(c) for c in self.calls()), 1)


class CanaryCli(GhCase):
    """`pr status|land` end to end with the fake gh: changed files, harness-repo probe, commit status."""

    def setUp(self):
        super().setUp()
        (self.gh / "contents.txt").write_text(".claude/HARNESS_VERSION\n")  # o/r is an installing repo

    def pr_files(self, *names, renamed_from=None):
        rows = [{"filename": n} for n in names]
        if renamed_from:
            rows[0]["previous_filename"] = renamed_from
        (self.gh / "files.json").write_text(json.dumps(rows))

    def post(self, state, description=""):
        """The head's combined status: another context, then harness-canary in state."""
        (self.gh / "status.json").write_text(json.dumps({"statuses": [
            {"context": "ci/other", "state": "failure", "description": "unrelated"},
            {"context": "harness-canary", "state": state, "description": description}]}))

    def status(self, *args):
        r = self.cli("status", "42", "--json", *args)
        return r, json.loads(r.stdout) if r.stdout.strip() else None

    def api_calls(self, part=""):
        return [c for c in self.calls() if c[0] == "api" and any(part in x for x in c)]

    def green_harness_pr(self):
        self.set_pr(view(), [check("lint", "pass")])
        self.pr_files("README.md", SKILL)

    def test_a_harness_pr_without_the_canary_waits_and_names_the_command(self):
        """A PR touching a skill, CI green and CLEAN, with no harness-canary status is WAITING (exit 5) and
        blocked_on names the command that posts it.

        Without this, a harness change lands untested by the canary exam the gate exists for.
        """
        # GIVEN a green harness PR and no canary status
        self.green_harness_pr()
        # WHEN status runs
        r, s = self.status()
        # THEN WAITING on the canary, the skill listed as the reason it is required
        self.assertEqual(r.returncode, Exit.PARTIAL, r.stderr)
        self.assertEqual((s["verdict"], s["blocked_on"]), ("WAITING", NOT_POSTED))
        self.assertEqual((s["canary"]["required"], s["canary"]["paths"], s["canary"]["state"]), (True, [SKILL], None))

    def test_with_repo_the_named_command_carries_it(self):
        """With --repo the canary command in blocked_on carries the same --repo, so it is copy-pasteable."""
        # GIVEN a green harness PR and no canary status
        self.green_harness_pr()
        # WHEN status runs with --repo
        _, s = self.status("--repo", "o/r")
        # THEN the command ends in --repo o/r
        self.assertEqual(s["blocked_on"], NOT_POSTED + " --repo o/r")

    def test_a_harness_pr_with_a_successful_canary_is_ready(self):
        """harness-canary success counts as a pass: the same PR is READY (exit 0)."""
        # GIVEN a green harness PR, canary success
        self.green_harness_pr()
        self.post("success", "3/3 tasks passed")
        # WHEN status runs
        r, s = self.status()
        # THEN READY
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual((s["verdict"], s["canary"]["state"]), ("READY", "success"))

    def test_a_failed_or_errored_canary_is_failed(self):
        """harness-canary failure or error is FAILED (exit 1), like a failed required check."""
        for state in ("failure", "error"):
            # GIVEN a green harness PR whose canary ended in state
            self.green_harness_pr()
            self.post(state, "exam result")
            # WHEN status runs
            r, s = self.status()
            # THEN FAILED
            with self.subTest(state=state):
                self.assertEqual(r.returncode, Exit.FAIL, r.stderr)
                self.assertEqual(s["verdict"], "FAILED")

    def test_a_pending_canary_waits_with_its_description(self):
        """A pending canary is WAITING, and blocked_on quotes its description (what the exam is doing)."""
        # GIVEN a green harness PR, canary pending
        self.green_harness_pr()
        self.post("pending", "exam running")
        # WHEN status runs
        r, s = self.status()
        # THEN WAITING on the pending canary
        self.assertEqual(r.returncode, Exit.PARTIAL, r.stderr)
        self.assertEqual(s["blocked_on"], "harness-canary pending: exam running")

    def test_a_pr_touching_no_harness_path_never_waits_for_the_canary(self):
        """A README-only PR is READY with no canary status, and its head's statuses are never read.

        Nobody posts a canary for such a PR; requiring one would hang it until the cap.
        """
        # GIVEN a green README-only PR, no canary
        self.set_pr(view(), [check("lint", "pass")])
        self.pr_files("README.md")
        # WHEN status runs
        r, s = self.status()
        # THEN READY, not required, no status or contents call
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual((s["verdict"], s["canary"]["required"]), ("READY", False))
        self.assertEqual((self.api_calls("/status"), self.api_calls("/contents/")), ([], []))

    def test_canary_check_none_restores_the_old_behaviour_without_api_calls(self):
        """`canary-check: none` makes a harness PR READY on CI alone, with no gh api call at all."""
        # GIVEN the canary off and a green harness PR
        self.rules("## coordinator\ncanary-check: none\n")
        self.green_harness_pr()
        # WHEN status runs
        r, s = self.status()
        # THEN READY, canary null, no api call
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertIsNone(s["canary"])
        self.assertEqual(self.api_calls(), [])

    def test_a_file_renamed_away_from_a_harness_path_counts(self):
        """A rename counts by its old name too: moving a skill out of .claude/skills changes the harness."""
        # GIVEN a green PR renaming the skill file to docs/
        self.set_pr(view(), [check("lint", "pass")])
        self.pr_files("docs/x.md", renamed_from=SKILL)
        # WHEN status runs
        _, s = self.status()
        # THEN required, the old name listed
        self.assertEqual((s["verdict"], s["canary"]["paths"]), ("WAITING", [SKILL]))

    def test_project_harness_paths_replace_the_defaults(self):
        """harness-paths under `## harness` (the harness-edit hook's line) decides which PRs need the canary.

        One list for both the edit hook and the PR gate; a copy would drift and gate the wrong PRs.
        """
        # GIVEN harness-paths: docs/**
        self.rules("## harness\nharness-paths: docs/**\n")
        self.set_pr(view(), [check("lint", "pass")])
        cases = {"docs/a.md": True, SKILL: False}
        for path, required in cases.items():
            # WHEN status runs on a PR changing path
            self.pr_files(path)
            _, s = self.status()
            # THEN required exactly for the project's glob
            with self.subTest(path=path):
                self.assertEqual(s["canary"]["required"], required)

    def test_auto_on_a_repo_that_is_no_harness_never_requires_the_canary(self):
        """With no canary-check line, a PR in a repo with neither .claude/HARNESS_VERSION nor harness-loop/loop.py
        is READY on CI alone, even when it touches AGENTS.md.

        A plain product repo touching its AGENTS.md has nobody to run a canary; requiring one would hang it.
        """
        # GIVEN a plain repo and a green PR changing AGENTS.md
        (self.gh / "contents.txt").write_text("")
        self.set_pr(view(), [check("lint", "pass")])
        self.pr_files("AGENTS.md")
        # WHEN status runs
        r, s = self.status()
        # THEN READY, not required because not a harness repo, statuses never read
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual((s["canary"]["required"], s["canary"]["reason"]), (False, "not a harness repo"))
        self.assertEqual(self.api_calls("/status"), [])

    def test_auto_on_the_harness_source_repo_requires_the_canary(self):
        """harness-loop/loop.py at the base ref marks the harness source repo itself: the canary is required."""
        # GIVEN the source repo (no HARNESS_VERSION) and a green harness PR
        (self.gh / "contents.txt").write_text("harness-loop/loop.py\n")
        self.green_harness_pr()
        # WHEN status runs
        _, s = self.status()
        # THEN required, probed at the PR's base ref
        self.assertEqual(s["blocked_on"], NOT_POSTED)
        self.assertIn("repos/o/r/contents/harness-loop/loop.py?ref=main", self.api_calls("loop.py")[0])

    def test_an_explicit_canary_line_needs_no_harness_repo_probe(self):
        """`canary-check: harness-canary` requires the canary without asking GitHub what the repo is."""
        # GIVEN the explicit line, a plain repo and a green harness PR
        self.rules("## coordinator\ncanary-check: harness-canary\n")
        (self.gh / "contents.txt").write_text("")
        self.green_harness_pr()
        # WHEN status runs
        _, s = self.status()
        # THEN required, no contents call
        self.assertEqual(s["blocked_on"], NOT_POSTED)
        self.assertEqual(self.api_calls("/contents/"), [])

    def test_a_failing_gh_api_exits_3_with_its_message(self):
        """A gh api failure (auth, rate limit) is BLOCKED (exit 3) with gh's message, never a guessed verdict."""
        # GIVEN gh api failing
        self.green_harness_pr()
        (self.gh / "api.err").write_text("gh: API rate limit exceeded (HTTP 403)\n")
        # WHEN status runs
        r, _ = self.status()
        # THEN blocked, message passed on
        self.assertEqual(r.returncode, Exit.BLOCKED)
        self.assertIn("API rate limit exceeded", r.stderr)

    def test_an_unreadable_status_reply_exits_3(self):
        """A status reply that is not the documented JSON is BLOCKED (exit 3), never read as "not posted"."""
        # GIVEN a green harness PR and a status reply that is not JSON
        self.green_harness_pr()
        (self.gh / "status.json").write_text("<html>proxy error</html>")
        # WHEN status runs
        r, _ = self.status()
        # THEN blocked, the status call named
        self.assertEqual(r.returncode, Exit.BLOCKED)
        self.assertIn("commits/" + "a" * 40 + "/status: unexpected JSON", r.stderr)

    def test_land_refuses_a_harness_pr_without_the_canary(self):
        """`pr land --yes` refuses (exit 2) a green harness PR with no canary, says why, and merges nothing."""
        # GIVEN a green harness PR, no canary
        self.green_harness_pr()
        # WHEN landed
        r = self.cli("land", "42", "--yes")
        # THEN refused with the canary named, no merge
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn(NOT_POSTED, r.stderr)
        self.assertEqual(self.merges(), [])

    def test_land_merges_a_harness_pr_once_the_canary_passed(self):
        """With harness-canary success posted, `pr land --yes` merges the checked head."""
        # GIVEN a green harness PR with canary success
        self.green_harness_pr()
        self.post("success")
        # WHEN landed
        r = self.cli("land", "42", "--yes")
        # THEN merged
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual(self.merges(), [["pr", "merge", "42", "--merge", "--match-head-commit", "a" * 40]])


if __name__ == "__main__":
    unittest.main()
