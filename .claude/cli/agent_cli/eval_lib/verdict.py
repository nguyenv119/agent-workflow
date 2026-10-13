"""Verdict library for loop evals (Python 3.9+). Same names as verdict.sh:

    import os, subprocess, sys
    lib = subprocess.check_output(["agent-cli", "eval", "lib", "--py"], text=True).strip()
    sys.path.insert(0, os.path.dirname(lib))
    from verdict import ok, fail, refuse, blocked, win

ok(msg) records a pass; fail(msg) exits 1; refuse(msg) exits 2; blocked(msg) exits 3; win() prints the
WIN sentinel as the last line and exits 0, only if no fail was recorded in this process; with EVAL_ONLY set
(agent-cli eval run --only) win() exits 5 (partial) and never prints it.
"""
import os
import sys

_failed = False


def _say(prefix, msg):
    print(f"{prefix}: {msg}", flush=True)


def ok(msg):
    _say("ok", msg)


def fail(msg):
    global _failed
    _failed = True
    _say("fail", msg)
    sys.exit(1)


def refuse(msg):
    _say("refused", msg)
    sys.exit(2)


def blocked(msg):
    _say("blocked", msg)
    sys.exit(3)


def win():
    if os.environ.get("EVAL_ONLY"):
        _say("partial", f"only {os.environ['EVAL_ONLY']} ran, no WIN")
        sys.exit(5)
    if _failed:
        _say("fail", "win refused, a check failed earlier")
        sys.exit(1)
    print("<promise>WIN</promise>", flush=True)
    sys.exit(0)
