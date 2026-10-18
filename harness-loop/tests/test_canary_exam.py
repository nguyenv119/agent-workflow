"""Tests for loop.py exam --subscription-only (the canary) and exam-route --subscription-only.

They reuse test_loop's throwaway checkout (LoopRepo: fake gh, fake run_exam, fake preflight) through its module, so
discovery does not run test_loop's own tests twice. Docker is a fake command (EXAM_DOCKER) that logs each call; the
task fetch is a fake command (LOOP_FETCH_CACHE) that logs each call, so no test downloads anything.
"""
import contextlib
import io
import json
import os
import sys
import textwrap
import unittest
import unittest.mock

import test_loop as tl
from helpers import GIT, installed_tree, run

loop = tl.loop
DUMMY = "dummy-plan-value-1ik4"
REVIEWER = ".claude/skills/reviewer-tests/SKILL.md"
CAND_PASS, CAND_FAIL = tl.SPEC_PASS["candidate"], tl.SPEC_FAIL["candidate"]
MAIN = tl.SPEC_FAIL["baseline"]  # 4/5 solved
FAIL_TWICE = {"baseline": MAIN, "candidate": CAND_FAIL}
FAIL_THEN_PASS = {"baseline": MAIN, "candidate": CAND_FAIL, "candidate-2": MAIN}
ONE_FEWER = {"baseline": {t: [1, 2.0] for t in ("t1", "t2", "t3")},  # main 3/3, the candidate 2/3
             "candidate": {"t1": [1, 2.0], "t2": [1, 2.0], "t3": [0, 2.0]}}


