"""Tests for `agent-cli pr status|wait|land`.

The verdict and the wait loop run in-process (a fake clock stands in for 40 minutes); the commands run
as subprocesses with a fake `gh` on PATH that replays gh JSON recorded from a public repo's PRs.
The hand-pr-poll hook rule is tested in test_hook.py (Deny).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, FakeClock, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import pr  # noqa: E402
from agent_cli.core import Exit  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures/pr"
REQ = {"required": ["lint", "govulncheck"], "advisory": [], "preview": None}
DEFAULT = {"required": None, "advisory": [], "preview": None}

# REVIEW: mocking core dependency (gh / the GitHub API, which has no local or test-mode equivalent).
# It replays $FAKE_GH/view.json and checks.json (no checks.json = gh's "no checks reported", exit 1;
# view.err = a failing gh pr view; a `slow` file = sleep that many seconds first), appends every argv to
# calls.log and each `gh pr merge` argv to merge.log.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys, time
d, a = os.environ["FAKE_GH"], sys.argv[1:]
open(f"{d}/calls.log", "a").write(json.dumps(a) + "\n")
if os.path.exists(f"{d}/slow"):
    time.sleep(float(open(f"{d}/slow").read()))
if a[:2] == ["pr", "view"]:
    if os.path.exists(f"{d}/view.err"):
        sys.stderr.write(open(f"{d}/view.err").read()); sys.exit(1)
    print(open(f"{d}/view.json").read())
elif a[:2] == ["pr", "checks"]:
    if not os.path.exists(f"{d}/checks.json"):
        sys.stderr.write("no checks reported on the 'feature' branch\n"); sys.exit(1)
    print(open(f"{d}/checks.json").read())
elif a[:2] == ["pr", "merge"]:
    open(f"{d}/merge.log", "a").write(json.dumps(a) + "\n")
else:
    sys.exit(2)
'''


def load(name):
    return json.loads((FIX / name).read_text())


def view(state="OPEN", merge="CLEAN", head="a" * 40):
    return {"baseRefName": "main", "headRefOid": head, "mergeStateStatus": merge, "state": state,
            "url": "https://github.com/o/r/pull/42"}


def check(name, bucket, link=""):
    return {"name": name, "bucket": bucket, "state": bucket.upper(), "link": link or f"https://ci.example/{name}"}


class Verdict(unittest.TestCase):
    """verdict(): the coordinator's CI-green definition applied to one gh pr view + gh pr checks."""

    def test_all_required_passing_and_clean_is_ready(self):
        """Every required check `pass` plus mergeStateStatus CLEAN is READY (exit 0).

        This is the gate the coordinator waits on before the next bead; a wrong READY stacks work on red.
        """
        # GIVEN a recorded PR whose checks all passed, two of them required
        # WHEN judged
        s = pr.verdict(14580, load("view_14580.json"), load("checks_14580.json"), REQ)
        # THEN READY, and the required list is exactly the two configured checks
        self.assertEqual(s["verdict"], "READY")
        self.assertEqual(sorted(c["name"] for c in s["required"]), ["govulncheck", "lint"])
        self.assertEqual(pr.EXIT[s["verdict"]], Exit.OK)

    def test_any_required_failure_is_failed(self):
        """A required check in bucket `fail` is FAILED (exit 1), even while other checks still run.

        Waiting out the rest of the run would waste the 40-minute cap on a PR that is already red.
        """
        # GIVEN a recorded PR whose govulncheck failed while a build is pending
        # WHEN judged with govulncheck required
        s = pr.verdict(14665, load("view_14665.json"), load("checks_14665.json"), REQ)
        # THEN FAILED
        self.assertEqual(s["verdict"], "FAILED")
        self.assertEqual(pr.EXIT[s["verdict"]], Exit.FAIL)

    def test_required_check_pending_is_waiting(self):
        """A required check still `pending` is WAITING (exit 5), so `wait` keeps polling."""
        # GIVEN the recorded PR with one build still running, that build required
        cfg = dict(REQ, required=["lint", "build (windows-latest)"])
        # WHEN judged
        s = pr.verdict(14665, load("view_14665.json"), load("checks_14665.json"), cfg)
        # THEN WAITING
        self.assertEqual(s["verdict"], "WAITING")
        self.assertEqual(pr.EXIT[s["verdict"]], Exit.PARTIAL)

    def test_zero_checks_is_no_checks_never_ready(self):
        """No checks at all is NO_CHECKS: "no checks" is never "green"."""
        # GIVEN an open, CLEAN PR with no checks
        # WHEN judged
        s = pr.verdict(42, view(), [], DEFAULT)
        # THEN NO_CHECKS
        self.assertEqual(s["verdict"], "NO_CHECKS")

    def test_advisory_only_failure_stays_ready(self):
        """A failing advisory check (a bot reviewer) never blocks, even though GitHub then says UNSTABLE.

        Advisory checks are comment-only integrations; blocking on them stalls every PR they dislike.
        """
        # GIVEN govulncheck failed but is advisory, lint required and passing, merge state UNSTABLE
        cfg = {"required": ["lint"], "advisory": ["govulncheck"], "preview": None}
        # WHEN judged
        s = pr.verdict(14665, view(merge="UNSTABLE"), load("checks_14665.json"), cfg)
        # THEN READY, with the failure listed under advisory
        self.assertEqual(s["verdict"], "READY")
        self.assertEqual(s["advisory"], [{"name": "govulncheck", "bucket": "fail",
                                          "link": next(c["link"] for c in load("checks_14665.json")
                                                       if c["name"] == "govulncheck")}])

    def test_unstable_with_a_required_check_not_passing_is_not_ready(self):
        """UNSTABLE counts only when every required check passed; a pending required check keeps it WAITING."""
        # GIVEN a required check still pending under UNSTABLE
        # WHEN judged
        s = pr.verdict(42, view(merge="UNSTABLE"), [check("lint", "pending")], DEFAULT)
        # THEN WAITING
        self.assertEqual(s["verdict"], "WAITING")

    def test_skipped_required_check_counts_as_passing(self):
        """A configured required check that GitHub skipped (a path filter) counts as passing, as in branch protection.

        Otherwise a docs-only PR whose required test job is path-filtered would WAIT until the 40-minute cap.
        """
        # GIVEN lint passing and a required test job skipped, merge state CLEAN
        cfg = dict(DEFAULT, required=["lint", "test"])
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pass"), check("test", "skipping")], cfg)
        # THEN READY, the skipped check still listed as required
        self.assertEqual(s["verdict"], "READY")
        self.assertEqual([c["bucket"] for c in s["required"]], ["pass", "skipping"])

    def test_missing_required_check_is_waiting_and_named(self):
        """A configured required check that has not appeared is WAITING and listed in missing_required.

        Checks can take minutes to appear; reading "everything present passed" as green would skip them.
        """
        # GIVEN a required e2e check absent from an otherwise green PR
        cfg = dict(REQ, required=["lint", "e2e"])
        # WHEN judged
        s = pr.verdict(14580, load("view_14580.json"), load("checks_14580.json"), cfg)
        # THEN WAITING, e2e named
        self.assertEqual(s["verdict"], "WAITING")
        self.assertEqual(s["missing_required"], ["e2e"])

    def test_without_config_every_non_skipped_check_is_required(self):
        """With no required-checks line, every non-skipped check is required; skipped ones are left out."""
        # GIVEN the recorded passing PR and no config
        checks = load("checks_14580.json")
        # WHEN judged
        s = pr.verdict(14580, load("view_14580.json"), checks, DEFAULT)
        # THEN READY, required = the non-skipped checks
        self.assertEqual(s["verdict"], "READY")
        self.assertEqual(len(s["required"]), sum(c["bucket"] != "skipping" for c in checks))

    def test_without_config_any_failure_is_failed(self):
        """With no config, the recorded govulncheck failure fails the PR."""
        # GIVEN the recorded failing PR and no config
        # WHEN judged
        s = pr.verdict(14665, load("view_14665.json"), load("checks_14665.json"), DEFAULT)
        # THEN FAILED
        self.assertEqual(s["verdict"], "FAILED")

    def test_merged_pr_is_merged_with_checks_as_they_were(self):
        """A merged PR is MERGED (exit 0) whatever GitHub's merge state says (UNKNOWN after merge)."""
        # GIVEN a merged PR with merge state UNKNOWN and no checks
        # WHEN judged
        s = pr.verdict(42, view(state="MERGED", merge="UNKNOWN"), [], DEFAULT)
        # THEN MERGED, lists empty
        self.assertEqual((s["verdict"], s["required"], s["advisory"]), ("MERGED", [], []))
        self.assertEqual(pr.EXIT["MERGED"], Exit.OK)

    def test_merged_pr_with_real_checks_lists_its_required_checks(self):
        """A merged PR keeps its checks in the status: required is the checks it merged with, not empty."""
        # GIVEN the recorded passing PR, now merged
        merged = dict(load("view_14580.json"), state="MERGED", mergeStateStatus="UNKNOWN")
        # WHEN judged with two required checks
        s = pr.verdict(14580, merged, load("checks_14580.json"), REQ)
        # THEN MERGED with both required checks listed
        self.assertEqual(s["verdict"], "MERGED")
        self.assertEqual(sorted(c["name"] for c in s["required"]), ["govulncheck", "lint"])

    def test_unknown_merge_state_on_an_open_pr_is_waiting(self):
        """UNKNOWN (GitHub still computing mergeability) is WAITING, not READY, even with every check passed."""
        # GIVEN passing checks and UNKNOWN on an open PR
        # WHEN judged
        s = pr.verdict(42, view(merge="UNKNOWN"), [check("lint", "pass")], DEFAULT)
        # THEN WAITING, blocked on UNKNOWN
        self.assertEqual((s["verdict"], s["blocked_on"]), ("WAITING", "UNKNOWN"))

    def test_has_hooks_with_passing_checks_is_ready(self):
        """HAS_HOOKS is GitHub's clean state for a repo with merge hooks: READY like CLEAN."""
        # GIVEN passing checks and HAS_HOOKS
        # WHEN judged
        s = pr.verdict(42, view(merge="HAS_HOOKS"), [check("lint", "pass")], DEFAULT)
        # THEN READY, no blocked_on
        self.assertEqual(s["verdict"], "READY")
        self.assertNotIn("blocked_on", s)

    def test_closed_pr_is_closed_exit_1(self):
        """A closed, unmerged PR is CLOSED (exit 1): it will never go green."""
        # GIVEN a closed PR with passing checks
        # WHEN judged
        s = pr.verdict(42, view(state="CLOSED", merge="UNKNOWN"), [check("lint", "pass")], DEFAULT)
        # THEN CLOSED, red
        self.assertEqual(s["verdict"], "CLOSED")
        self.assertEqual(pr.EXIT["CLOSED"], Exit.FAIL)

    def test_dirty_merge_state_is_conflict(self):
        """mergeStateStatus DIRTY (conflicts) is CONFLICT, exit 1: no amount of waiting fixes it."""
        # GIVEN passing checks and a DIRTY merge state
        # WHEN judged
        s = pr.verdict(42, view(merge="DIRTY"), [check("lint", "pass")], DEFAULT)
        # THEN CONFLICT
        self.assertEqual(s["verdict"], "CONFLICT")
        self.assertEqual(pr.EXIT["CONFLICT"], Exit.FAIL)

    def test_blocked_merge_state_with_passing_checks_is_waiting(self):
        """BLOCKED with all checks passing (e.g. a review outstanding) is WAITING, never READY."""
        # GIVEN passing checks and BLOCKED
        # WHEN judged
        s = pr.verdict(42, view(merge="BLOCKED"), [check("lint", "pass")], DEFAULT)
        # THEN WAITING, naming the merge state that holds it
        self.assertEqual(s["verdict"], "WAITING")
        self.assertEqual(s["blocked_on"], "BLOCKED")

    def test_waiting_on_a_check_has_no_blocked_on(self):
        """blocked_on appears only when every required check passed: a pending check is the reason otherwise."""
        # GIVEN a pending required check under BLOCKED
        # WHEN judged
        s = pr.verdict(42, view(merge="BLOCKED"), [check("lint", "pending")], DEFAULT)
        # THEN WAITING without blocked_on
        self.assertEqual(s["verdict"], "WAITING")
        self.assertNotIn("blocked_on", s)

    def test_cancelled_required_check_is_failed(self):
        """A cancelled required check is FAILED: it will not finish by itself."""
        # GIVEN a cancelled required check
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "cancel")], DEFAULT)
        # THEN FAILED
        self.assertEqual(s["verdict"], "FAILED")

    def test_preview_url_is_the_named_checks_link(self):
        """preview_url is the details link of the check named on the preview-check line, else null."""
        # GIVEN a preview check and the config naming it
        checks = [check("lint", "pass"), check("deploy-preview", "pass", "https://preview.example/42")]
        cfg = dict(DEFAULT, preview="deploy-preview")
        # WHEN judged with and without the config line
        with_line = pr.verdict(42, view(), checks, cfg)
        without = pr.verdict(42, view(), checks, DEFAULT)
        # THEN only the configured run carries the link
        self.assertEqual(with_line["preview_url"], "https://preview.example/42")
        self.assertIsNone(without["preview_url"])

    def test_output_has_the_documented_keys(self):
        """The status object carries exactly the documented keys, so background logs parse the same way."""
        # GIVEN any PR
        # WHEN judged
        s = pr.verdict(42, view(), [check("lint", "pass")], DEFAULT)
        # THEN the keys match the contract
        self.assertEqual(set(s), {"pr", "url", "head", "merge_state", "verdict", "required", "advisory",
                                  "missing_required", "preview_url"})
        self.assertEqual(s["required"], [{"name": "lint", "bucket": "pass", "link": "https://ci.example/lint"}])


