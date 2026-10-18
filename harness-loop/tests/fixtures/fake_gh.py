#!/usr/bin/env python3
"""Fake `gh` for loop.py tests: logs each call (argv, plus the --body / --body-file text) to $FAKE_GH_LOG.

`pr create` prints a PR url; `pr view` prints, of {"state": $FAKE_GH_STATE, "body": the last
body sent for that PR, "headRefOid": $FAKE_GH_HEAD, "headRefName": "pr-head", "isCrossRepository": $FAKE_GH_CROSS
set, "headRepositoryOwner": {"login": ...}}, ONLY the fields named in `--json` (as real gh does), and exits 1 if
$FAKE_GH_FAIL_VIEW is set. `pr list` prints [] or, when $FAKE_GH_OPEN_PR is set, one open PR with that number.
`pr edit` exits 1 (stderr "edit refused") if $FAKE_GH_FAIL_EDIT is set, `api` if $FAKE_GH_FAIL_API is. Everything
else prints nothing.

REVIEW: mocking core dependency. Why: loop.py's PR and status calls go to GitHub, which no test may reach.
What breaks if it drifts from real gh: a fake that printed every field would let the code read a field it never
asked for (e.g. drop isCrossRepository from --json and the fork check still "works" here, never on GitHub).
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
    print("edit refused", file=sys.stderr)
    sys.exit(1)
if args[:1] == ["api"] and os.environ.get("FAKE_GH_FAIL_API"):
    sys.exit(1)
if args[:2] == ["pr", "view"] and os.environ.get("FAKE_GH_FAIL_VIEW"):
    sys.exit("gh: could not resolve to a PullRequest")
if args[:2] == ["pr", "create"]:
    print("https://github.com/nguyenv119/agent-workflow/pull/77")
elif args[:2] == ["pr", "list"]:
    n = os.environ.get("FAKE_GH_OPEN_PR")
    print(json.dumps([{"number": int(n), "url": f"https://github.com/nguyenv119/agent-workflow/pull/{n}"}] if n else []))
elif args[:2] == ["pr", "view"]:
    bodies = [r["body"] for r in prior if "body" in r]
    view = {"state": os.environ.get("FAKE_GH_STATE", "OPEN"), "body": bodies[-1] if bodies else "",
            "headRefOid": os.environ.get("FAKE_GH_HEAD", ""), "headRefName": "pr-head",
            "isCrossRepository": bool(os.environ.get("FAKE_GH_CROSS")),
            "headRepositoryOwner": {"login": "someone-else" if os.environ.get("FAKE_GH_CROSS") else "owner"}}
    fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
    print(json.dumps({k: v for k, v in view.items() if k in fields}))
