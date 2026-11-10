"""Tests for `agent-cli pr --rest` (and CLAUDE_CODE_REMOTE): the same verdicts and lands through GitHub REST, and
required checks taken from the PR's own repo. A fake `gh` on PATH replays REST objects (see helpers.FAKE_GH)."""
import json
import sys
import unittest

from helpers import CLAUDE, GIT, GhCase, check, rest_pull, run, run_row, set_env, view

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import pr  # noqa: E402
from agent_cli.core import Exit  # noqa: E402


class RestCli(GhCase):
    """`pr status|wait|land --rest` (and CLAUDE_CODE_REMOTE): the same verdicts read through GitHub REST, because
    `gh pr` GraphQL returns 403 in a cloud session."""

    def set_origin(self, slug):
        run(GIT + ["remote", "add", "origin", f"https://github.com/{slug}.git"], self.p)

    def test_status_rest_reads_only_rest_paths_and_matches_the_graphql_verdict(self):
        """`status --rest` makes no `gh pr` call, and prints the very status the GraphQL path prints for the same PR.

        Cloud sessions cannot call `gh pr`; a REST verdict that differed would make chains land on another bar.
        """
        # GIVEN one green PR, shown both as gh pr view/checks JSON and as REST objects
        self.set_pr(view(head="b" * 40), [check("lint", "pass", "https://ci.example/lint")])
        self.set_rest(rest_pull(head="b" * 40), [run_row("lint")])
        # WHEN status runs both ways
        gql = self.cli("status", "42", "--json", "--repo", "o/r")
        seen = len(self.calls())
        rest = self.cli("status", "42", "--json", "--repo", "o/r", "--rest")
        # THEN the same status, and the REST run called no gh pr
        self.assertEqual(rest.returncode, Exit.OK, rest.stderr)
        self.assertEqual(json.loads(rest.stdout), json.loads(gql.stdout))
        rest_calls = self.calls()[seen:]
        self.assertTrue(rest_calls and all(c[0] == "api" for c in rest_calls), rest_calls)

    def test_claude_code_remote_selects_rest_without_the_flag(self):
        """CLAUDE_CODE_REMOTE set means REST: a cloud session needs no flag, and runs no `gh pr` or `gh repo view`."""
        # GIVEN a REST-only gh (no view.json, so any gh pr view would crash the fake) and the cloud env var
        self.set_rest(rest_pull(), [run_row("lint")])
        self.env["CLAUDE_CODE_REMOTE"] = "1"
        # WHEN status runs with --repo
        r = self.cli("status", "42", "--json", "--repo", "o/r")
        # THEN READY, every call a gh api
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "READY")
        self.assertEqual({c[0] for c in self.calls()}, {"api"})

    def test_rest_without_repo_uses_the_cwd_origin(self):
        """With no --repo, the repo is the cwd's `origin` (read from git, never `gh repo view`, which is GraphQL)."""
        # GIVEN a checkout whose origin is o/r
        self.set_origin("o/r")
        self.set_rest(rest_pull(), [run_row("lint")])
        # WHEN status runs --rest without --repo
        r = self.cli("status", "42", "--json", "--rest")
        # THEN READY for o/r's PR 42
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertIn(["api", "repos/o/r/pulls/42"], self.calls())

    def test_rest_without_repo_or_origin_is_refused_with_an_example(self):
        """REST needs the repo in the path: no --repo and no origin exits 2 with the invocation to use."""
        # GIVEN a checkout with no origin
        # WHEN status runs --rest without --repo
        r = self.cli("status", "42", "--rest")
        # THEN refused, with the --repo example, gh never called
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertIn("--repo", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_check_runs_and_commit_statuses_map_to_gh_buckets(self):
        """Check runs and commit statuses become the pass/fail/pending/skipping/cancel buckets `gh pr checks` uses.

        The verdict code reads buckets only; a wrong mapping would turn red into green.
        """
        # GIVEN one row of each kind, none required by name (so all count)
        runs = [run_row("ok"), run_row("bad", "failure"), run_row("busy", None, "in_progress"),
                run_row("skip", "skipped"), run_row("stop", "cancelled"), run_row("meh", "neutral")]
        sts = [{"context": "s-ok", "state": "success"}, {"context": "s-bad", "state": "failure"},
               {"context": "s-err", "state": "error"}, {"context": "s-wait", "state": "pending"}]
        self.set_rest(rest_pull(), runs, sts)
        # WHEN fetched
        set_env(self, **self.env)
        _, checks = pr.fetch_rest(42, "o/r")
        # THEN the buckets
        self.assertEqual({c["name"]: c["bucket"] for c in checks},
                         {"ok": "pass", "bad": "fail", "busy": "pending", "skip": "skipping", "stop": "cancel",
                          "meh": "skipping", "s-ok": "pass", "s-bad": "fail", "s-err": "fail", "s-wait": "pending"})

    def test_pull_state_is_mapped_to_the_graphql_view(self):
        """merged -> MERGED, closed -> CLOSED, open -> OPEN; a fork head is cross-repository; mergeable_state is
        upper-cased; url and base ref carry over."""
        set_env(self, **self.env)
        for pull, want in [(rest_pull(state="closed", merged=True, mergeable_state="unknown"), ("MERGED", False)),
                           (rest_pull(state="closed"), ("CLOSED", False)),
                           (rest_pull(fork=True, mergeable_state="has_hooks"), ("OPEN", True))]:
            # GIVEN the REST pull
            self.set_rest(pull)
            # WHEN fetched
            v, _ = pr.fetch_rest(42, "o/r", sleep=lambda s: None)
            # THEN the view GraphQL would give
            self.assertEqual((v["state"], v["isCrossRepository"]), want)
            self.assertEqual((v["baseRefName"], v["url"]), ("main", "https://github.com/o/r/pull/42"))
        self.assertEqual(v["mergeStateStatus"], "HAS_HOOKS")

    def test_a_null_mergeable_state_is_reread_up_to_five_times_three_seconds_apart(self):
        """GitHub computes mergeable_state lazily (null on the first read): re-read up to 5 times, 3 s apart.

        Without the retry a fresh green PR reads UNKNOWN, and `land` refuses it as not READY.
        """
        set_env(self, **self.env)
        # GIVEN two null reads, then clean
        self.set_rest(rest_pull(), null_reads=2)
        naps = []
        # WHEN fetched
        v, _ = pr.fetch_rest(42, "o/r", sleep=naps.append)
        # THEN CLEAN after two 3 s naps
        self.assertEqual((v["mergeStateStatus"], naps), ("CLEAN", [3, 3]))
        # AND GIVEN it never settles
        (self.gh / "rest_reads").write_text("0")
        self.set_rest(rest_pull(), null_reads=99)
        naps.clear()
        # THEN 5 reads, 4 naps, UNKNOWN
        v, _ = pr.fetch_rest(42, "o/r", sleep=naps.append)
        self.assertEqual((v["mergeStateStatus"], naps), ("UNKNOWN", [3, 3, 3, 3]))

    def test_gh_api_failure_is_a_gh_error(self):
        """A failing gh api (a 403 on the pull) raises GhError, so status exits 3 with gh's message."""
        # GIVEN every gh api failing
        (self.gh / "api.err").write_text("gh: HTTP 403 forbidden")
        # WHEN status runs --rest
        r = self.cli("status", "42", "--rest", "--repo", "o/r")
        # THEN exit 3 with the message
        self.assertEqual(r.returncode, Exit.BLOCKED)
        self.assertIn("HTTP 403", r.stderr)

    def test_land_rest_puts_the_merge_pinned_to_the_checked_head(self):
        """`land --rest --yes` merges with PUT pulls/N/merge, sha = the head judged and merge_method as asked.

        The sha keeps the head guard `--match-head-commit` gives on the GraphQL path.
        """
        # GIVEN a READY PR
        self.set_rest(rest_pull(head="c" * 40), [run_row("lint")])
        # WHEN landed with squash
        r = self.cli("land", "42", "--method", "squash", "--yes", "--repo", "o/r", "--rest")
        # THEN one PUT, no gh pr merge, the usual output line
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual(self.merges(), [["api", "-X", "PUT", "repos/o/r/pulls/42/merge", "-f", "sha=" + "c" * 40,
                                          "-f", "merge_method=squash"]])
        self.assertEqual(json.loads(r.stdout)["landed"], "merged")

    def test_land_rest_without_yes_merges_nothing(self):
        """A dry run (no --yes) over REST says "would merge" and sends no PUT."""
        # GIVEN a READY PR
        self.set_rest(rest_pull(), [run_row("lint")])
        # WHEN landed without --yes
        r = self.cli("land", "42", "--repo", "o/r", "--rest")
        # THEN would merge, nothing sent
        self.assertEqual(json.loads(r.stdout)["landed"], "would merge")
        self.assertEqual(self.merges(), [])

    def test_land_rest_refuses_a_pr_that_is_not_ready(self):
        """Over REST, a pending check still refuses the land (exit 2) and sends no PUT."""
        # GIVEN a running check
        self.set_rest(rest_pull(), [run_row("lint", None, "in_progress")])
        # WHEN landed
        r = self.cli("land", "42", "--yes", "--repo", "o/r", "--rest")
        # THEN refused
        self.assertEqual(r.returncode, Exit.REFUSED)
        self.assertEqual(self.merges(), [])

    def test_land_rest_merge_failure_exits_1_with_the_message(self):
        """A refused PUT (head moved: 409) exits 1 with GitHub's message."""
        # GIVEN a READY PR whose merge GitHub refuses
        self.set_rest(rest_pull(), [run_row("lint")])
        (self.gh / "merge.err").write_text("gh: Head branch was modified (HTTP 409)")
        # WHEN landed
        r = self.cli("land", "42", "--yes", "--repo", "o/r", "--rest")
        # THEN exit 1
        self.assertEqual(r.returncode, Exit.FAIL)
        self.assertIn("HTTP 409", r.stderr)

    def test_wait_rest_on_a_green_pr_exits_0(self):
        """`wait --rest` polls REST and exits 0 on READY, as the GraphQL wait does."""
        # GIVEN a green PR
        self.set_rest(rest_pull(), [run_row("lint")])
        # WHEN waited with a zero cap
        r = self.cli("wait", "42", "--cap", "0s", "--json", "--repo", "o/r", "--rest")
        # THEN READY
        self.assertEqual(r.returncode, Exit.OK, r.stderr)
        self.assertEqual(json.loads(r.stdout)["verdict"], "READY")


class TargetChecksCli(GhCase):
    """Required checks come from the PR's own repo: a PR in another repo is never judged by the cwd repo's list."""

    PRODUCT = "## coordinator\nrequired-checks: test, quality, Vercel\nadvisory-checks: CodeRabbit\n"
    DESK = [run_row("check"), run_row("GitGuardian Security Checks")]

    def setUp(self):
        super().setUp()
        self.rules(self.PRODUCT)
        run(GIT + ["remote", "add", "origin", "https://github.com/acme/product.git"], self.p)

    def status(self, *args):
        r = self.cli("status", "7", "--json", "--repo", "acme/desk", *args)
        return r, json.loads(r.stdout)

    def test_another_repos_pr_is_not_judged_by_the_product_list_over_rest(self):
        """The bug: `status --repo acme/desk` listed the product's test/quality/Vercel as missing_required.

        A desk PR with its own `check` green stayed WAITING forever, so chains could never land it.
        """
        # GIVEN the product rules in the cwd repo (acme/product) and a green desk PR with no protection to read
        self.set_rest(rest_pull(), self.DESK)
        # WHEN status runs for acme/desk
        r, s = self.status("--rest")
        # THEN READY with nothing missing: every reported check counts
        self.assertEqual((r.returncode, s["verdict"], s["missing_required"]), (Exit.OK, "READY", []))
        self.assertEqual(sorted(c["name"] for c in s["required"]), ["GitGuardian Security Checks", "check"])

    def test_branch_protection_of_the_target_names_the_required_checks(self):
        """Over REST the target's branch protection is the required set: `check` only, GitGuardian advisory in effect.

        A repo can report checks it does not require; protection is what its merge button waits for.
        """
        # GIVEN protection requiring `check`, and GitGuardian still running
        self.set_rest(rest_pull(), [run_row("check"), run_row("GitGuardian Security Checks", None, "in_progress")],
                      protection={"contexts": [], "checks": [{"context": "check", "app_id": 15368}]})
        # WHEN status runs
        r, s = self.status("--rest")
        # THEN READY on `check` alone, read from the base branch's protection
        self.assertEqual(s["verdict"], "READY")
        self.assertEqual([c["name"] for c in s["required"]], ["check"])
        self.assertIn(["api", "repos/acme/desk/branches/main/protection/required_status_checks", "--jq",
                       pr.PROTECTION_JQ], self.calls())

    def test_a_missing_protected_check_keeps_the_pr_waiting(self):
        """A check protection requires that has not reported is WAITING and named, never skipped."""
        # GIVEN protection requiring `check` and `e2e`, only `check` reported
        self.set_rest(rest_pull(), [run_row("check")], protection={"contexts": ["check", "e2e"]})
        # WHEN status runs
        r, s = self.status("--rest")
        # THEN WAITING on e2e
        self.assertEqual((r.returncode, s["missing_required"]), (Exit.PARTIAL, ["e2e"]))

    def test_rulesets_count_as_protection(self):
        """Required checks set by a repository ruleset (not classic protection) are read too."""
        # GIVEN no classic protection but a ruleset requiring `check`, GitGuardian running
        self.set_rest(rest_pull(), [run_row("check"), run_row("GitGuardian Security Checks", None, "queued")],
                      rules=[{"type": "pull_request"},
                             {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "check"}]}}])
        # WHEN status runs
        r, s = self.status("--rest")
        # THEN READY on `check`
        self.assertEqual((s["verdict"], [c["name"] for c in s["required"]]), ("READY", ["check"]))

    def test_unreadable_protection_falls_back_to_every_reported_check(self):
        """A 403 on protection (a token without admin) is not an error: all reported checks must pass instead."""
        # GIVEN protection answering 403 and a failing GitGuardian
        self.set_rest(rest_pull(), [run_row("check"), run_row("GitGuardian Security Checks", "failure")])
        (self.gh / "protection.err").write_text("gh: Resource not accessible (HTTP 403)")
        # WHEN status runs
        r, s = self.status("--rest")
        # THEN FAILED, since every reported check counts
        self.assertEqual((r.returncode, s["verdict"]), (Exit.FAIL, "FAILED"))

    def test_the_product_repo_keeps_its_own_lines_and_reads_no_protection(self):
        """A PR in the cwd repo is judged by the plain lines as before, and REST never reads its protection."""
        # GIVEN a product PR (acme/product) with the plain required list green
        self.set_rest(rest_pull(), [run_row("test"), run_row("quality"), run_row("Vercel")],
                      protection={"contexts": ["unrelated"]})
        # WHEN status runs for acme/product over REST
        r = self.cli("status", "7", "--json", "--repo", "acme/product", "--rest")
        # THEN READY by the plain list, no protection call
        self.assertEqual(json.loads(r.stdout)["verdict"], "READY", r.stderr)
        self.assertFalse([c for c in self.calls() if "protection" in " ".join(c)])

    def test_a_keyed_line_for_the_target_still_wins(self):
        """`required-checks[acme/desk]:` beats both the product list and protection."""
        # GIVEN a keyed line naming `check`, protection naming something else
        self.rules(self.PRODUCT + "required-checks[acme/desk]: check\n")
        self.set_rest(rest_pull(), self.DESK, protection={"contexts": ["other"]})
        # WHEN status runs
        r, s = self.status("--rest")
        # THEN judged by the keyed line
        self.assertEqual((s["verdict"], [c["name"] for c in s["required"]]), ("READY", ["check"]))

    def test_the_graphql_path_also_ignores_the_product_list_for_another_repo(self):
        """Without --rest (laptop), `--repo acme/desk` from the product checkout is not judged by the product list either.

        The laptop reads no protection (its extra API call is left out); every reported check counts.
        """
        # GIVEN a green desk PR as gh pr view/checks
        self.set_pr(view(), [check("check", "pass")])
        # WHEN status runs
        r, s = self.status()
        # THEN READY, nothing missing
        self.assertEqual((s["verdict"], s["missing_required"]), ("READY", []))
        self.assertFalse([c for c in self.calls() if "protection" in " ".join(c)])
