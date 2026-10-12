#!/usr/bin/env python3
"""Fake run_exam.py for loop.py tests (LOOP_RUN_EXAM points here). No Harbor, no spend.

Writes a sealed, read-only run dir the way run_exam.py does, under
$HARNESS_LOOP_HOME/runs/, from $FAKE_EXAM_SPEC: {"<kind>": {"<task>": [reward, cost]}}.
Logs its argv to $FAKE_EXAM_LOG and prints run_exam's last line.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "exam"))
import compare  # noqa: E402

with open(os.environ["FAKE_EXAM_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
ap = argparse.ArgumentParser()
ap.add_argument("--harness", required=True)
ap.add_argument("--tasks", required=True)
ap.add_argument("--budget-usd", type=float, required=True)
ap.add_argument("--screen", action="store_true")
ap.add_argument("--tries", type=int, default=1)
ap.add_argument("--kind", required=True)
args = ap.parse_args()

spec = json.loads(os.environ["FAKE_EXAM_SPEC"])[args.kind]
runs = Path(os.environ["HARNESS_LOOP_HOME"]) / "runs"
runs.mkdir(parents=True, exist_ok=True)
base = f"fake-{args.harness[:7]}{'-screen' if args.screen else ''}"
n = 1
while (runs / (base if n == 1 else f"{base}-{n}")).exists():
    n += 1
run = runs / (base if n == 1 else f"{base}-{n}")
total = 0.0
for task, (reward, cost) in spec.items():
    for t in range(1, args.tries + 1):
        d = run / task / f"try-{t}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"task": task, "try": t, "agent": "workflow", "status": "ok",
                                                   "reward": reward, "cost_usd": cost}))
        total += cost
(run / "run.json").write_text(json.dumps({
    "run_id": run.name, "kind": args.kind, "harness_commit": args.harness, "screen": args.screen,
    "manifest": str(Path(args.tasks).resolve()), "agent": "workflow", "tries": args.tries,
    "status": "complete", "max_budget_usd": 12.05}))
(run / "summary.json").write_text(json.dumps({"total_cost_usd": round(total, 4)}))
compare.seal(run)
subprocess.run(["chmod", "-R", "a-w", str(run)], check=True)
print(f"run_exam: {run} complete solved=? cost=${total:.2f}")