class Canary(tl.LoopTestCase):
    """loop.py exam --pr 5 --subscription-only --post-status on refs/pull/5/head of the throwaway checkout."""

    def setUp(self):
        super().setUp()
        self.docker_log = self.r.tmp / "docker.log"
        self.fetch_log = self.r.tmp / "fetch.log"
        self.r.env.update({
            "CLAUDE_CODE_OAUTH_TOKEN": DUMMY, "EXAM_DOCKER": self.docker(0), "LOOP_FETCH_CACHE": self.fetcher(),
            # paid credentials present: the canary must never touch them
            "AWS_ACCESS_KEY_ID": "AKIAPLANTED", "AWS_SECRET_ACCESS_KEY": "planted-secret",
            "ANTHROPIC_API_KEY": "sk-planted", "CLAUDE_CODE_USE_BEDROCK": "1"})

    def docker(self, code):
        script = self.r.tmp / f"docker{code}.py"
        script.write_text(textwrap.dedent(f"""\
            import sys
            open({str(self.docker_log)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
            sys.exit({code})
            """))
        return f"{sys.executable} {script}"

    def fetcher(self):
        """LOOP_FETCH_CACHE: logs its argv (and whether Docker was already asked) and exits $FAKE_FETCH_EXIT."""
        script = self.r.tmp / "fetch.py"
        script.write_text(textwrap.dedent(f"""\
            import os, sys
            after_docker = os.path.exists({str(self.docker_log)!r})
            open({str(self.fetch_log)!r}, "a").write(" ".join(sys.argv[1:]) + f" docker-first={{after_docker}}\\n")
            sys.exit(int(os.environ.get("FAKE_FETCH_EXIT", "0")))
            """))
        return f"{sys.executable} {script}"

    def fetches(self):
        return self.fetch_log.read_text().splitlines() if self.fetch_log.is_file() else []

    def pr_head(self, files):
        run(GIT + ["checkout", "-q", "-b", "pr-5", self.r.base_sha], self.r.aw)
        for rel, text in files.items():
            (self.r.aw / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.r.aw / rel).write_text(text)
        run(GIT + ["add", "-A"], self.r.aw)
        run(GIT + ["commit", "-qm", "hand-made change"], self.r.aw)
        run(GIT + ["push", "-q", "origin", "HEAD:refs/pull/5/head"], self.r.aw)
        sha = run(GIT + ["rev-parse", "HEAD"], self.r.aw).stdout.strip()
        run(GIT + ["checkout", "-q", "main"], self.r.aw)
        return sha

    def reviewer_pr(self):
        return self.pr_head({REVIEWER: tl.BASE_FILES[REVIEWER].replace("every", "each")})

    def canary(self, head, *extra, spec=None, env=None):
        return self.r.loop("exam", "--pr", "5", "--tasks", str(self.r.manifest), "--subscription-only",
                           "--rerun-on-fail", "1", "--post-status", *extra,
                           env={"FAKE_GH_HEAD": head, "FAKE_EXAM_SPEC": json.dumps(spec or tl.SPEC_PASS),
                                **(env or {})})

    def statuses(self):
        """(repo/sha path, state, context, description) of each `gh api .../statuses/...` call."""
        out = []
        for c in self.r.gh_calls():
            a = c["args"]
            if a[:1] == ["api"]:
                f = dict(a[i + 1].split("=", 1) for i, x in enumerate(a) if x == "-f")
                out.append((a[1], f["state"], f["context"], f["description"]))
        return out

    def preflight_envs(self):
        f = self.r.tmp / "preflight.jsonl"
        return [json.loads(line)["env"] for line in f.read_text().splitlines()] if f.is_file() else []

    def runs(self, kind=None):
        runs = self.r.home / "runs"
        return sorted(d for d in runs.iterdir()
                      if kind is None or json.loads((d / "run.json").read_text())["kind"] == kind) \
            if runs.is_dir() else []

    def kinds(self):
        return [c[c.index("--kind") + 1] for c in self.r.exam_calls()]

    def assertNoPaidCredential(self):
        for env in self.preflight_envs():
            self.assertFalse([k for k in env if k.startswith("AWS_") or k == "ANTHROPIC_API_KEY"], env)

    def assertCouldNotRun(self, p, reason_start, head):
        """Exit 5 and exactly one status: error "could not run: <reason...>" on head in agent-workflow."""
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        (status,) = self.statuses()
        self.assertEqual(status[:3], (f"repos/{loop.GH_REPO}/statuses/{head}", "error", "harness-canary"))
        self.assertTrue(status[3].startswith(f"could not run: {reason_start}"), status)

    def test_pass_posts_success_on_the_pr_head_and_books_notional(self):
        """
        What: a candidate passing the screen exits 0, posts success "pass solved B/n vs main A/n" on the PR head,
        and its runs are booked on the subscription (usd 0, notional_usd kept).
        Why: the merge gate reads the status; the ledger must never count plan use as money.
        Breaks: a missing status blocks every harness PR; a money row eats the weekly cap.
        """
        # GIVEN a PR changing a reviewer skill, Bedrock and API keys in the env too
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head)
        # THEN exit 0, one success status on the head in agent-workflow, the score block written
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.statuses(), [(f"repos/{loop.GH_REPO}/statuses/{head}", "success", "harness-canary",
                                            "pass solved 4/5 vs main 3/5")])
        self.assertTrue([c for c in self.r.gh_calls() if c["args"][:2] == ["pr", "edit"]])
        # AND the PR view asked whether the head is a fork
        (view,) = [c["args"] for c in self.r.gh_calls() if c["args"][:2] == ["pr", "view"]][:1]
        self.assertIn("isCrossRepository", view[view.index("--json") + 1])
        # AND only the subscription was preflighted, with no paid key, and runs/rows are on the subscription
        self.assertEqual(len(self.preflight_envs()), 1)
        self.assertNoPaidCredential()
        rows = [(r["kind"], r["usd"], r.get("notional_usd"), r["route"]) for r in self.r.spend()]
        self.assertEqual(rows, [("exam-baseline", 0.0, 10.0, "subscription"),
                                ("exam-candidate", 0.0, 10.0, "subscription")])
        # AND the tasks were fetched once, for the manifest, after the Docker check
        self.assertEqual(self.fetches(), [f"{self.r.manifest} docker-first=True"])

    def test_fail_then_pass_on_the_rerun_is_a_pass(self):
        """
        What: a screen fail reruns the candidate once in a fresh run dir; the second compare is the verdict.
        Why: one flaky task must not block a PR. Breaks: the first result sticks, or the rerun reuses the dir.
        """
        # GIVEN a candidate that fails its first run and passes its second
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, spec=FAIL_THEN_PASS)
        # THEN exit 0, "pass (rerun)", two candidate run dirs
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("success", "harness-canary", "pass (rerun) solved 4/5 vs main 4/5")])
        self.assertEqual(len(self.runs("candidate")), 2)

    def test_fail_twice_exits_1_with_failure_and_keeps_both_candidate_runs(self):
        """
        What: two screen fails exit 1 with status failure "fail (rerun) solved B/n vs main A/n".
        Why: the merge gate must see a failure, and both runs stay for the reviewer.
        """
        # GIVEN a candidate that fails both runs
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, spec=FAIL_TWICE)
        # THEN exit 1, failure, both candidate run dirs, the verdict printed
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("failure", "harness-canary", "fail (rerun) solved 1/5 vs main 4/5")])
        self.assertEqual(len(self.runs("candidate")), 2)
        self.assertIn("canary: fail (rerun) solved 1/5 vs main 4/5", p.stdout)

    def test_one_fewer_solved_than_main_fails_after_the_rerun(self):
        """
        What: the canary passes only when the candidate solves at least as many tasks as main: 2/3 vs 3/3 fails,
        reruns once, and a second 2/3 exits 1 with "fail (rerun) solved 2/3 vs main 3/3".
        Why: on three tasks one lost task is a third of the signal; a harmful harness line scored 2/3 vs 3/3 and
        passed the round's slack-1 rule. Breaks: a regressing harness PR gets a green canary.
        """
        # GIVEN a candidate solving 2/3 on both runs against main's 3/3
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, spec=ONE_FEWER)
        # THEN exit 1, failure after one rerun
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("failure", "harness-canary", "fail (rerun) solved 2/3 vs main 3/3")])
        self.assertEqual(len(self.runs("candidate")), 2)

    def test_candidate_runs_early_stop_with_slack_0(self):
        """
        What: both candidate runs (first and rerun) get --early-stop-slack 0 next to --early-stop-vs.
        Why: the early stop must use the canary's rule; with slack 1 it would keep paying for a candidate the
        slack-0 screen already fails. Breaks: the fake run_exam cannot early-stop, so only the args show it.
        """
        # GIVEN a candidate that fails both runs
        head = self.reviewer_pr()
        # WHEN the canary runs
        self.canary(head, spec=ONE_FEWER)
        # THEN each candidate call carries slack 0
        cands = [c for c in self.r.exam_calls() if c[c.index("--kind") + 1] == "candidate"]
        self.assertEqual([c[c.index("--early-stop-slack") + 1] for c in cands], ["0", "0"])

    def test_rerun_whose_tries_all_errored_is_could_not_run(self):
        """
        What: a screen fail whose rerun errored on every try exits 5 with state error "could not run: the
        candidate's rerun errored", not "fail (rerun)".
        Why: a Docker or image-build failure on the rerun says nothing about the PR.
        Breaks: a good PR gets a red failure because the laptop's Docker died mid-rerun.
        """
        # GIVEN a candidate that fails its first run and whose rerun errors on every try
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, spec=FAIL_TWICE, env={"FAKE_EXAM_ERRORED": "candidate-2"})
        # THEN could not run, with both candidate runs kept
        self.assertCouldNotRun(p, "the candidate's rerun errored", head)
        self.assertEqual(len(self.runs("candidate")), 2)

    def test_early_stopped_rerun_is_a_fail_not_could_not_run(self):
        """
        What: a rerun that early-stops (incomplete, run.json early_stop set: it can no longer pass) is "fail
        (rerun)", exit 1, not "could not run".
        Why: an early stop is a verdict; only errored tries or a stop without one mean the run said nothing.
        Breaks: every clearly failing PR gets a retryable error instead of a failure.
        """
        # GIVEN a candidate that fails, and runs given --early-stop-vs that stop early
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, spec=FAIL_TWICE, env={"FAKE_EXAM_EARLY_STOP": "cannot pass"})
        # THEN failure, exit 1
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([s[1] for s in self.statuses()], ["failure"])
        self.assertTrue(self.statuses()[0][3].startswith("fail (rerun)"), self.statuses())

    def test_not_needed_posts_success_and_makes_no_run(self):
        """
        What: a PR touching nothing /work loads posts success "not needed (no loaded file)", with no run dir and
        no task fetch.
        Why: the gate requires the status on every PR, and fetching is exam setup.
        Breaks: a README PR waits on a laptop forever, or downloads the task set for nothing.
        """
        # GIVEN a PR touching only a file outside the loaded set
        head = self.pr_head({"harness-loop/notes.txt": "notes\n"})
        # WHEN the canary runs
        p = self.canary(head)
        # THEN success "not needed", no run, no preflight, no fetch
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("success", "harness-canary", "not needed (no loaded file)")])
        self.assertEqual((self.runs(), self.preflight_envs(), self.fetches()), ([], [], []))

    def test_laptop_only_exits_5_before_any_gh_call(self):
        """
        What: with CLAUDE_CODE_REMOTE set, exit 5 "laptop-only" before gh, git, preflight or Docker.
        Why: a cloud session has no Docker and must leave the PR for a laptop. Breaks: an error status there.
        """
        # GIVEN a PR that needs the exam
        head = self.reviewer_pr()
        # WHEN the canary runs in a cloud session
        p = self.canary(head, env={"CLAUDE_CODE_REMOTE": "true"})
        # THEN exit 5 with no gh call, preflight, run or Docker call
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("laptop-only", p.stderr)
        self.assertEqual((self.r.gh_calls(), self.preflight_envs(), self.runs()), ([], [], []))
        self.assertFalse(self.docker_log.exists())

    def test_fork_pr_exits_5_with_no_status_and_no_exam(self):
        """
        What: a cross-repository head exits 5 "fork PR", posts nothing, starts no preflight, Docker or exam.
        Why: the sandbox holds the plan token and runs the PR's harness; a fork could read it.
        Breaks: a fork PR's harness runs with the plan token. The fake gh answers only the --json fields asked
        for, so this test also fails if the PR view stops asking for isCrossRepository.
        """
        # GIVEN a PR whose head is in another repo
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"FAKE_GH_CROSS": "1"})
        # THEN exit 5, no status, preflight, exam or Docker call
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("fork PR, not run with the plan token", p.stderr)
        self.assertEqual((self.statuses(), self.preflight_envs(), self.r.exam_calls()), ([], [], []))
        self.assertFalse(self.docker_log.exists())

    def test_docker_down_posts_error_and_exits_5(self):
        """
        What: `docker info` failing posts state error "could not run: Docker is not running", exit 5, no run and
        no task fetch.
        Why: could-not-run is not a fail: callers retry later. Breaks: a red failure on a good PR.
        """
        # GIVEN a PR that needs the exam and Docker down
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"EXAM_DOCKER": self.docker(1)})
        # THEN state error, exit 5, one docker info call, nothing run or fetched
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("error", "harness-canary", "could not run: Docker is not running")])
        self.assertEqual((self.r.exam_calls(), self.fetches()), ([], []))
        self.assertEqual(self.docker_log.read_text(), "info\n")

    def test_task_fetch_failure_posts_error_and_exits_5(self):
        """
        What: the task fetch (fetch_cache.py, run after the gate, the preflight and Docker) failing posts state
        error "could not run: task fetch failed" and exits 5 with no run.
        Why: a network hiccup is no verdict on the PR. Breaks: a red failure, or an exam on a half-fetched set.
        """
        # GIVEN a PR that needs the exam and a fetch that fails
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"FAKE_FETCH_EXIT": "1"})
        # THEN could not run, after the preflight and the Docker check, with no exam run
        self.assertCouldNotRun(p, "task fetch failed", head)
        self.assertEqual(len(self.preflight_envs()), 1)
        self.assertEqual(self.r.exam_calls(), [])

    def test_exam_crash_posts_error_and_exits_5(self):
        """
        What: run_exam leaving no run (a RuntimeError inside loop.py) exits 5 with state error, not a traceback.
        Why: a crash is "could not run"; exit 1 would read as a failed PR. Breaks: red status on a good PR.
        """
        # GIVEN a PR that needs the exam and a run_exam that dies
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"LOOP_RUN_EXAM": f"{sys.executable} -c 'import sys; sys.exit(3)'"})
        # THEN could not run, and no traceback
        self.assertCouldNotRun(p, "", head)
        self.assertNotIn("Traceback", p.stderr)

    def test_gh_pr_view_failure_exits_5_with_no_status(self):
        """
        What: `gh pr view` failing in the canary exits 5 (could not run) with no status and no run.
        Why: no head sha is known yet, and gh trouble is not a PR failure. Breaks: a traceback and exit 1.
        """
        # GIVEN gh that cannot read the PR
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"FAKE_GH_FAIL_VIEW": "1"})
        # THEN exit 5, nothing posted or run
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("could not run", p.stderr)
        self.assertEqual((self.statuses(), self.runs()), ([], []))

    def test_git_fetch_failure_posts_error_on_the_head_and_exits_5(self):
        """
        What: `git fetch` failing (origin unreachable) exits 5 with state error on the head gh named.
        Why: the PR is not at fault. Breaks: a RuntimeError traceback and exit 1.
        """
        # GIVEN a PR that needs the exam and an origin that is gone
        head = self.reviewer_pr()
        run(GIT + ["remote", "set-url", "origin", str(self.r.tmp / "gone.git")], self.r.aw)
        # WHEN the canary runs
        p = self.canary(head)
        # THEN could not run on the head, no exam
        self.assertCouldNotRun(p, "", head)
        self.assertEqual(self.r.exam_calls(), [])

    def test_pr_moved_while_fetching_exits_5(self):
        """
        What: gh naming another head than the fetched one exits 5 "moved while fetching", no exam.
        Why: the verdict must be for the sha it is posted on; a retry picks up the new head.
        """
        # GIVEN gh reporting a head the fetch does not find
        self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary("0" * 40)
        # THEN exit 5, no exam
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("moved while fetching", p.stderr)
        self.assertEqual(self.r.exam_calls(), [])

    def test_failed_pr_edit_still_posts_the_status_then_exits_5(self):
        """
        What: when `gh pr edit` of the score block fails, the verdict status is still posted, then exit 5 naming
        the kept body file.
        Why: the status is what the merge gate reads. Breaks: a passing PR left with no status.
        """
        # GIVEN a passing PR and a gh that cannot edit the body
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"FAKE_GH_FAIL_EDIT": "1"})
        # THEN the success status is posted, exit 5 names the body file
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertEqual([s[1] for s in self.statuses()], ["success"])
        self.assertIn(str(self.r.home / "exams/pr-5/pr-body.md"), p.stderr)

    def test_commit_mode_never_reuses_an_installed_tree_run_as_main(self):
        """
        What: a booked, sealed baseline run of an installed tree whose HARNESS_VERSION commit is main is not
        reused by the --pr canary; main runs again.
        Why: an installed tree (maybe an installing repo's private one) is not main's harness at that commit.
        Breaks: a PR scored against some other repo's tree.
        """
        # GIVEN a booked installed-tree baseline run pinned to main's commit
        tree = installed_tree(self.r.tmp / "tree", {"commit": self.r.base_sha, "addon": None, "addon_sha256": None})
        self.r.base_run(harness=("--installed", str(tree)), env={"HARNESS_MODEL_ACCESS": "subscription"})
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head)
        # THEN main ran again
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.kinds(), ["baseline", "candidate"])

    def test_both_runs_errored_posts_error_and_exits_5(self):
        """
        What: main's and the candidate's runs both erroring (every try) is "could not run", state error, exit 5.
        Why: an image build timeout is no verdict on the PR. Breaks: a red failure on a good PR.
        """
        # GIVEN a PR whose two runs both error
        head = self.reviewer_pr()
        # WHEN the canary runs
        p = self.canary(head, env={"FAKE_EXAM_ERRORED": "baseline candidate"})
        # THEN could not run, and no rerun
        self.assertCouldNotRun(p, "", head)
        self.assertEqual(len(self.runs("candidate")), 1)

    def test_no_plan_token_exits_4_and_never_preflights_a_paid_credential(self):
        """
        What: no token (and Bedrock/API keys set) exits 4 naming CLAUDE_CODE_OAUTH_TOKEN; the fake preflight is
        never called with AWS_* or ANTHROPIC_API_KEY; nothing runs or is fetched. Same for `exam-route
        --subscription-only`.
        Why: walking EXAM_ROUTES would make paid calls the ledger never records.
        Breaks: a canary silently billed to Bedrock or the API.
        """
        # GIVEN a PR that needs the exam and no plan token
        head = self.reviewer_pr()
        for args in (("exam", "--pr", "5", "--tasks", str(self.r.manifest), "--subscription-only"),
                     ("exam-route", "--subscription-only")):
            with self.subTest(cmd=args[0]):
                # WHEN the canary (or exam-route) runs
                p = self.r.loop(*args, env={"FAKE_GH_HEAD": head, "CLAUDE_CODE_OAUTH_TOKEN": ""})
                # THEN exit 4 naming the token, no paid preflight, run or fetch, no planted value printed
                self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
                self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", p.stderr)
                self.assertNoPaidCredential()
                self.assertEqual((self.runs(), self.r.exam_calls(), self.fetches()), ([], [], []))
                for planted in ("planted-secret", "sk-planted", "AKIAPLANTED"):
                    self.assertNotIn(planted, p.stdout + p.stderr)

    def test_rejected_plan_token_exits_4_after_one_preflight(self):
        """
        What: a token the preflight rejects exits 4; the subscription was the only route tried.
        Why: a bad plan token must pause, never fall through to a paid route.
        Breaks: a rejected token silently moves the exam to Bedrock or the API.
        """
        # GIVEN a preflight that rejects the subscription
        # WHEN exam-route --subscription-only runs
        p = self.r.loop("exam-route", "--subscription-only",
                        env={"EXAM_PREFLIGHT_CLAUDE": self.r.preflight(fail="subscription")})
        # THEN exit 4 after one preflight, no paid key, no token printed
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertEqual(len(self.preflight_envs()), 1)
        self.assertNoPaidCredential()
        self.assertNotIn(DUMMY, p.stdout + p.stderr)

    def test_env_file_token_reaches_only_the_subprocesses(self):
        """
        What: with no token in the env, the one in HARNESS_EXAM_ENV_FILE reaches the preflight and every run_exam,
        and appears in no stdout, stderr or loop-home file; when the preflight rejects it (and echoes it, as a
        careless error might), it is still in neither stdout nor stderr.
        Why: a laptop shell need not export it. Breaks: the token in a log or a public status.
        """
        # GIVEN a PR, no token in the env, and a scratch env file holding a dummy one
        head = self.reviewer_pr()
        env_file = self.r.tmp / "exam.env"
        env_file.write_text(f'AWS_SECRET_ACCESS_KEY=x\nexport CLAUDE_CODE_OAUTH_TOKEN="{DUMMY}"\n')
        token_log = self.r.tmp / "tokens.txt"
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "", "HARNESS_EXAM_ENV_FILE": str(env_file),
               "FAKE_EXAM_TOKEN_LOG": str(token_log)}
        with self.subTest("preflight accepts the file's token"):
            # WHEN the canary runs
            p = self.canary(head, env=env)
            # THEN the preflight and both run_exams got the token, and no output or file holds it
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", self.preflight_envs()[0])
            self.assertEqual(token_log.read_text().split(), [DUMMY, DUMMY])
            files = [f.read_text(errors="replace") for f in self.r.home.rglob("*") if f.is_file()]
            for text in (p.stdout, p.stderr, *files, *(json.dumps(c) for c in self.r.gh_calls())):
                self.assertNotIn(DUMMY, text)
        with self.subTest("preflight rejects the file's token"):
            # WHEN the canary runs with a preflight that fails and prints every credential it saw
            p = self.canary(head, env={**env, "EXAM_PREFLIGHT_CLAUDE": self.r.preflight(fail="subscription")})
            # THEN exit 4, and the token is in neither stdout nor stderr
            self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
            self.assertNotIn(DUMMY, p.stdout)
            self.assertNotIn(DUMMY, p.stderr)

    def test_status_goes_on_the_pr_head_not_the_restacked_commit(self):
        """
        What: when main moved, the exam screens the restacked commit but the status lands on the PR head sha.
        Why: GitHub's merge gate reads the head's statuses. Breaks: a status on a commit no one sees.
        """
        # GIVEN a PR and a later commit on main
        head = self.reviewer_pr()
        self.r.commit_on_main("harness-loop/other.txt", "x\n")
        # WHEN the canary runs with --status-repo
        p = self.canary(head, "--status-repo", "owner/repo")
        # THEN the status is on the head in that repo, and the candidate ran the restacked commit
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([s[0] for s in self.statuses()], [f"repos/owner/repo/statuses/{head}"])
        cand = [c for c in self.r.exam_calls() if c[c.index("--kind") + 1] == "candidate"][0]
        self.assertNotEqual(cand[cand.index("--harness") + 1], head)

    def test_status_description_is_capped_at_140(self):
        """
        What: post_status sends at most 140 characters of description (GitHub's limit).
        Why: gh rejects a longer one, and a rejected post turns a verdict into exit 5.
        Breaks: a long free-check reason loses the verdict.
        """
        # GIVEN the fake gh on PATH, in this process
        env = {"PATH": self.r.env["PATH"], "FAKE_GH_LOG": self.r.env["FAKE_GH_LOG"]}
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            # WHEN a 300-character description is posted
            loop.post_status(("o/r", "a" * 40), "error", "x" * 300)
        # THEN gh got the first 140
        (status,) = self.statuses()
        self.assertEqual(status[3], "x" * 140)

    def test_installed_post_status_needs_repo_and_sha(self):
        """
        What: installed mode with --post-status but no --status-repo/--status-sha is a usage error (exit 2).
        Why: installed trees have no PR to read the repo or head from.
        Breaks: a status posted on agent-workflow for another repo's change, or a crash.
        """
        # GIVEN one installed tree
        cand = installed_tree(self.r.tmp / "c")
        # WHEN installed mode runs with --post-status alone
        p = self.r.loop("exam", "--candidate-installed", str(cand), "--baseline-installed", str(cand),
                        "--tasks", str(self.r.manifest), "--subscription-only", "--post-status")
        # THEN exit 2 naming the missing flag
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("--status-repo", p.stderr)


