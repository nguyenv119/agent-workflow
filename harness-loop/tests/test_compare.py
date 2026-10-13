"""Tests for harness-loop/exam/compare.py on hand-written, sealed run dirs."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXAM = Path(__file__).resolve().parents[1] / "exam"
SCRIPT = EXAM / "compare.py"
sys.path.insert(0, str(EXAM))
import compare  # noqa: E402

CAP = 12.05


def make_run(root, name, tries, cap=CAP, status="complete"):
    """Write a sealed run dir. tries: {task: [(status, reward, cost), ...]}."""
    run = Path(root) / name
    for task, rows in tries.items():
        for i, (st, reward, cost) in enumerate(rows, 1):
            d = run / task / f"try-{i}"
            d.mkdir(parents=True)
            (d / "result.json").write_text(json.dumps(
                {"task": task, "try": i, "status": st, "reward": reward, "cost_usd": cost}))
    (run / "run.json").write_text(json.dumps({"max_budget_usd": cap, "status": status}))
    (run / "summary.json").write_text("{}")
    compare.seal(run)
    return run


def ok(reward, cost=10.0):
    return ("ok", reward, cost)


class CompareTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        for dirpath, dirs, files in os.walk(self.root):
            for n in dirs + files:
                p = Path(dirpath) / n
                p.chmod(p.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(self.root)

    def cmp(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                              text=True, capture_output=True)

    def fields(self, out):
        return dict(tok.split("=", 1) for tok in out.split() if "=" in tok)

    def test_identical_runs_give_zero_raw_margin_floored_at_one_task(self):
        """
        Two baseline runs of one harness on 5 tasks x 1 try often match exactly,
        so the raw margin is 0. Without the 1/n floor a single lucky task flip
        would count as a win.
        """
        # GIVEN three identical runs (A, B, and A's repeat) on 5 tasks
        t = {f"t{i}": [ok(r)] for i, r in enumerate([1, 1, 1, 0, 0])}
        a, b, a2 = (make_run(self.root, n, t) for n in ("a", "b", "a2"))
        # WHEN compare runs with --margin-from
        p = self.cmp(a, b, "--margin-from", a2)
        # THEN diff and raw margin are 0, margin is 1/5 and the verdict is a tie
        self.assertEqual(p.returncode, 0, p.stderr)
        f = self.fields(p.stdout)
        self.assertEqual((f["n"], f["diff"], f["se"]), ("5", "0.000", "0.000"))
        self.assertEqual((f["margin_raw"], f["margin"], f["verdict"]), ("0.000", "0.200", "tie"))

    def test_known_rewards_give_exact_diff_and_se(self):
        """The paired statistics are the whole decision; check them against hand arithmetic."""
        # GIVEN A solves t0..t2 and B solves t0, t3, t4 (d = 0,-1,-1,+1,+1)
        a = make_run(self.root, "a", {f"t{i}": [ok(r)] for i, r in enumerate([1, 1, 1, 0, 0])})
        b = make_run(self.root, "b", {f"t{i}": [ok(r)] for i, r in enumerate([1, 0, 0, 1, 1])})
        # WHEN compared
        f = self.fields(self.cmp(a, b).stdout)
        # THEN mean d = 0, sample sd = 1, se = 1/sqrt(5) = 0.447, ci95 = 0.877
        self.assertEqual((f["diff"], f["se"], f["ci95"]), ("0.000", "0.447", "±0.877"))
        self.assertEqual((f["solvedA"], f["solvedB"]), ("3", "3"))

    def test_verdict_win_and_lose_beyond_margin(self):
        """A candidate that gains (or drops) more than the margin is a win (or lose)."""
        # GIVEN baseline A = A2 solving 1 of 5; B solving 4; C solving 0
        base = {f"t{i}": [ok(r)] for i, r in enumerate([1, 0, 0, 0, 0])}
        a, a2 = make_run(self.root, "a", base), make_run(self.root, "a2", base)
        b = make_run(self.root, "b", {f"t{i}": [ok(r)] for i, r in enumerate([1, 1, 1, 1, 0])})
        c = make_run(self.root, "c", {f"t{i}": [ok(0)] for i in range(5)})
        # WHEN each candidate is compared with --margin-from
        win = self.fields(self.cmp(a, b, "--margin-from", a2).stdout)
        lose = self.fields(self.cmp(a, c, "--margin-from", a2).stdout)
        # THEN +0.6 > 0.2 wins; -0.2 is not beyond the 0.2 margin, so it ties
        self.assertEqual(win["verdict"], "win")
        self.assertEqual(lose["verdict"], "tie")
        # AND a drop of 2 tasks (-0.4) loses
        a3 = make_run(self.root, "a3", {f"t{i}": [ok(r)] for i, r in enumerate([1, 1, 0, 0, 0])})
        a4 = make_run(self.root, "a4", {f"t{i}": [ok(r)] for i, r in enumerate([1, 1, 0, 0, 0])})
        self.assertEqual(self.fields(self.cmp(a3, c, "--margin-from", a4).stdout)["verdict"], "lose")

    def test_edited_result_after_seal_exits_3(self):
        """No later process (the improver included) may quietly edit a past score."""
        # GIVEN a sealed run whose result.json is then edited
        a = make_run(self.root, "a", {"t0": [ok(0)]})
        b = make_run(self.root, "b", {"t0": [ok(0)]})
        f = a / "t0/try-1/result.json"
        f.write_text(f.read_text().replace('"reward": 0', '"reward": 1'))
        # WHEN compared
        p = self.cmp(a, b)
        # THEN compare refuses with exit 3 naming the run
        self.assertEqual(p.returncode, 3)
        self.assertIn(str(a), p.stderr)

    def test_added_result_after_seal_exits_3(self):
        """A try folder slipped in after sealing changes the listing too."""
        # GIVEN a sealed run plus one extra result.json written afterwards
        a = make_run(self.root, "a", {"t0": [ok(0)]})
        b = make_run(self.root, "b", {"t0": [ok(0)]})
        (a / "t0/try-2").mkdir()
        (a / "t0/try-2/result.json").write_text(json.dumps({"status": "ok", "reward": 1}))
        # WHEN compared
        # THEN exit 3
        self.assertEqual(self.cmp(a, b).returncode, 3)

    def test_baseline_cut_try_is_excluded_from_pairing(self):
        """Running out of budget in the baseline is not the harness failing."""
        # GIVEN A cut on t0 (its only try); B solves t0 and t1
        a = make_run(self.root, "a", {"t0": [("budget_cut", 1, CAP)], "t1": [ok(1)]})
        b = make_run(self.root, "b", {"t0": [ok(1)], "t1": [ok(1)]})
        # WHEN compared
        f = self.fields(self.cmp(a, b).stdout)
        # THEN only t1 is paired and the cut is reported
        self.assertEqual((f["n"], f["cutA"], f["cutB"]), ("1", "1", "0"))
        self.assertEqual(f["costA"], "10.00")

    def test_candidate_cut_try_counts_unsolved_at_cap_and_fails_screen(self):
        """A candidate's overspend is itself the regression."""
        # GIVEN A solves t0 and t1 at $10 each; B solves t1 but is cut on t0
        a = make_run(self.root, "a", {"t0": [ok(1)], "t1": [ok(1)]})
        b = make_run(self.root, "b", {"t0": [("budget_cut", 1, 3.0)], "t1": [ok(1)]})
        # WHEN compared under the screen rule
        f = self.fields(self.cmp(a, b, "--screen-rule").stdout)
        # THEN t0 is paired as unsolved, costed at the full cap, and the screen fails
        self.assertEqual((f["n"], f["solvedB"], f["diff"]), ("2", "1", "-0.500"))
        self.assertEqual(f["costB"], f"{CAP + 10:.2f}")
        self.assertEqual((f["cutA"], f["cutB"], f["screen"]), ("0", "1", "fail"))

    def test_baseline_error_try_is_excluded(self):
        """A broken sandbox in the baseline is not the harness failing."""
        # GIVEN A's t0 errored with a stray reward 1
        a = make_run(self.root, "a", {"t0": [("error", 1, 1.0)], "t1": [ok(0)]})
        b = make_run(self.root, "b", {"t0": [ok(0)], "t1": [ok(0)]})
        # WHEN compared
        f = self.fields(self.cmp(a, b).stdout)
        # THEN t0 is not paired
        self.assertEqual(f["n"], "1")

    def test_edited_run_json_after_seal_exits_3(self):
        """run.json carries the solved count the win check reads; it is sealed too."""
        # GIVEN a sealed run whose run.json is then edited
        a = make_run(self.root, "a", {"t0": [ok(0)]})
        b = make_run(self.root, "b", {"t0": [ok(0)]})
        (a / "run.json").write_text(json.dumps({"max_budget_usd": CAP, "status": "complete", "solved": 5}))
        # WHEN compared / THEN exit 3
        self.assertEqual(self.cmp(a, b).returncode, 3)

    def test_candidate_error_try_counts_unsolved_at_cap_and_fails_screen(self):
        """A candidate that crashes the run has regressed, the same as one that overspends."""
        # GIVEN A solves t0 and t1 at $10 each; B solves t1 but errors on t0
        a = make_run(self.root, "a", {"t0": [ok(1)], "t1": [ok(1)]})
        b = make_run(self.root, "b", {"t0": [("error", None, 0.0)], "t1": [ok(1)]})
        # WHEN compared under the screen rule
        f = self.fields(self.cmp(a, b, "--screen-rule").stdout)
        # THEN t0 is paired as unsolved at the cap, errB is 1 and the screen fails
        self.assertEqual((f["n"], f["solvedB"], f["costB"]), ("2", "1", f"{CAP + 10:.2f}"))
        self.assertEqual((f["errA"], f["errB"], f["screen"]), ("0", "1", "fail"))

    def test_candidate_error_counts_in_the_verdict(self):
        """The verdict uses the same rule: B's errors are losses, A's are dropped."""
        # GIVEN A = A2 solving t0..t2 (A errors on t3), B erroring on t0..t2
        base = {"t0": [ok(1)], "t1": [ok(1)], "t2": [ok(1)], "t3": [("error", None, 0.0)], "t4": [ok(0)]}
        a, a2 = make_run(self.root, "a", base), make_run(self.root, "a2", base)
        b = make_run(self.root, "b", {f"t{i}": [("error", None, 0.0)] if i < 3 else [ok(0)] for i in range(5)})
        # WHEN compared with --margin-from
        f = self.fields(self.cmp(a, b, "--margin-from", a2).stdout)
        # THEN t3 is dropped (n=4), B loses 3 of 4 and the verdict is lose
        self.assertEqual((f["n"], f["diff"], f["verdict"]), ("4", "-0.750", "lose"))

    def test_incomplete_candidate_fails_screen(self):
        """A candidate run that stopped early cannot pass the screen on the tasks it reached."""
        # GIVEN identical scores, but B's run is marked incomplete
        t = {f"t{i}": [ok(1)] for i in range(5)}
        a = make_run(self.root, "a", t)
        b = make_run(self.root, "b", t, status="incomplete")
        # WHEN screened
        f = self.fields(self.cmp(a, b, "--screen-rule").stdout)
        # THEN statuses are printed and the screen fails
        self.assertEqual((f["statusA"], f["statusB"], f["screen"]), ("complete", "incomplete", "fail"))

    def screen(self, a_solved, b_solved, a_cost, b_cost):
        a = make_run(self.root, f"a{a_cost}{b_cost}{a_solved}{b_solved}",
                     {f"t{i}": [ok(int(i < a_solved), a_cost / 5)] for i in range(5)})
        b = make_run(self.root, f"b{a_cost}{b_cost}{a_solved}{b_solved}",
                     {f"t{i}": [ok(int(i < b_solved), b_cost / 5)] for i in range(5)})
        return self.fields(self.cmp(a, b, "--screen-rule").stdout)["screen"]

    def test_screen_one_fewer_solved_passes(self):
        """The screen tolerates one lost task (noise at n=5)."""
        # GIVEN A solves 3, B solves 2, same cost / WHEN screened / THEN pass
        self.assertEqual(self.screen(3, 2, 100, 100), "pass")

    def test_screen_two_fewer_solved_fails(self):
        """Two lost tasks is a regression the screen must stop."""
        # GIVEN A solves 3, B solves 1 / WHEN screened / THEN fail
        self.assertEqual(self.screen(3, 1, 100, 100), "fail")

    def test_screen_cost_125_percent_passes(self):
        """Exactly 1.25x the baseline cost is the allowed edge."""
        # GIVEN B costs 125 vs A's 100 / WHEN screened / THEN pass
        self.assertEqual(self.screen(3, 3, 100, 125), "pass")

    def test_screen_cost_126_percent_fails(self):
        """Anything past 1.25x the baseline cost fails."""
        # GIVEN B costs 126 vs A's 100 / WHEN screened / THEN fail
        self.assertEqual(self.screen(3, 3, 100, 126), "fail")

    def test_example_output_shape(self):
        """The first line carries every field the loop parses."""
        # GIVEN two 5-task runs
        t = {f"t{i}": [ok(1, 11.62)] for i in range(5)}
        a, b = make_run(self.root, "a", t), make_run(self.root, "b", t)
        # WHEN compared
        first = self.cmp(a, b).stdout.splitlines()[0]
        # THEN it matches the spec's example format
        self.assertEqual(first, "n=5 diff=0.000 se=0.000 ci95=±0.000 solvedA=5 solvedB=5 "
                                "costA=58.10 costB=58.10 cutA=0 cutB=0 errA=0 errB=0 "
                                "statusA=complete statusB=complete")


