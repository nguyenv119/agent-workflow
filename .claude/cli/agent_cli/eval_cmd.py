"""`agent-cli eval`: scaffold, run and control-test loop evals, plus the verdict library they use.

A run is WIN only when the entry exits 0 AND its last non-empty stdout line is the sentinel (the
win-condition skill's dual condition); every run leaves evidence/<UTC ts>.log and .json in the eval dir.
"""
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

from . import core, lock, wait
from .core import Exit
from .hook_rules import SENTINEL

EVAL_LIB = Path(__file__).resolve().parent / "eval_lib"
DIR = ".claude/loop-evals/<epic-id>"
ENTRIES = ("win.sh", "win.py")
# script exit -> verdict; exit 0 is WIN or NO_SENTINEL, anything unlisted (a script's own 4 included) is FAIL.
# TIMEOUT is only the cap firing; ERROR is an entry that could not start.
VERDICTS = {2: "REFUSED", 3: "BLOCKED", 5: "PARTIAL"}
EXITS = {"WIN": Exit.OK, "NO_SENTINEL": Exit.FAIL, "FAIL": Exit.FAIL, "ERROR": Exit.FAIL, "REFUSED": Exit.REFUSED,
         "BLOCKED": Exit.BLOCKED, "TIMEOUT": Exit.TIMEOUT, "PARTIAL": Exit.PARTIAL}
NO_FAIL_CONTROL = "An eval with no fail control has not been tested."
TAIL = 65536  # bytes of stdout read back to find the last line

WIN_SH = """#!/usr/bin/env bash
# Loop eval. Run it with: agent-cli eval run <this dir>   (exit 0 = WIN)
# Only the verdict library prints the WIN sentinel: ok records a check, fail exits 1, win ends a passing run.
# With `agent-cli eval run --only <check>`, EVAL_ONLY names the one check to run and win exits 5 (partial).
source "$(agent-cli eval lib --sh)"

# TODO: replace with a positive assertion on the real outcome (R1: rows landed, a file has content),
# read from the live system. Until edited, count stays 0 and this calls fail, so the scaffold never WINs.
count=0
if [ "$count" -ge 1 ]; then ok "rows landed: $count"; else fail "TODO: no positive assertion yet (count=$count)"; fi

win
"""
WIN_PY = """#!/usr/bin/env python3
# Loop eval. Run it with: agent-cli eval run <this dir>   (exit 0 = WIN)
# Only the verdict library prints the WIN sentinel: ok records a check, fail exits 1, win ends a passing run.
# With `agent-cli eval run --only <check>`, EVAL_ONLY names the one check to run and win exits 5 (partial).
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(subprocess.check_output(["agent-cli", "eval", "lib", "--py"], text=True).strip()))
from verdict import fail, ok, win  # noqa: E402

# TODO: replace with a positive assertion on the real outcome (R1: rows landed, a file has content),
# read from the live system. Until edited, count stays 0 and this calls fail, so the scaffold never WINs.
count = 0
if count >= 1:
    ok(f"rows landed: {count}")
else:
    fail(f"TODO: no positive assertion yet (count={count})")

win()
"""
CONTROLS_README = """# Controls

A control proves the eval discriminates: it runs the eval on a case whose answer is known.

- `fail-<name>`: a case the eval must reject (empty input, a broken assertion). It must NOT print WIN.
- `pass-<name>`: a known-good case. It must print WIN.

Each control is either
- `<name>.env`: KEY=VALUE lines; the eval's entry runs with these variables and CONTROL=<name>, or
- an executable `<name>`: it runs itself, with CONTROL=<name> and EVAL_ENTRY=<path of the entry>.

Both run with the eval dir as cwd. Run them all with `agent-cli eval controls <eval dir>`; it exits 1 naming
any control that behaved wrongly, and 3 when there is no fail control: an eval with no fail control has not
been tested.
"""


