#!/usr/bin/env python3
"""Fake `gh` for loop.py tests: logs each call (argv, plus the --body / --body-file text) to $FAKE_GH_LOG.

`pr create` prints a PR url; `pr view` prints {"state": $FAKE_GH_STATE, "body": the last
body sent for that PR, "headRefOid": $FAKE_GH_HEAD, "headRefName": "pr-head"}. `pr list` prints [] or, when
$FAKE_GH_OPEN_PR is set, one open PR with that number. `pr edit` exits 1 if $FAKE_GH_FAIL_EDIT is set. Everything else
prints nothing.
"""
import json
import os
import sys

args = sys.argv[1:]
rec = {"args": args}
if "--body" in args:
    rec["body"] = args[args.index("--body") + 1]
if "--body-file" in args:
    with open(args[args.index("--body-file") + 1]) as fh:
        rec["body"] = fh.read()
log = os.environ["FAKE_GH_LOG"]
prior = []
if os.path.exists(log):
    with open(log) as fh:
        prior = [json.loads(line) for line in fh]
with open(log, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
if args[:2] == ["pr", "edit"] and os.environ.get("FAKE_GH_FAIL_EDIT"):
    sys.exit(1)
if args[:2] == ["pr", "create"]:
    print("https://github.com/nguyenv119/agent-workflow/pull/77")
elif args[:2] == ["pr", "list"]:
    n = os.environ.get("FAKE_GH_OPEN_PR")
    print(json.dumps([{"number": int(n), "url": f"https://github.com/nguyenv119/agent-workflow/pull/{n}"}] if n else []))
elif args[:2] == ["pr", "view"]:
    bodies = [r["body"] for r in prior if "body" in r]
    print(json.dumps({"state": os.environ.get("FAKE_GH_STATE", "OPEN"), "body": bodies[-1] if bodies else "",
                      "headRefOid": os.environ.get("FAKE_GH_HEAD", ""), "headRefName": "pr-head"}))