class Config(unittest.TestCase):
    """config(): the three optional lines under `## coordinator` in project-rules.md."""

    def test_lines_under_coordinator_are_read(self):
        """required/advisory lists split on commas; preview-check is one name; bullets and backticks are fine."""
        # GIVEN a rules file with the lines under ## coordinator
        text = ("# Rules\n## implementer\nrequired-checks: wrong\n## coordinator\nSome prose.\n"
                "- `required-checks:` test, lint/typecheck , Preview Deploy\nadvisory-checks: bot-review\n"
                "preview-check: Preview Deploy\n## reviewer\nadvisory-checks: wrong\n")
        # WHEN parsed
        cfg = pr.config(text)
        # THEN only the coordinator section counts
        self.assertEqual(cfg, {"required": ["test", "lint/typecheck", "Preview Deploy"],
                               "advisory": ["bot-review"], "preview": "Preview Deploy"})

    def test_missing_lines_mean_every_check_required(self):
        """No required-checks line -> required None (every non-skipped check), no advisory, no preview."""
        # GIVEN a rules file without the lines, and no file at all
        # WHEN parsed
        # THEN both give the default
        self.assertEqual(pr.config("## coordinator\nprose\n"), DEFAULT)
        self.assertEqual(pr.config(""), DEFAULT)


