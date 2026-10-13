"""`agent-cli lock`: a named mutex in a state dir that reaps a lock whose holder died or ran past its cap.

Every read-modify-write of a lock dir runs under an flock on locks/.guard, so two reapers never both win.
"""
import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

from . import core, wait
from .core import Exit

PRINCIPLE = ("Separate before serializing shared state: give each lane its own copy first (its own DB branch, "
             "its own worktree); lock only what truly is shared.\n"
             "Locks are machine-global: one dir per machine, ${AGENT_CLI_STATE:-~/.cache/agent-cli}/locks, shared "
             "by every repo (AGENT_CLI_STATE points tests at a private dir).\n"
             "The owner is --pid, a process that outlives the work: run acquire, work and release in one script.")
ONE_SCRIPT = ("bash -c 'agent-cli lock acquire gates --pid $$ --wait 30m || exit $?; make test; "
              "agent-cli lock release gates --pid $$'")
EXAMPLES = [ONE_SCRIPT, "agent-cli lock status --json", "agent-cli lock release gates --pid 4121"]
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def register(sub):
    p, ls = core.group(sub, "lock", "named mutex for parallel lanes; reaps a lock whose holder died", EXAMPLES)
    p.description = PRINCIPLE
    p = core.subcommand(ls, "acquire", "take a lock; exit 3 naming the holder if a live process holds it",
                        [ONE_SCRIPT, "agent-cli lock acquire gates --pid $$ --cap 120m --wait 10m"])
    p.description = PRINCIPLE
    p.add_argument("name")
    p.add_argument("--cap", default="120m", help="others may reap the lock after this long (default 120m)")
    p.add_argument("--wait", default="0", help="keep trying this long before exit 3 (default 0)")
    p.add_argument("--pid", type=int, required=True, help="owner pid: the script that does the work ($$)")
    p.add_argument("--dry-run", action="store_true", help="say what would happen, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=acquire)
    p = core.subcommand(ls, "release", "drop a lock you own (--force: any owner); not held = already done",
                        ["agent-cli lock release gates --pid $$",
                         "agent-cli lock release gates --pid $$ --force --dry-run"])
    p.add_argument("name")
    p.add_argument("--pid", type=int, required=True, help="owner pid given to acquire")
    p.add_argument("--force", action="store_true", help="release even if another process owns it")
    p.add_argument("--dry-run", action="store_true", help="say what would happen, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=release)
    p = core.subcommand(ls, "status", "list locks with owner, age and whether the owner is alive",
                        ["agent-cli lock status", "agent-cli lock status gates --json"])
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true", help="one JSON object per lock")
    p.set_defaults(func=status)


def root():
    return Path(os.environ.get("AGENT_CLI_STATE") or Path.home() / ".cache/agent-cli") / "locks"


@contextlib.contextmanager
def guard():
    root().mkdir(parents=True, exist_ok=True)
    with open(root() / ".guard", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def read(name):
    """The owner dict of a held lock, or None. A dir with no readable owner.json counts as held by pid 0."""
    d = root() / name
    if not d.is_dir():
        return None
    try:
        return json.loads((d / "owner.json").read_text())
    except (OSError, ValueError):
        return {"pid": 0, "host": "?", "started": d.stat().st_mtime, "cap": 0, "cmdline": ""}


def alive(o):
    """False when the owner pid is gone; another host's pid cannot be checked, so it counts as alive."""
    if o["host"] != socket.gethostname():
        return True
    if o["pid"] <= 0:
        return False
    try:
        os.kill(o["pid"], 0)
    except (ProcessLookupError, OverflowError):
        return False
    except PermissionError:
        pass
    return True


def stale(o):
    return not alive(o) or time.time() - o["started"] > o["cap"]


def describe(o):
    return (f"pid {o['pid']} on {o['host']} since {time.strftime('%H:%M:%S', time.localtime(o['started']))} "
            f"(cap {o['cap'] // 60}m)")


def check_name(name, example):
    if not NAME.fullmatch(name):
        core.die(Exit.REFUSED, f"bad lock name {name!r}: letters, digits, . _ - only", example)


def out(a, row, text):
    if a.json:
        core.emit(row, True)
    else:
        print(text)


def try_take(name, pid, cap, dry_run=False):
    """One acquire attempt under the guard; never exits. Returns (status, owner):
    ("acquired", reaped owner or None), ("already-held", None) or ("held", the live holder)."""
    with guard():
        held = read(name)
        if held and held["pid"] == pid and held["host"] == socket.gethostname():
            return "already-held", None
        if held and not stale(held):
            return "held", held
        if not dry_run:
            if held:
                shutil.rmtree(root() / name)
            take(name, pid, cap)
        return "acquired", held


def acquire(a):
    check_name(a.name, EXAMPLES[0])
    if a.pid <= 0:
        core.die(Exit.REFUSED, f"--pid {a.pid} is not a process id", ONE_SCRIPT)
    cap, wait_s = core.duration(a.cap, ONE_SCRIPT), core.duration(a.wait, ONE_SCRIPT)
    holder = []

    def check(_left):
        status, owner = try_take(a.name, a.pid, cap, a.dry_run)
        holder[:] = [owner]
        return None if status == "held" else (status, owner)

    got = wait.poll(check, 1, wait_s)
    if got is None:
        core.die(Exit.BLOCKED, f"{a.name} held by {describe(holder[0])}",
                 f"agent-cli lock acquire {a.name} --pid $$ --wait 10m")
    status, reaped = got
    if status == "already-held":
        return out(a, {"status": status, "name": a.name, "pid": a.pid}, f"already held: {a.name} (pid {a.pid})")
    reaped = reaped and describe(reaped)
    if reaped and not a.json:
        print(f"reaped: {reaped}")
    verb = "would-acquire" if a.dry_run else "acquired"
    return out(a, {"status": verb, "name": a.name, "pid": a.pid, "reaped": reaped}, f"{verb}: {a.name} (pid {a.pid})")


def take(name, pid, cap):
    try:
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        cmdline = ""
    d = root() / name
    d.mkdir()
    (d / "owner.json").write_text(json.dumps({"pid": pid, "host": socket.gethostname(), "started": time.time(),
                                              "cap": cap, "cmdline": cmdline}))


def release(a):
    check_name(a.name, EXAMPLES[2])
    pid = a.pid
    if pid <= 0:
        core.die(Exit.REFUSED, f"--pid {pid} is not a process id", ONE_SCRIPT)
    with guard():
        held = read(a.name)
        if not held:
            return out(a, {"status": "already-done", "name": a.name}, f"already done: {a.name} is not held")
        if held["pid"] != pid and not a.force:
            core.die(Exit.REFUSED, f"{a.name} is held by {describe(held)}, not pid {pid}",
                     f"agent-cli lock release {a.name} --pid $$ --force")
        if not a.dry_run:
            shutil.rmtree(root() / a.name)
    verb = "would-release" if a.dry_run else "released"
    return out(a, {"status": verb, "name": a.name, "owner": describe(held)}, f"{verb}: {a.name} ({describe(held)})")


def status(a):
    if a.name:
        check_name(a.name, EXAMPLES[1])
    names = [a.name] if a.name else sorted(p.name for p in root().glob("*") if p.is_dir()) if root().is_dir() else []
    rows = []
    for n in names:
        o = read(n)
        if o:
            rows.append({"name": n, "status": "held", "pid": o["pid"], "host": o["host"],
                         "age_s": int(time.time() - o["started"]), "cap_s": o["cap"], "alive": alive(o), "stale": stale(o), "cmdline": o["cmdline"]})
    for r in rows:
        out(a, r, f"{r['name']}  pid {r['pid']} on {r['host']}  age {r['age_s']}s  cap {r['cap_s']}s  "
                  f"{'alive' if r['alive'] else 'dead'}{'  stale' if r['stale'] else ''}")
    if a.name and not rows:
        out(a, {"name": a.name, "status": "not-held"}, f"not held: {a.name}")
    elif not rows and not a.json:
        print("no locks")
