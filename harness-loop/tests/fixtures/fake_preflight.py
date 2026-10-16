#!/usr/bin/env python3
"""Fake `claude` for the exam's credential preflight. No model call.

    EXAM_PREFLIGHT_CLAUDE="python3 fake_preflight.py <log> [fail=<route>,...] [limit=<route>,...]"

loop.py runs the preflight with only the candidate's credential in its env, so the fake takes its settings
on its command line. It names the route from that credential (CLAUDE_CODE_USE_BEDROCK: bedrock,
ANTHROPIC_API_KEY: api, else subscription) and appends {"route", "argv", "env"} (env keys only) to <log>.
A `fail` route gets an error result whose text carries every credential value it saw, as a careless error
message might; a `limit` route gets a usage-limit error. Any other route gets {"is_error": false}.
"""
import json
import os
import sys

log, opts = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a and not a.startswith("-"))
route = ("bedrock" if os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1" else "api"
         if os.environ.get("ANTHROPIC_API_KEY") else "subscription")
with open(log, "a") as fh:
    fh.write(json.dumps({"route": route, "argv": sys.argv[2:], "env": sorted(os.environ)}) + "\n")
if route in opts.get("limit", "").split(","):
    print(json.dumps({"type": "result", "is_error": True, "result": "You've hit your session limit"}))
    sys.exit(1)
if route in opts.get("fail", "").split(","):
    seen = " ".join(os.environ.get(k, "") for k in ("CLAUDE_CODE_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID",
                                                     "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"))
    print(json.dumps({"type": "result", "is_error": True, "result": f"401 invalid credential {seen}"}))
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0.0001}))