class Wait(unittest.TestCase):
    """wait_loop(): poll, sleep, stop on a verdict, the cap, or no checks past the grace."""

    def run_wait(self, results, cap=2400, every=60, grace=300, lefts=None):
        clock, lines, it = FakeClock(), [], iter(results)

        def poll(left):
            if lefts is not None:
                lefts.append(left)
            r = next(it, results[-1])
            if isinstance(r, Exception):
                raise r
            return r
        code = pr.wait_loop(42, poll, cap, every, grace, clock=clock, sleep=clock.sleep, out=lines.append)
        return code, lines, clock

    def s(self, v, head="h1"):
        return {"pr": 42, "head": head, "verdict": v}

    def test_ready_on_first_poll_exits_0(self):
        """A green PR exits 0 after one poll and one JSON line, no sleep."""
        # GIVEN READY
        # WHEN waited
        code, lines, clock = self.run_wait([self.s("READY")])
        # THEN 0, one line, no sleep
        self.assertEqual((code, len(lines), clock.sleeps), (Exit.OK, 1, []))

    def test_failed_exits_1_within_one_poll(self):
        """A red PR exits 1 on the first poll: the caller diagnoses instead of waiting out the cap."""
        # GIVEN FAILED
        # WHEN waited
        code, lines, _ = self.run_wait([self.s("FAILED")])
        # THEN 1 after one line
        self.assertEqual((code, len(lines)), (Exit.FAIL, 1))

    def test_cap_exits_4_after_polling_every_interval(self):
        """Never green: polls every --every seconds and exits 4 at the cap (failure-to-conclude)."""
        # GIVEN WAITING forever, cap 120 s, every 60 s
        # WHEN waited
        lefts = []
        code, lines, clock = self.run_wait([self.s("WAITING")], cap=120, every=60, lefts=lefts)
        # THEN polls at 0, 60, 120 then exit 4, never sleeping past the cap; each poll is told the time left
        self.assertEqual(code, Exit.TIMEOUT)
        self.assertEqual(len(lines), 3)
        self.assertEqual(clock.sleeps, [60, 60])
        self.assertEqual(lefts, [120, 60, 0])

    def test_zero_cap_exits_4_after_one_poll(self):
        """--cap 0s still polls once and prints the line, then exits 4."""
        # GIVEN WAITING and cap 0
        # WHEN waited
        code, lines, _ = self.run_wait([self.s("WAITING")], cap=0)
        # THEN one line, 4
        self.assertEqual((code, len(lines)), (Exit.TIMEOUT, 1))

    def test_head_change_prints_a_line_and_restarts_the_wait(self):
        """A new push mid-wait prints a head_changed line and gives the new head a full cap.

        The old head's checks no longer matter; without the restart a late fix would time out.
        """
        # GIVEN h1 waiting, then h2 waiting twice, then h2 ready; cap 100 s
        res = [self.s("WAITING"), self.s("WAITING", "h2"), self.s("WAITING", "h2"), self.s("READY", "h2")]
        # WHEN waited
        code, lines, clock = self.run_wait(res, cap=100, every=60)
        # THEN head_changed is printed and the wait ends READY at t=160, past the first head's cap
        self.assertIn({"pr": 42, "event": "head_changed", "from": "h1", "to": "h2"}, lines)
        self.assertEqual(code, Exit.OK)
        self.assertEqual(clock.t, 160)

    def test_no_checks_past_the_grace_exits_3(self):
        """Checks that never appear exit 3 after --appear-grace, not after the 40-minute cap."""
        # GIVEN NO_CHECKS forever, grace 300 s
        # WHEN waited
        code, _, clock = self.run_wait([self.s("NO_CHECKS")], grace=300)
        # THEN blocked at the grace
        self.assertEqual(code, Exit.BLOCKED)
        self.assertEqual(clock.t, 300)

    def test_head_change_restarts_the_appear_grace(self):
        """A push while no checks show gives the new head's checks a full grace to appear."""
        # GIVEN h1 with no checks until t=240, then h2 with no checks from t=300 on; grace 300
        res = [self.s("NO_CHECKS")] * 5 + [self.s("NO_CHECKS", "h2")]
        # WHEN waited every 60 s
        code, _, clock = self.run_wait(res, grace=300)
        # THEN blocked 300 s after the head changed at t=300, not at t=300 itself
        self.assertEqual(code, Exit.BLOCKED)
        self.assertEqual(clock.t, 600)

    def test_checks_appearing_within_the_grace_keep_waiting(self):
        """NO_CHECKS for a minute, then checks show up and pass: exit 0, not 3."""
        # GIVEN NO_CHECKS then READY
        # WHEN waited
        code, _, _ = self.run_wait([self.s("NO_CHECKS"), self.s("READY")])
        # THEN 0
        self.assertEqual(code, Exit.OK)

    def test_a_gh_error_after_the_first_poll_is_logged_and_polling_continues(self):
        """A network blip mid-wait prints an error line and keeps polling; it does not end a 40-minute wait."""
        # GIVEN WAITING, a gh error, then READY
        res = [self.s("WAITING"), pr.GhError("gh pr view 42: timeout"), self.s("READY")]
        # WHEN waited
        code, lines, _ = self.run_wait(res)
        # THEN the error is a line and the wait ends READY
        self.assertIn({"pr": 42, "error": "gh pr view 42: timeout"}, lines)
        self.assertEqual(code, Exit.OK)

    def test_a_gh_error_on_the_first_poll_raises(self):
        """A first poll that fails (wrong PR number, no auth) stops at once instead of retrying for 40 minutes."""
        # GIVEN a failing first poll
        # WHEN waited / THEN GhError propagates
        with self.assertRaises(pr.GhError):
            self.run_wait([pr.GhError("gh pr view 42: not found")])