def register(sub):
    _, es = core.group(sub, "eval", "scaffold, run and control-test loop evals; the verdict library",
                       [f"agent-cli eval run {DIR}", f"agent-cli eval controls {DIR}",
                        'source "$(agent-cli eval lib --sh)"'])
    p = core.subcommand(es, "lib", "print the verdict library path (ok, fail, refuse, blocked, win)",
                        ["agent-cli eval lib --sh", "agent-cli eval lib --py"])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sh", action="store_true", help="bash library, to source")
    g.add_argument("--py", action="store_true", help="python module, to import")
    p.set_defaults(func=lambda a: print(EVAL_LIB / ("verdict.sh" if a.sh else "verdict.py")))

    p = core.subcommand(es, "init", "scaffold win.sh (or win.py) and controls/README.md; never overwrites",
                        [f"agent-cli eval init {DIR}", f"agent-cli eval init {DIR} --lang py --dry-run"])
    p.add_argument("dir")
    p.add_argument("--lang", choices=("sh", "py"), default="sh", help="entry language (default sh)")
    p.add_argument("--dry-run", action="store_true", help="say what would be created, write nothing")
    p.set_defaults(func=init)

    p = core.subcommand(es, "run", "run an eval; exit 0 only for WIN (exit 0 AND the sentinel last), "
                        "1 fail, no sentinel or entry could not start, 2/3 passed through, 4 cap, 5 partial (--only)",
                        [f"agent-cli eval run {DIR}", f"agent-cli eval run {DIR} --only rows --json",
                         f"agent-cli eval run {DIR} --entry check.py --cap 20m"])
    p.description = ("Runs the entry with the eval dir as cwd and writes evidence/<UTC ts>.log (stdout, then "
                     "stderr) and .json\n{exit, last_line, verdict, duration_s, entry_sha256, git_head} "
                     "(git_head: HEAD of the repo at your cwd, or null; exit is null when the cap fired or the "
                     "entry could not start).")
    p.add_argument("dir")
    p.add_argument("--entry", help=f"script in the dir to run (default: the first of {', '.join(ENTRIES)})")
    p.add_argument("--only", metavar="CHECK", help="export EVAL_ONLY=CHECK; the run can then never WIN (exit 5)")
    p.add_argument("--cap", default="60m", help="kill the eval after this long and exit 4 (default 60m)")
    p.add_argument("--json", action="store_true", help="one JSON object")
    lock_args(p)
    p.set_defaults(func=run)

    p = core.subcommand(es, "controls", "run controls/fail-* (must not WIN) and controls/pass-* (must WIN); "
                        "exit 1 naming a mismatch, 3 if there is no fail control",
                        [f"agent-cli eval controls {DIR}", f"agent-cli eval controls {DIR} --json"])
    p.description = CONTROLS_README.split("\n", 2)[2].strip()
    p.add_argument("dir")
    p.add_argument("--entry", help=f"script in the dir to run (default: the first of {', '.join(ENTRIES)})")
    p.add_argument("--cap", default="60m", help="cap per control run, exit 4 at the first one past it (default 60m)")
    p.add_argument("--json", action="store_true", help="one JSON object per control, then a summary")
    lock_args(p)
    p.set_defaults(func=controls)


def lock_args(p):
    p.add_argument("--lock", metavar="NAME", help="hold `agent-cli lock` NAME for the whole run (exit 3 if busy)")
    p.add_argument("--lock-wait", default="0", help="with --lock, wait this long for a busy lock (default 0)")


@contextlib.contextmanager
def held(a, cap, example):
    """Hold lock a.lock (owner: this process) around the block; no --lock, no lock. The lock's cap is
    cap; SIGTERM becomes exit 143 so the eval's process group is killed and the lock released."""
    if not a.lock:
        yield
        return
    lock.check_name(a.lock, example)
    wait_s = core.duration(a.lock_wait, example)
    old = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        lock.take_waiting(a.lock, os.getpid(), cap, wait_s, f"{example} --lock {a.lock} --lock-wait 30m")
        yield
    finally:
        signal.signal(signal.SIGTERM, old)
        lock.remove_if_owner(a.lock, os.getpid())


