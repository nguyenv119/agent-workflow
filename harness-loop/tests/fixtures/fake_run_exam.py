#!/usr/bin/env python3
"""Fake run_exam.py for loop.py tests (LOOP_RUN_EXAM points here). No Harbor, no spend.

Writes a sealed, read-only run dir the way run_exam.py does, under
$HARNESS_LOOP_HOME/runs/, from $FAKE_EXAM_SPEC: {"<kind>": {"<task>": [reward, cost]}}
("side-<kind>" when called with --addon).
Logs its argv to $FAKE_EXAM_LOG and prints run_exam's last line. $FAKE_EXAM_INCOMPLETE
(space-separated kinds) marks those runs incomplete, as a budget stop would; $FAKE_EXAM_EARLY_STOP (a
reason) does that to a run given --early-stop-vs and records the reason as run.json early_stop;
$FAKE_EXAM_RATE_LIMITED (space-separated kinds) marks those runs incomplete with run.json rate_limited,
as a usage limit would.
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
ap.add_argument("--addon")
ap.add_argument("--env")
ap.add_argument("--early-stop-vs")
args = ap.parse_args()

# A side check (run with --addon) reads "side-<kind>" from the spec and $FAKE_EXAM_INCOMPLETE.
key = f"side-{args.kind}" if args.addon else args.kind
spec = json.loads(os.environ["FAKE_EXAM_SPEC"])[key]
runs = Path(os.environ["HARNESS_LOOP_HOME"]) / "runs"
runs.mkdir(parents=True, exist_ok=True)
set_name = json.loads(Path(args.tasks).read_text()).get("set")  # run_exam names runs after the set
base = f"fake-{args.harness[:7]}{'-' + set_name if set_name else ''}{'-screen' if args.screen else ''}"
n = 1
while (runs / (base if n == 1 else f"{base}-{n}")).exists():
    n += 1
run = runs / (base if n == 1 else f"{base}-{n}")
total = 0.0
status = "incomplete" if key in os.environ.get("FAKE_EXAM_INCOMPLETE", "").split() else "complete"
early_stop = os.environ.get("FAKE_EXAM_EARLY_STOP") if args.early_stop_vs else None
status = "incomplete" if early_stop else status
limited = key in os.environ.get("FAKE_EXAM_RATE_LIMITED", "").split()
status = "incomplete" if limited else status
for task, (reward, cost) in spec.items():
    for t in range(1, args.tries + 1):
        d = run / task / f"try-{t}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"task": task, "try": t, "agent": "workflow", "status": "ok",
                                                   "reward": reward, "cost_usd": cost}))
        total += cost
manifest = json.loads(Path(args.tasks).read_text())
(run / "run.json").write_text(json.dumps({
    "run_id": run.name, "kind": args.kind, "harness_commit": args.harness, "screen": args.screen,
    "set": manifest.get("set"), "model": os.environ.get("FAKE_EXAM_MODEL", "m"), "tasks": sorted(t["id"] for t in manifest.get("tasks", []) if t["screen"] or not args.screen),
    "manifest": str(Path(args.tasks).resolve()), "agent": "workflow", "tries": args.tries,
    "status": status, **({"early_stop": early_stop} if early_stop else {}),
    **({"rate_limited": "You've hit your session limit"} if limited else {}), "max_budget_usd": float(os.environ.get("FAKE_EXAM_CAP", "12.05"))}))
(run / "summary.json").write_text(json.dumps({"total_cost_usd": round(total, 4)}))
compare.seal(run)
subprocess.run(["chmod", "-R", "a-w", str(run)], check=True)
print(f"run_exam: {run} {status} solved=? cost=${total:.2f}")