class CliCase(unittest.TestCase):
    """A project with a fake gh on PATH; set_pr() writes what gh will report."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-pr-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = make_project(self.tmp / "proj")
        self.gh = self.tmp / "gh-data"
        self.gh.mkdir()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(FAKE_GH)
        (bin_ / "gh").chmod(0o755)
        self.env = {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_GH": str(self.gh)}

    def set_pr(self, view_obj, checks=None):
        (self.gh / "view.json").write_text(json.dumps(view_obj))
        if checks is not None:
            (self.gh / "checks.json").write_text(json.dumps(checks))

    def rules(self, text):
        (self.p / ".claude/project-rules.md").write_text(text)

    def cli(self, *args):
        return agent_cli(self.p, "pr", *args, env=self.env)

    def calls(self):
        f = self.gh / "calls.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def merges(self):
        f = self.gh / "merge.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


class StatusCli(CliCase):
    def test_status_prints_one_json_line_and_exits_by_verdict(self):
        """`pr status` prints one JSON object and exits 0 READY, 1 FAILED, 5 WAITING, read via project-rules.md."""
        # GIVEN the project's required checks
        self.rules("## coordinator\nrequired-checks: lint, govulncheck\n")
        cases = [("14580", Exit.OK, "READY"), ("14665", Exit.FAIL, "FAILED")]
        for n, code, verdict in cases:
            # WHEN status runs on each recorded PR
            self.set_pr(load(f"view_{n}.json"), load(f"checks_{n}.json"))
            r = self.cli("status", n)
            # THEN one line, the verdict and its exit code
            with self.subTest(pr=n):
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertEqual(len(r.stdout.splitlines()), 1)
                self.assertEqual(json.loads(r.stdout)["verdict"], verdict)

    def test_status_waiting_exits_5(self):
        """WAITING is partial (exit 5): the PR is neither green nor red yet."""
        # GIVEN a pending required build
        self.rules("## coordinator\nrequired-checks: build (windows-latest)\n")
        self.set_pr(load("view_14665.json"), load("checks_14665.json"))
        # WHEN status runs
        r = self.cli("status", "14665")
        # THEN 5
        self.assertEqual(r.returncode, Exit.PARTIAL, r.stderr)

    def test_merged_pr_on_a_repo_without_checks_is_merged(self):
        """gh's "no checks reported" (exit 1) on a merged PR reads as no checks: MERGED, exit 0, no crash."""
        # GIVEN a merged PR and gh reporting no checks
        self.set_pr(view(state="MERGED", merge="UNKNOWN"))
        # WHEN status runs
        r = self.cli("status", "42")
        out = json.loads(r.stdout)
        # THEN MERGED with empty lists
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out["verdict"], out["required"], out["missing_required"]), ("MERGED", [], []))

    def test_closed_pr_exits_1(self):
        """A closed, unmerged PR is CLOSED and exits 1 through the CLI: red, read `verdict`."""
        # GIVEN a closed PR with passing checks
        self.set_pr(view(state="CLOSED", merge="UNKNOWN"), [check("lint", "pass")])
        # WHEN status runs
        r = self.cli("status", "42")
        # THEN 1, verdict CLOSED
        self.assertEqual(r.returncode, Exit.FAIL, r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "CLOSED")

    def test_repo_flag_reaches_both_gh_calls_once_each(self):
        """`status 42 --repo o/r` makes exactly one gh pr view and one gh pr checks, both with --repo o/r.

        A missing --repo on either call reads the wrong repo's PR 42; extra calls spend the shared API budget.
        """
        # GIVEN a READY PR
        self.set_pr(view(), [check("lint", "pass")])
        # WHEN status runs with --repo
        r = self.cli("status", "42", "--repo", "o/r")
        # THEN two gh calls, view then checks, each ending in --repo o/r
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertEqual([c[:3] for c in calls], [["pr", "view", "42"], ["pr", "checks", "42"]])
        for c in calls:
            self.assertEqual(c[-2:], ["--repo", "o/r"])

    def test_gh_failure_exits_3_with_gh_message(self):
        """A failing gh pr view (no such PR, no auth) exits 3 and passes gh's message on."""
        # GIVEN gh failing
        (self.gh / "view.err").write_text("GraphQL: Could not resolve to a PullRequest with the number of 9.\n")
        # WHEN status runs
        r = self.cli("status", "9")
        # THEN blocked, gh's reason shown
        self.assertEqual(r.returncode, Exit.BLOCKED)
        self.assertIn("Could not resolve to a PullRequest", r.stderr)

    def test_help_has_examples_for_each_verb(self):
        """Each pr verb's --help ends with an Examples: block (R2), and bare `pr` prints the group help."""
        # GIVEN the three verbs
        for verb in ("status", "wait", "land"):
            # WHEN help is asked
            r = self.cli(verb, "--help")
            # THEN examples are shown
            with self.subTest(verb=verb):
                self.assertEqual(r.returncode, 0)
                self.assertIn("Examples:\n  agent-cli pr " + verb, r.stdout)
        self.assertIn("status", self.cli().stdout)


