#!/usr/bin/env python3
"""Compare two sealed exam runs task by task, with a luck margin.

    compare.py <runA> <runB> [--margin-from <runA2>] [--screen-rule [--solved-slack N]]

A is the baseline, B the candidate. Each run dir holds <task>/try-<n>/result.json
and summary.sha256 (written by run_exam.py). A run whose files no longer match
its seal is refused with exit 3.

Per task, a run's reward is the mean over its counted tries:
  - status ok tries count with their reward;
  - budget_cut and error tries: in the BASELINE excluded (running out of
    budget or a broken sandbox is not the harness failing); in the CANDIDATE
    counted as reward 0 at the full cap (run.json max_budget_usd), because a
    candidate's overspend or crash is itself the regression.
Tasks with counted tries in both runs are paired; d_i = B - A.
A task is solved when its mean reward is >= 0.5. Costs are over paired tasks.

Prints  n= diff= se= ci95=± solvedA= solvedB= costA= costB= cutA= cutB= errA= errB=
        statusA= statusB=   (run.json status: complete|incomplete)
--margin-from A2 (a second run of A's harness): margin_raw = 1.96 x SE of the
paired difference A vs A2, margin = max(margin_raw, 1/n), and verdict=win|lose|tie.
--screen-rule: screen=pass iff solvedB >= solvedA - --solved-slack (default SCREEN_SOLVED_SLACK, 1; the
canary passes 0), costB <= SCREEN_COST_RATIO (1.25) x costA,
cutB <= cutA, errB <= errA and B is not incomplete. screen_fail_decided() is the same rule asked
mid-run (run_exam.py --early-stop-vs, with --early-stop-slack as the slack).
Exit: 0 ok, 2 usage / nothing to pair, 3 seal mismatch.
"""
import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

SEAL = "summary.sha256"
DEFAULT_CAP = 12.05
EPS = 1e-9
SCREEN_SOLVED_SLACK = 1  # the screen allows the candidate this many fewer solved tasks than main
SCREEN_COST_RATIO = 1.25  # and at most this multiple of main's cost


def sealed_files(run):
    """Every file the seal covers: each try's result.json (<task>/try-<n>/, or
    <agent>/<task>/try-<n>/ in a controls run), run.json and summary.json."""
    run = Path(run)
    files = [*run.glob("*/try-*/result.json"), *run.glob("*/*/try-*/result.json"),
             run / "run.json", run / "summary.json"]
    return sorted(p for p in files if p.is_file())


def listing(run):
    """sha256sum-style lines over sealed_files, sorted by relative path."""
    run = Path(run)
    return "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(run).as_posix()}\n"
                   for p in sealed_files(run))


def seal(run):
    (Path(run) / SEAL).write_text(listing(run))


def check_seal(run):
    f = Path(run) / SEAL
    if not f.is_file() or f.read_text() != listing(run):
        print(f"compare: {run}: files do not match {SEAL}; refusing", file=sys.stderr)
        sys.exit(3)


def load(run):
    """({task: [result dicts]}, cap, run status) for a sealed run."""
    check_seal(run)
    run = Path(run)
    meta = json.loads((run / "run.json").read_text())
    tries = {}
    for f in sorted(run.glob("*/try-*/result.json")):
        tries.setdefault(f.parent.parent.name, []).append(json.loads(f.read_text()))
    return tries, float(meta.get("max_budget_usd") or DEFAULT_CAP), meta.get("status", "unknown")


def counted(rows, candidate, cap):
    """[(reward, cost)] of the tries that count for one task."""
    out = []
    for r in rows:
        if r["status"] == "ok":
            out.append((float(r["reward"]), float(r.get("cost_usd") or 0)))
        elif r["status"] in ("budget_cut", "error") and candidate:
            out.append((0.0, cap))
    return out


def paired(a, b, b_is_candidate=True):
    """Per paired task: (rewardA, rewardB, costA, costB), plus {status: (countA, countB)}
    for budget_cut and error over the tasks both runs have."""
    (ta, _, _), (tb, cap_b, _) = a, b
    common = sorted(set(ta) & set(tb))
    count = lambda t, st: sum(r["status"] == st for task in common for r in t[task])  # noqa: E731
    rows = []
    for task in common:
        ca = counted(ta[task], False, 0)
        cb = counted(tb[task], b_is_candidate, cap_b)
        if ca and cb:
            rows.append((statistics.fmean(x for x, _ in ca), statistics.fmean(x for x, _ in cb),
                         sum(c for _, c in ca), sum(c for _, c in cb)))
    return rows, {st: (count(ta, st), count(tb, st)) for st in ("budget_cut", "error")}