def init(a):
    d = Path(a.dir)
    files = [(d / f"win.{a.lang}", WIN_SH if a.lang == "sh" else WIN_PY, 0o755),
             (d / "controls/README.md", CONTROLS_README, 0o644)]
    for path, text, mode in files:
        if path.exists():
            print(f"exists: {path}")
        elif a.dry_run:
            print(f"would create: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(mode)
            print(f"created: {path}")
    return Exit.OK


def resolve(a, example):
    """(eval dir, entry path, argv to run it); dies with exit 2 when either is missing."""
    d = Path(a.dir)
    if not d.is_dir():
        core.die(Exit.REFUSED, f"no eval dir {d}", example)
    names = [a.entry] if a.entry else ENTRIES
    entry = next((d / n for n in names if (d / n).is_file()), None)
    if entry is None:
        core.die(Exit.REFUSED, f"no entry {' or '.join(names)} in {d}", f"agent-cli eval init {d}")
    entry = entry.resolve()
    runner = [sys.executable] if entry.suffix == ".py" else ["bash"] if entry.suffix == ".sh" else []
    if not runner and not os.access(entry, os.X_OK):
        core.die(Exit.REFUSED, f"entry {entry} is not executable and is not .sh or .py", f"chmod +x {entry}")
    return d, entry, runner + [str(entry)]


def verdict(code, last, only):
    """The verdict for exit code (None = the cap fired) and last non-empty stdout line; see VERDICTS."""
    if code is None:
        return "TIMEOUT"
    if code == 0 and last == SENTINEL:
        return "PARTIAL" if only else "WIN"  # a partial run can never WIN, even with a hand-printed sentinel
    return "NO_SENTINEL" if code == 0 else VERDICTS.get(code, "FAIL")


def evidence_paths(d, suffix=""):
    """(log, json) at evidence/<UTC ts><suffix>, numbered -2, -3... so a run never overwrites another's."""
    ev = d / "evidence"
    ev.mkdir(exist_ok=True)
    base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + suffix
    n = 1
    while True:
        stem = str(ev / (base if n == 1 else f"{base}-{n}"))
        try:
            with open(stem + ".log", "x"):
                return Path(stem + ".log"), Path(stem + ".json")
        except FileExistsError:
            n += 1


def execute(d, entry, argv, cap, extra_env, only=None, control=None, control_file=None):
    """Run one eval (or control) with evidence; returns the evidence record plus its path.

    An argv that cannot start (OSError: no shebang, not executable) is verdict ERROR, exit null, still with evidence.
    control_file: an executable control's path, hashed into control_sha256.
    """
    env = core.child_env(drop=("EVAL_ONLY", "CONTROL", "EVAL_ENTRY"))
    env.update(extra_env)
    if only:
        env["EVAL_ONLY"] = only
    log_path, json_path = evidence_paths(d, f"-{control}" if control else "")
    start = time.monotonic()
    error = None
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            code = wait.run_capped(argv, cap, stdout=out, stderr=err, cwd=str(d), env=env)
        except OSError as e:
            code, error = None, f"could not start {argv[0]}: {e}"
        duration = round(time.monotonic() - start, 1)
        out.seek(0, os.SEEK_END)
        out.seek(max(0, out.tell() - TAIL))
        lines = [ln for ln in out.read().decode(errors="replace").splitlines() if ln.strip()]
        with open(log_path, "wb") as log:
            for f, head in ((out, b""), (err, b"--- stderr ---\n")):
                if not f.seek(0, os.SEEK_END):
                    continue
                f.seek(-1, os.SEEK_END)
                newline = f.read(1) == b"\n"
                f.seek(0)
                log.write(head)
                shutil.copyfileobj(f, log)
                if not newline:
                    log.write(b"\n")
            if error:
                log.write(f"--- error ---\n{error}\n".encode())
    last = lines[-1].rstrip("\r\n") if lines else ""
    rec = {"exit": code, "last_line": last, "verdict": "ERROR" if error else verdict(code, last, only),
           "duration_s": float(duration), "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
           "git_head": core.git("rev-parse", "HEAD", check=False, timeout=10)}
    if error:
        rec["error"] = error
    if control:
        rec["control"] = control
    if control_file:
        rec["control_sha256"] = hashlib.sha256(Path(control_file).read_bytes()).hexdigest()
    if only:
        rec["only"] = only
    json_path.write_text(json.dumps(rec, indent=2) + "\n")
    return rec, json_path


def run(a):
    example = f"agent-cli eval run {DIR}"
    cap = core.duration(a.cap, example)
    if a.only is not None and not a.only.strip():
        core.die(Exit.REFUSED, "--only needs a check name", f"agent-cli eval run {DIR} --only rows")
    d, entry, argv = resolve(a, example)
    with held(a, cap + 60, example):
        rec, path = execute(d, entry, argv, cap, {}, only=a.only)
    core.emit({"dir": str(d), "verdict": rec["verdict"], "exit": rec["exit"], "duration_s": rec["duration_s"],
               "evidence": str(path)}, a.json)
    return EXITS[rec["verdict"]]


def find_controls(d):
    """[(name, path, kind)] for each *.env or executable file in d/controls, kind 'env' or 'exec'."""
    out = []
    cdir = d / "controls"
    for f in sorted(cdir.iterdir()) if cdir.is_dir() else []:
        if f.is_file() and f.suffix == ".env":
            out.append((f.stem, f, "env"))
        elif f.is_file() and os.access(f, os.X_OK):
            out.append((f.name, f, "exec"))
    return out


def env_file(path):
    """KEY=VALUE lines of a .env control (blank and # lines skipped, one layer of quotes dropped)."""
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip().removeprefix("export ").strip()] = v
    return out