class WaitCli(CliCase):
    def test_wait_on_a_green_pr_exits_0(self):
        """`pr wait` on a green PR prints the status line and exits 0 at once."""
        # GIVEN a READY PR
        self.set_pr(view(), [check("lint", "pass")])
        # WHEN waited
        r = self.cli("wait", "42", "--cap", "2m")
        # THEN 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "READY")

    def test_wait_with_zero_cap_on_a_waiting_pr_exits_4(self):
        """`--cap 0s` on a pending PR exits 4 after printing one line."""
        # GIVEN a pending PR
        self.set_pr(view(), [check("lint", "pending")])
        # WHEN waited with cap 0
        r = self.cli("wait", "42", "--cap", "0s")
        # THEN 4
        self.assertEqual(r.returncode, Exit.TIMEOUT, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1)

    def test_no_checks_with_zero_grace_exits_3(self):
        """gh reporting no checks with --appear-grace 0s exits 3 at once with "no checks appeared"."""
        # GIVEN an open PR and no checks.json (gh's "no checks reported")
        self.set_pr(view())
        # WHEN waited with no grace
        r = self.cli("wait", "42", "--appear-grace", "0s")
        # THEN blocked with the message and an example
        self.assertEqual(r.returncode, Exit.BLOCKED, r.stderr)
        self.assertIn("no checks appeared", r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "NO_CHECKS")

    def test_gh_call_is_cut_at_the_time_left(self):
        """A hung gh call cannot outlive the cap: with --cap 0s it is killed after 1 s (max(1, left)), not 120 s."""
        # GIVEN gh that takes 8 s to answer
        self.set_pr(view(), [check("lint", "pass")])
        (self.gh / "slow").write_text("8")
        # WHEN waited with no cap
        t = time.monotonic()
        r = self.cli("wait", "42", "--cap", "0s")
        took = time.monotonic() - t
        # THEN the first poll times out (exit 3, gh's timeout named) well before gh would answer
        self.assertEqual(r.returncode, Exit.BLOCKED, r.stderr)
        self.assertIn("timed out", r.stderr)
        self.assertLess(took, 6)

    def test_every_below_30_seconds_is_refused(self):
        """--every under 30 s is refused (exit 2): GitHub's API budget is shared by every session."""
        # GIVEN a PR
        self.set_pr(view(), [check("lint", "pending")])
        # WHEN waited every 5 s
        r = self.cli("wait", "42", "--every", "5")
        # THEN refused with an example
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("example:", r.stderr)

    def test_bad_cap_is_refused(self):
        """An unparseable --cap fails fast with exit 2 and an example (R3)."""
        # GIVEN a PR
        self.set_pr(view(), [check("lint", "pass")])
        # WHEN the cap is garbage
        r = self.cli("wait", "42", "--cap", "forty")
        # THEN refused
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("example:", r.stderr)


