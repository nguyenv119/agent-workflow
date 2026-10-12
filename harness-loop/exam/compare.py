#!/usr/bin/env python3
"""Compare two sealed exam runs task by task, with a luck margin.

    compare.py <runA> <runB> [--margin-from <runA2>] [--screen-rule]

A is the baseline, B the candidate. Each run dir holds <task>/try-<n>/result.json
and summary.sha256 (written by run_exam.py). A run whose files no longer match
its seal is refused with exit 3.

Per task, a run's reward is the mean over its counted tries:
  - status ok tries count with their reward;
  - budget_cut: in the BASELINE excluded (running out of budget is not the
    harness failing); in the CANDIDATE counted as reward 0 at the full cap
    (run.json max_budget_usd), because overspend is itself the regression;
  - error tries never count.
Tasks with counted tries in both runs are paired; d_i = B - A.
A task is solved when its mean reward is >= 0.5. Costs are over paired tasks.

Prints  n= diff= se= ci95=± solvedA= solvedB= costA= costB= cutA= cutB=
--margin-from A2 (a second run of A's harness): margin_raw = 1.96 x SE of the
paired difference A vs A2, margin = max(margin_raw, 1/n), and verdict=win|lose|tie.
--screen-rule: screen=pass iff solvedB >= solvedA - 1, costB <= 1.25 x costA
and cutB <= cutA.
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
    """({task: [result dicts]}, cap) for a sealed run."""
    check_seal(run)
    run = Path(run)
    meta = json.loads((run / "run.json").read_text())
    tries = {}
    for f in sorted(run.glob("*/try-*/result.json")):
        tries.setdefault(f.parent.parent.name, []).append(json.loads(f.read_text()))
    return tries, float(meta.get("max_budget_usd") or DEFAULT_CAP)


def counted(rows, candidate, cap):
    """[(reward, cost)] of the tries that count for one task."""
    out = []
    for r in rows:
        if r["status"] == "ok":
            out.append((float(r["reward"]), float(r.get("cost_usd") or 0)))
        elif r["status"] == "budget_cut" and candidate:
            out.append((0.0, cap))
    return out


def paired(a, b, b_is_candidate=True):
    """Per paired task: (rewardA, rewardB, costA, costB), plus cut counts over common tasks."""
    (ta, _), (tb, cap_b) = a, b
    common = sorted(set(ta) & set(tb))
    cut = lambda t, task: sum(r["status"] == "budget_cut" for r in t[task])  # noqa: E731
    rows = []
    for task in common:
        ca = counted(ta[task], False, 0)
        cb = counted(tb[task], b_is_candidate, cap_b)
        if ca and cb:
            rows.append((statistics.fmean(x for x, _ in ca), statistics.fmean(x for x, _ in cb),
                         sum(c for _, c in ca), sum(c for _, c in cb)))
    return rows, sum(cut(ta, t) for t in common), sum(cut(tb, t) for t in common)


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
    args = ap.parse_args(argv)

    a, b = load(args.run_a), load(args.run_b)
    rows, cut_a, cut_b = paired(a, b)
    if not rows:
        sys.exit("compare: no task has counted tries in both runs")
    n = len(rows)
    diff, se = diff_stats(rows)
    solved_a = sum(ra >= 0.5 for ra, _, _, _ in rows)
    solved_b = sum(rb >= 0.5 for _, rb, _, _ in rows)
    cost_a = sum(r[2] for r in rows)
    cost_b = sum(r[3] for r in rows)
    print(f"n={n} diff={diff:.3f} se={se:.3f} ci95=±{1.96 * se:.3f} solvedA={solved_a} solvedB={solved_b} "
          f"costA={cost_a:.2f} costB={cost_b:.2f} cutA={cut_a} cutB={cut_b}")
    if args.margin_from:
        # A2 is a second baseline: its cut tries are excluded too.
        base_rows, _, _ = paired(a, load(args.margin_from), b_is_candidate=False)
        if not base_rows:
            sys.exit("compare: no task has counted tries in both baseline runs")
        margin_raw = 1.96 * diff_stats(base_rows)[1]
        margin = max(margin_raw, 1 / n)
        verdict = "win" if diff > margin + EPS else "lose" if diff < -margin - EPS else "tie"
        print(f"margin_raw={margin_raw:.3f} margin={margin:.3f} verdict={verdict}")
    if args.screen_rule:
        ok = solved_b >= solved_a - 1 and cost_b <= 1.25 * cost_a + EPS and cut_b <= cut_a
        print(f"screen={'pass' if ok else 'fail'}")


if __name__ == "__main__":
    main()