def res(task, status="ok", reward=1, cost=10.0):
    """One finished candidate try, as run_exam's schedule() holds it."""
    return {"task": task, "try": 1, "status": status, "reward": reward, "cost_usd": cost}


class ScreenFailDecidedTest(unittest.TestCase):
    """compare.screen_fail_decided: run_exam's early stop asks it after every finished candidate try."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        # main solves t0..t2 and misses t3, t4, every try ok at $10: solvedA=3, costA=$50, no cut, no error
        self.base = compare.load(make_run(self.root, "a", {f"t{i}": [ok(int(i < 3))] for i in range(5)}))

    def decided(self, done):
        tasks = {f"t{i}" for i in range(5)}
        return compare.screen_fail_decided(self.base, done, tasks - {r["task"] for r in done}, CAP)

    def test_a_cut_beyond_mains_cut_count_decides_fail(self):
        """
        The screen fails on cutB > cutA, and a cut try cannot be undone, so one cut more than main's
        decides it. Without this an exam with a broken dry run would pay for four more tasks.
        """
        # GIVEN main cut nothing, WHEN the candidate's first try is budget_cut
        why = self.decided([res("t0", "budget_cut", None, 3.0)])
        # THEN the fail is decided, naming the cut rule
        self.assertIn("cut", why)

    def test_an_error_beyond_mains_error_count_decides_fail(self):
        """errB > errA fails the screen whatever comes later, so the first extra error stops the run."""
        # GIVEN main erred nowhere, WHEN the candidate's first try errors
        why = self.decided([res("t0", "error", None, 0.0)])
        # THEN the fail is decided, naming the error rule
        self.assertIn("error", why)

    def test_too_few_tasks_left_to_reach_mains_solved_minus_one_decides_fail(self):
        """
        The screen needs solvedB >= solvedA - 1. When the candidate's solves so far plus every paired task
        still to run cannot reach that, no later result can pass it.
        """
        # GIVEN main solved 3 (the screen needs 2), and the candidate missed t0..t2: 0 + 2 left = 2, still reachable
        self.assertIsNone(self.decided([res(t, reward=0) for t in ("t0", "t1", "t2")]))
        # WHEN it also misses t3 (0 + 1 left < 2)
        why = self.decided([res(t, reward=0) for t in ("t0", "t1", "t2", "t3")])
        # THEN the fail is decided, naming the solved rule
        self.assertIn("solved", why)

    def test_cost_past_125_percent_of_mains_decides_fail(self):
        """Cost only grows, so candidate spend past 1.25 x main's paired cost fails the screen already."""
        # GIVEN main's paired cost is $50, WHEN the candidate spends $63 on two solved tasks
        why = self.decided([res("t0", cost=31.5), res("t1", cost=31.5)])
        # THEN the fail is decided, naming the cost rule
        self.assertIn("cost", why)

    def test_a_run_that_can_still_pass_is_not_decided(self):
        """
        One miss, $20 spent, no cut or error: the remaining tasks can still pass the screen, so the run
        must go on. Stopping here would fail a candidate the full screen passes.
        """
        # GIVEN main as above, WHEN the candidate missed t0 and solved t1 for $20
        why = self.decided([res("t0", reward=0), res("t1")])
        # THEN nothing is decided
        self.assertIsNone(why)


if __name__ == "__main__":
    unittest.main()
