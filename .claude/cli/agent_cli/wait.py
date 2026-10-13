"""`agent-cli wait`: rerun a status command until it succeeds, refuses or blocks, or a cap passes.

poll() is the shared loop: `wait`, `lock acquire --wait` and later waiters call it with their own check.
run_capped() runs one command in its own process group, stdin closed, and kills the whole group when it
returns (cap or not), so no background child outlives it; `wait` attempts and `eval run` use it.
"""
import json
import os
import signal
import subprocess
import tempfile
import time

from . import core
from .core import Exit

EXAMPLES = ["agent-cli wait --until 'gh pr checks 12' --every 60 --cap 30m",
            "agent-cli wait --until 'test -f /tmp/done' --every 5 --cap 20s --json"]
# --until exit code -> (our exit, result); 126/127 = not executable / not found, which retrying never fixes.
RESULTS = {0: (Exit.OK, "met"), 2: (Exit.REFUSED, "refused"), 3: (Exit.BLOCKED, "blocked"),
           126: (Exit.REFUSED, "refused"), 127: (Exit.REFUSED, "refused")}
KEEP = 4096  # bytes of each attempt's output kept, to find its last line
RESTART = object()  # returned by a check: start the cap over (e.g. a PR got a new head)


def register(sub):
    p = core.subcommand(sub, "wait", "rerun a status command until it exits 0 (exit 0), exits 2 or 3 (passed "
                        "through; 126/127 -> 2) or --cap passes (exit 4); run it in the background, not a sleep loop",
                        EXAMPLES)
    p.add_argument("--until", required=True, metavar="CMD",
                   help="status command, run with bash -c each attempt; one attempt is killed at the cap")
    p.add_argument("--every", default="60", help="time between attempts, at least 5s (default 60; e.g. 30, 2m)")
    p.add_argument("--cap", default="30m", help="give up after this long and exit 4 (default 30m)")
    p.add_argument("--json", action="store_true", help="one JSON object per attempt")
    p.set_defaults(func=wait)


def poll(check, every, cap, clock=time.monotonic, sleep=time.sleep):
    """Call check(seconds_left) until it returns non-None (returned) or cap seconds pass (None).

    check always runs at least once; sleeps are `every` seconds, the last one cut short at the cap.
    A check returning RESTART starts the cap over from now and keeps polling.
    """
    start = clock()
    while True:
        got = check(cap - (clock() - start))
        if got is RESTART:
            start = clock()
        elif got is not None:
            return got
        left = cap - (clock() - start)
        if left <= 0:
            return None
        sleep(min(every, left))


def run_capped(argv, timeout, **kw):
    """Exit code of argv, or None if it ran past timeout. Its whole process group is killed either way,
    so children it left in the background die with it; stdin is /dev/null.

    kw pass to subprocess.Popen (stdout, stderr, cwd, env). Raises OSError when argv cannot start.
    """
    p = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, **kw)
    try:
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()


def attempt(cmd, timeout):
    """(exit code or None on timeout, last output line) of one `bash -c cmd`; its process group dies with it."""
    with tempfile.TemporaryFile() as out:
        code = run_capped(["bash", "-c", cmd], timeout, stdout=out)
        out.seek(max(0, out.tell() - KEEP))
        lines = out.read().decode(errors="replace").strip().splitlines()
    return code, (lines or [""])[-1]


def wait(a, clock=time.monotonic, sleep=time.sleep):
    every, cap = core.duration(a.every, EXAMPLES[0]), core.duration(a.cap, EXAMPLES[0])
    if every < 5:
        core.die(Exit.REFUSED, f"--every {a.every} is under the 5s minimum", EXAMPLES[0])
    start, n = clock(), [0]

    def show(row):
        print(json.dumps(row) if a.json else " ".join(f"{k}={v}" for k, v in row.items()), flush=True)

    def check(left):
        n[0] += 1
        code, last = attempt(a.until, max(1, left))
        row = {"attempt": n[0], "exit": code, "elapsed_s": round(clock() - start, 1), "last": last}
        if code is None:
            row["timed_out"] = True
        done = RESULTS.get(code)
        if done:
            row["result"] = done[1]
        show(row)
        return done and done[0]

    got = poll(check, every, cap, clock, sleep)
    if got is None:
        show({"attempts": n[0], "elapsed_s": round(clock() - start, 1), "result": "timeout"})
        return Exit.TIMEOUT
    return got