def controls(a):
    example = f"agent-cli eval controls {DIR}"
    cap = core.duration(a.cap, example)
    d, entry, argv = resolve(a, example)
    found = [c for c in find_controls(d) if c[0].startswith(("fail-", "pass-"))]
    if not any(name.startswith("fail-") for name, _, _ in found):
        core.die(Exit.BLOCKED, f"no fail-* control in {d / 'controls'}. {NO_FAIL_CONTROL} (win-condition skill)",
                 f"printf 'INPUT=empty\\n' > {d / 'controls/fail-empty-input.env'}")
    with held(a, (cap + 60) * len(found), example):
        return run_controls(a, d, entry, argv, cap, found)


def run_controls(a, d, entry, argv, cap, found):
    mismatched = []
    for name, path, kind in found:
        extra = {"CONTROL": name}
        if kind == "env":
            extra = {**env_file(path), **extra}
            cmd, control_file = argv, None
        else:
            extra["EVAL_ENTRY"] = str(entry)
            control_file = path.resolve()
            cmd = [str(control_file)]
        rec, ev = execute(d, entry, cmd, cap, extra, control=name, control_file=control_file)
        if rec["verdict"] == "TIMEOUT":  # only the cap firing stops the run early
            core.emit({"control": name, "verdict": "TIMEOUT", "evidence": str(ev)}, a.json)
            return Exit.TIMEOUT
        won, want = rec["verdict"] == "WIN", name.startswith("pass-")
        broken = rec["verdict"] == "ERROR"  # a control that could not start tested nothing
        good = won == want and not broken
        if not good:
            mismatched.append(name)
        if a.json:
            print(json.dumps({"control": name, "win": won, "ok": good, "verdict": rec["verdict"],
                              "evidence": str(ev)}))
        else:
            note = ("ok" if good else f"ERROR: {rec['error']}" if broken
                    else "MISMATCH: a fail control must not WIN" if won else "MISMATCH: a pass control must WIN")
            print(f"{name}: {'WIN' if won else 'no WIN'} ({note})")
    ok = len(found) - len(mismatched)
    if a.json:
        print(json.dumps({"controls": len(found), "ok": ok, "mismatched": mismatched}))
    else:
        print(f"controls: {ok}/{len(found)}" + (f"  mismatched: {', '.join(mismatched)}" if mismatched else ""))
    return Exit.FAIL if mismatched else Exit.OK
