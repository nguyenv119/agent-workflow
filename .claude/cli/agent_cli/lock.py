"""`agent-cli lock`: a named mutex in a state dir that reaps a lock whose holder died or ran past its cap.
With --slots N it is a semaphore: N lock dirs <name>.1 .. <name>.N, and acquire takes the first free one.

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
    p.add_argument("--slots", type=int, default=1,
                   help="up to this many holders at once, as <name>.1..<name>.N (default 1: a plain mutex)")
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


def slot_names(name, slots):
    return [name] if slots <= 1 else [f"{name}.{i}" for i in range(1, slots + 1)]


def held_names(name):
    """name and its slot dirs name.<n> that exist, for release."""
    if not root().is_dir():
        return []
    return [p.name for p in sorted(root().glob(f"{name}*"))
            if p.is_dir() and (p.name == name or re.fullmatch(re.escape(name) + r"\.\d+", p.name))]


def try_take(name, pid, cap, dry_run=False, slots=1):
    """One acquire attempt under the guard; never exits. Returns (status, slot name, owners):
    ("acquired", slot, [reaped owner]) / ("acquired", slot, []), ("already-held", slot, []) or
    ("held", None, every live holder)."""
    with guard():
        here = socket.gethostname()
        held = [(n, read(n)) for n in slot_names(name, slots)]
        for n, o in held:
            if o and o["pid"] == pid and o["host"] == here:
                return "already-held", n, []
        for n, o in held:
            if not o or stale(o):
                if not dry_run:
                    if o:
                        shutil.rmtree(root() / n)
                    take(n, pid, cap)
                return "acquired", n, [o] if o else []
        return "held", None, [o for _, o in held]


def take_waiting(name, pid, cap, wait_s, example, dry_run=False, slots=1):
    """try_take every second for up to wait_s; exit 3 naming the holders (and example) if every slot stays held."""
    holders = []

    def check(_left):
        status, slot, owners = try_take(name, pid, cap, dry_run, slots)
        holders[:] = owners
        return None if status == "held" else (status, slot, owners)

    got = wait.poll(check, 1, wait_s)
    if got is None:
        what = name if slots <= 1 else f"all {slots} slots of {name}"
        core.die(Exit.BLOCKED, f"{what} held by {'; '.join(describe(o) for o in holders)}", example)
    return got


def remove_if_owner(name, pid, force=False, dry_run=False):
    """Under the guard, remove name (or the slot name.<n> that pid holds) when pid on this host holds it
    (or with force); dry_run removes nothing. Returns (owner or None when not held, whether it was or would
    be removed). The one release path."""
    with guard():
        found = [(n, read(n)) for n in held_names(name)]
        mine = [(n, o) for n, o in found if o["pid"] == pid and o["host"] == socket.gethostname()]
        n, held = (mine or found or [(name, None)])[0]
        ok = bool(held) and (force or bool(mine))
        if ok and not dry_run:
            shutil.rmtree(root() / n)
        return held, ok


def acquire(a):
    check_name(a.name, EXAMPLES[0])
    if a.pid <= 0:
        core.die(Exit.REFUSED, f"--pid {a.pid} is not a process id", ONE_SCRIPT)
    if a.slots < 1:
        core.die(Exit.REFUSED, f"--slots {a.slots}: give 1 or more", ONE_SCRIPT)
    cap, wait_s = core.duration(a.cap, ONE_SCRIPT), core.duration(a.wait, ONE_SCRIPT)
    slots = f" --slots {a.slots}" if a.slots > 1 else ""
    status, slot, reaped = take_waiting(a.name, a.pid, cap, wait_s,
                                        f"agent-cli lock acquire {a.name}{slots} --pid $$ --wait 10m",
                                        a.dry_run, a.slots)
    if status == "already-held":
        return core.out(a, {"status": status, "name": slot, "pid": a.pid}, f"already held: {slot} (pid {a.pid})")
    reaped = reaped and describe(reaped[0])
    if reaped and not a.json:
        print(f"reaped: {reaped}")
    verb = "would-acquire" if a.dry_run else "acquired"
    return core.out(a, {"status": verb, "name": slot, "pid": a.pid, "reaped": reaped},
                    f"{verb}: {slot} (pid {a.pid})")


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
    held, ok = remove_if_owner(a.name, pid, a.force, a.dry_run)
    if not held:
        return core.out(a, {"status": "already-done", "name": a.name}, f"already done: {a.name} is not held")
    if not ok:
        core.die(Exit.REFUSED, f"{a.name} is held by {describe(held)}, not pid {pid} on this host",
                 f"agent-cli lock release {a.name} --pid $$ --force")
    verb = "would-release" if a.dry_run else "released"
    return core.out(a, {"status": verb, "name": a.name, "owner": describe(held)},
                    f"{verb}: {a.name} ({describe(held)})")


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
        core.out(a, r, f"{r['name']}  pid {r['pid']} on {r['host']}  age {r['age_s']}s  cap {r['cap_s']}s  "
                  f"{'alive' if r['alive'] else 'dead'}{'  stale' if r['stale'] else ''}")
    if a.name and not rows:
        core.out(a, {"name": a.name, "status": "not-held"}, f"not held: {a.name}")
    elif not rows and not a.json:
        print("no locks")