def screen_fail_decided(base, done, remaining, cap=DEFAULT_CAP, slack=SCREEN_SOLVED_SLACK):
    """Why --screen-rule can no longer pass, whatever the remaining tasks do, or None (keep going).

    base: load() of the sealed baseline; done: the candidate's finished try results; remaining: ids of the
    candidate's tasks with a try not finished yet; cap: the candidate's max_budget_usd; slack: --solved-slack.
    Mirrors main()'s pairing and screen rule as they will stand once every task has run; only a fail can be decided early."""
    ta = base[0]
    common = ({r["task"] for r in done} | set(remaining)) & set(ta)
    ours = [r for r in done if r["task"] in common]
    for st, word in (("budget_cut", "cut"), ("error", "error")):
        b, a = sum(r["status"] == st for r in ours), sum(r["status"] == st for t in common for r in ta[t])
        if b > a:
            return f"{word} tries {b} > main's {a}"
    paired = {t: counted(ta[t], False, 0) for t in common if counted(ta[t], False, 0)}
    solved_a = sum(statistics.fmean(x for x, _ in c) >= 0.5 for c in paired.values())
    finished = [counted([r for r in ours if r["task"] == t], True, cap) for t in paired if t not in remaining]
    best = sum(statistics.fmean(x for x, _ in c) >= 0.5 for c in finished if c) + len(set(paired) & set(remaining))
    if best < solved_a - slack:
        return f"solved at most {best} < main's {solved_a} - {slack}"
    cost_a = sum(c for rows in paired.values() for _, c in rows)
    cost_b = sum(c for _, c in counted([r for r in ours if r["task"] in paired], True, cap))
    if cost_b > SCREEN_COST_RATIO * cost_a + EPS:
        return f"cost ${cost_b:.2f} > {SCREEN_COST_RATIO} x main's ${cost_a:.2f}"
    return None


def diff_stats(rows):
    d = [rb - ra for ra, rb, _, _ in rows]
    se = statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else 0.0
    return statistics.fmean(d), se


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_a")
    ap.add_argument("run_b")
    ap.add_argument("--margin-from")
    ap.add_argument("--screen-rule", action="store_true")
    ap.add_argument("--solved-slack", type=int, default=SCREEN_SOLVED_SLACK)
    args = ap.parse_args(argv)

    a, b = load(args.run_a), load(args.run_b)
    rows, counts = paired(a, b)
    (cut_a, cut_b), (err_a, err_b) = counts["budget_cut"], counts["error"]
    if not rows:
        sys.exit("compare: no task has counted tries in both runs")
    n = len(rows)
    diff, se = diff_stats(rows)
    solved_a = sum(ra >= 0.5 for ra, _, _, _ in rows)
    solved_b = sum(rb >= 0.5 for _, rb, _, _ in rows)
    cost_a = sum(r[2] for r in rows)
    cost_b = sum(r[3] for r in rows)
    print(f"n={n} diff={diff:.3f} se={se:.3f} ci95=±{1.96 * se:.3f} solvedA={solved_a} solvedB={solved_b} "
          f"costA={cost_a:.2f} costB={cost_b:.2f} cutA={cut_a} cutB={cut_b} errA={err_a} errB={err_b} "
          f"statusA={a[2]} statusB={b[2]}")
    if args.margin_from:
        # A2 is a second baseline: its cut tries are excluded too.
        base_rows, _ = paired(a, load(args.margin_from), b_is_candidate=False)
        if not base_rows:
            sys.exit("compare: no task has counted tries in both baseline runs")
        margin_raw = 1.96 * diff_stats(base_rows)[1]
        margin = max(margin_raw, 1 / n)
        verdict = "win" if diff > margin + EPS else "lose" if diff < -margin - EPS else "tie"
        print(f"margin_raw={margin_raw:.3f} margin={margin:.3f} verdict={verdict}")
    if args.screen_rule:
        ok = (solved_b >= solved_a - args.solved_slack and cost_b <= SCREEN_COST_RATIO * cost_a + EPS
              and cut_b <= cut_a and err_b <= err_a and b[2] != "incomplete")
        print(f"screen={'pass' if ok else 'fail'}")


if __name__ == "__main__":
    main()