class LandCli(CliCase):
    def test_land_refuses_a_pr_that_is_not_ready(self):
        """`pr land --yes` on a non-READY PR exits 2 and never calls gh pr merge."""
        # GIVEN a pending PR
        self.set_pr(view(), [check("lint", "pending")])
        # WHEN landed
        r = self.cli("land", "42", "--yes")
        # THEN refused, nothing merged
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("WAITING", r.stderr)
        self.assertEqual(self.merges(), [])

    def test_land_refuses_an_already_merged_pr(self):
        """A merged PR is refused with "already merged" (exit 2), so a repeat land is harmless."""
        # GIVEN a merged PR
        self.set_pr(view(state="MERGED", merge="UNKNOWN"))
        # WHEN landed as a dry run
        r = self.cli("land", "42", "--dry-run")
        # THEN refused with the reason
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("already merged", r.stderr)

    def test_land_refuses_a_short_verified_head(self):
        """--verified-head under 7 hex characters that git cannot resolve is refused (2) before any gh call."""
        # GIVEN a READY PR
        self.set_pr(view(), [check("lint", "pass")])
        # WHEN landed with a 4-character head
        r = self.cli("land", "42", "--verified-head", "abcd", "--yes")
        # THEN refused with an example, gh never called
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("at least 7 hex characters", r.stderr)
        self.assertIn("example: agent-cli pr land 42 --verified-head", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_land_without_yes_is_a_dry_run(self):
        """Without --yes, land says what it would merge and merges nothing."""
        # GIVEN a READY PR
        self.set_pr(view(), [check("lint", "pass")])
        # WHEN landed without --yes
        r = self.cli("land", "42")
        # THEN exit 0, would merge, gh merge never called
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["landed"], "would merge")
        self.assertEqual(self.merges(), [])

    def test_land_yes_merges_the_checked_head_without_auto(self):
        """With --yes, land merges pinned to the head it checked (--match-head-commit), never with --auto.

        Pinning the head means a push between check and merge makes GitHub refuse the merge.
        """
        # GIVEN a READY PR
        self.set_pr(view(head="b" * 40), [check("lint", "pass")])
        # WHEN landed with squash
        r = self.cli("land", "42", "--method", "squash", "--yes")
        # THEN one merge call with the pinned head, no --auto
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["landed"], "merged")
        self.assertEqual(self.merges(), [["pr", "merge", "42", "--squash", "--match-head-commit", "b" * 40]])