class CanaryInstalled(Canary):
    """loop.py exam --candidate-installed C --baseline-installed B (an installing repo's canary)."""

    def trees(self, addon=None):
        version = {"commit": "c" * 40, "addon": addon, "addon_sha256": "a" * 64 if addon else None}
        base = installed_tree(self.r.tmp / "base", version)
        cand = installed_tree(self.r.tmp / "cand", version, skill="Builds one bead, carefully.\n")
        return cand, base

    def installed(self, cand, base, *extra, spec=None, env=None):
        return self.r.loop("exam", "--candidate-installed", str(cand), "--baseline-installed", str(base),
                           "--tasks", str(self.r.manifest), "--subscription-only", "--rerun-on-fail", "1",
                           "--post-status", "--status-repo", "owner/scratch", "--status-sha", "d" * 40, *extra,
                           env={"FAKE_EXAM_SPEC": json.dumps(spec or tl.SPEC_PASS), **(env or {})})

    def assertNothingStarted(self):
        self.assertEqual((self.preflight_envs(), self.r.exam_calls(), self.fetches()), ([], [], []))
        self.assertFalse(self.docker_log.exists())

    def test_installed_trees_run_with_installed_and_post_to_the_status_repo(self):
        """
        What: both trees run through run_exam --installed and the status lands on --status-repo@--status-sha.
        Why: an installing repo's canary must post on ITS PR, never agent-workflow's.
        Breaks: the status lands on agent-workflow, or the exam installs agent-workflow instead of the trees.
        """
        # GIVEN two different installed trees
        cand, base = self.trees()
        # WHEN installed mode runs
        p = self.installed(cand, base)
        # THEN main then the candidate ran --installed, and success landed on the status repo
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        calls = self.r.exam_calls()
        self.assertEqual([(c[c.index("--kind") + 1], c[c.index("--installed") + 1]) for c in calls],
                         [("baseline", str(base.resolve())), ("candidate", str(cand.resolve()))])
        self.assertEqual(self.statuses(), [(f"repos/owner/scratch/statuses/{'d' * 40}", "success", "harness-canary",
                                            "pass solved 4/5 vs main 3/5")])

    def test_identical_trees_are_not_needed_and_start_nothing(self):
        """
        What: candidate and baseline trees with the same installed_sha256 post success "not needed (installed
        trees identical)" and exit 0 with no preflight, Docker, fetch or run.
        Why: the gate counts hook-seam link targets the installed trees cannot hold; on identical trees the exam
        would "pass" without testing anything. Breaks: a meaningless pass, and plan tokens spent on it.
        """
        # GIVEN one tree passed as both candidate and baseline
        _, base = self.trees()
        # WHEN installed mode runs
        p = self.installed(base, base)
        # THEN success "not needed", nothing started
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("success", "harness-canary", "not needed (installed trees identical)")])
        self.assertNothingStarted()

    def test_candidate_tree_that_does_not_load_is_a_dry_run_fail(self):
        """
        What: a candidate tree failing the free checks (a SKILL.md frontmatter without description) posts failure
        "dry run fail (...)" and exits 1 before any preflight, Docker, fetch or run.
        Why: the same free check a --pr exam runs; a tree that cannot load must not spend plan tokens.
        Breaks: a broken skill burns a whole exam, or passes on the tasks it never loads.
        """
        # GIVEN a candidate tree whose implementer skill lacks a description
        cand, base = self.trees()
        (cand / ".claude/skills/implementer/SKILL.md").write_text("---\nname: implementer\n---\n\nBuild.\n")
        # WHEN installed mode runs
        p = self.installed(cand, base)
        # THEN failure "dry run fail (...)", exit 1, nothing started
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual([s[1:] for s in self.statuses()],
                         [("failure", "harness-canary",
                           "dry run fail (implementer/SKILL.md frontmatter lacks description)")])
        self.assertNothingStarted()

    def test_tree_holding_a_dotenv_file_is_refused_naming_the_path(self):
        """
        What: a tree with a .claude/.env file exits 2 naming the file path (never its contents), before anything
        starts and with no status.
        Why: installed_files packs every file into the sandbox; a working checkout's .env holds real secrets.
        Breaks: secrets copied into a Docker sandbox that runs the PR's harness.
        """
        # GIVEN a candidate tree holding a dummy .claude/.env
        cand, base = self.trees()
        (cand / ".claude/.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={DUMMY}\n")
        # WHEN installed mode runs
        p = self.installed(cand, base)
        # THEN exit 2 naming the path, not the value, and nothing started or posted
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(str((cand / ".claude/.env").resolve()), p.stderr)
        self.assertNotIn(DUMMY, p.stdout + p.stderr)
        self.assertEqual(self.statuses(), [])
        self.assertNothingStarted()

    def test_ignored_harness_trees_inside_git_are_refused_not_identical(self):
        """
        What: two different trees whose .claude is git-ignored inside a git repo exit 2 "no harness files under
        <root>", with no status and nothing started.
        Why: such trees list no harness file, so both hash the same. Breaks: success "not needed (installed trees
        identical)" posted for two trees that differ, so a harness PR merges with no exam.
        """
        # GIVEN a git repo ignoring .claude/, holding two differing trees
        repo = self.r.tmp / "host"
        repo.mkdir()
        run(GIT + ["init", "-q"], repo)
        (repo / ".gitignore").write_text(".claude/\n")
        run(GIT + ["add", "-A"], repo)
        run(GIT + ["commit", "-qm", "init"], repo)
        version = {"commit": "c" * 40, "addon": None, "addon_sha256": None}
        base = installed_tree(repo / "base", version)
        cand = installed_tree(repo / "cand", version, skill="Builds one bead, carefully.\n")
        # WHEN installed mode runs
        p = self.installed(cand, base)
        # THEN exit 2 naming the empty tree, nothing posted or started
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("no harness files under", p.stderr)
        self.assertNotIn("identical", p.stdout + p.stderr)
        self.assertEqual(self.statuses(), [])
        self.assertNothingStarted()

    def test_second_canary_reuses_main_run_with_the_same_installed_hash(self):
        """
        What: a booked, sealed main run whose installed_sha256 matches the baseline tree is reused.
        Why: repeated canaries against the same main install must not re-run main. Breaks: double spend.
        """
        # GIVEN one finished canary on two trees
        cand, base = self.trees()
        self.assertEqual(self.installed(cand, base).returncode, 0)
        # WHEN it runs again
        p = self.installed(cand, base)
        # THEN only the candidate ran the second time
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.kinds(), ["baseline", "candidate", "candidate"])
        # AND a changed baseline tree is not reused
        (base / "AGENTS.md").write_text("# Agents, changed\n")
        self.assertEqual(self.installed(cand, base).returncode, 0)
        self.assertEqual(self.kinds()[3:], ["baseline", "candidate"])

    def test_tree_with_addon_in_the_default_home_exits_2(self):
        """
        What: a tree whose HARNESS_VERSION names an add-on, with the default ~/.harness-loop, exits 2 before any
        preflight or run.
        Why: the improver reads that home's runs/; an installing repo's tree is private.
        Breaks: private skills land where a public PR can quote them.
        """
        # GIVEN two add-on trees and no HARNESS_LOOP_HOME
        cand, base = self.trees(addon=".claude/harness-addon")
        # WHEN installed mode runs
        p = self.installed(cand, base, env={"HARNESS_LOOP_HOME": ""})
        # THEN exit 2 naming HARNESS_LOOP_HOME, nothing started or posted
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("HARNESS_LOOP_HOME", p.stderr)
        self.assertEqual((self.preflight_envs(), self.r.exam_calls(), self.statuses()), ([], [], []))

    def test_laptop_only_in_installed_mode_posts_nothing(self):
        """
        What: CLAUDE_CODE_REMOTE exits 5 in installed mode too, with no status and no run.
        Why: a cloud session has no Docker and holds no plan token for the sandbox.
        Breaks: an error status from a session that should never have tried.
        """
        # GIVEN two trees and a cloud session
        cand, base = self.trees()
        # WHEN installed mode runs
        p = self.installed(cand, base, env={"CLAUDE_CODE_REMOTE": "1"})
        # THEN exit 5, nothing posted or run
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertEqual((self.statuses(), self.r.exam_calls()), ([], []))


# Canary's PR tests are not CanaryInstalled's.
for _name in [n for n in vars(Canary) if n.startswith("test_")]:
    setattr(CanaryInstalled, _name, None)


if __name__ == "__main__":
    unittest.main()