class PatchId(CliCase):
    """--verified-head: a rebased head is landed only when its patch equals the verified one."""

    def setUp(self):
        super().setUp()
        self.git = lambda *a: run(GIT + list(a), self.p).stdout.strip()
        run(GIT + ["init", "-q", "--bare", str(self.tmp / "origin.git")], self.tmp)
        self.commit("base.txt", "base\n")
        self.git("remote", "add", "origin", str(self.tmp / "origin.git"))
        self.git("push", "-q", "-u", "origin", "main")
        self.git("checkout", "-q", "-b", "feat")
        self.commit("feature.txt", "v1\n")
        self.verified = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        self.commit("other.txt", "main moved\n")
        self.git("push", "-q", "origin", "main")

    def commit(self, name, text):
        (self.p / name).write_text(text)
        self.git("add", name)
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD")

    def head_on_main(self, text):
        """A new PR head: the feature re-made on top of moved main, with this feature.txt content."""
        self.git("checkout", "-q", "-B", "feat2", "main")
        sha = self.commit("feature.txt", text)
        self.git("checkout", "-q", "main")
        return sha

    def test_rebased_head_with_the_same_patch_lands(self):
        """A rebase that only moved the base (same patch-id) passes the re-check."""
        # GIVEN the verified feature rebased onto moved main, same content
        head = self.head_on_main("v1\n")
        self.set_pr(view(head=head), [check("lint", "pass")])
        # WHEN landed against the verified head
        r = self.cli("land", "42", "--verified-head", self.verified)
        # THEN allowed (dry run)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["landed"], "would merge")

    def test_rebased_head_with_changed_content_is_refused(self):
        """A rebase that changed the content (different patch-id) is refused: what was verified is not what lands."""
        # GIVEN a new head with different feature content
        head = self.head_on_main("v2, changed after review\n")
        self.set_pr(view(head=head), [check("lint", "pass")])
        # WHEN landed with --yes against the verified head
        r = self.cli("land", "42", "--verified-head", self.verified, "--yes")
        # THEN refused with a re-verify step as the example, never a land of the new head; nothing merged
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("re-verify", r.stderr)
        self.assertIn(f"example: git diff {self.verified[:12]} {head[:12]}", r.stderr)
        self.assertNotIn("--yes", r.stderr)
        self.assertEqual(self.merges(), [])

    def test_a_ref_resolving_to_the_verified_head_is_accepted(self):
        """--verified-head may be a ref (here the feature branch) that resolves to a full sha."""
        # GIVEN the verified feature rebased onto moved main, same content
        head = self.head_on_main("v1\n")
        self.set_pr(view(head=head), [check("lint", "pass")])
        # WHEN landed against the branch name
        r = self.cli("land", "42", "--verified-head", "feat")
        # THEN allowed (dry run)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
