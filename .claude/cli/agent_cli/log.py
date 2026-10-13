"""`agent-cli log`: one append-only decision log (TSV) per loop run, safe under concurrent appenders.

Fields escape backslash, tab, newline and CR as \\\\, \\t, \\n, \\r, so a row is always one line of 8 fields.
Each append takes an flock on the file and writes its row with one O_APPEND write.
"""
import fcntl
import json
import math
import os
import re
import sys
import time

from . import core
from .core import Exit

FIELDS = ("ts", "phase", "action", "result", "evidence", "verdict", "cost_usd", "note")
HEADER = "\t".join(FIELDS)
APPEND = ('agent-cli log append .claude/loop-<epic-id>.tsv --phase iter-3 --action "RUN_BEAD <bead-id>" '
          '--result fail --evidence "acceptance 4/5" --verdict NO --cost-usd 0.80 --note "<diagnosis>"')
INIT = "agent-cli log append .claude/loop-<epic-id>.tsv --phase start --action init --result ok"
SHOW = "agent-cli log show .claude/loop-<epic-id>.tsv --last 5"
ESC = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
UNESC = {v[1]: k for k, v in ESC.items()}


def register(sub):
    _, ls = core.group(sub, "log", "append-only loop decision log (TSV): one escaped row per step", [INIT, SHOW])
    p = core.subcommand(ls, "append", "append one row (creates the file with a header); prints the row number; "
                        "the same fields twice in one second = already done", [INIT, APPEND])
    p.add_argument("file", help="the log, a .tsv path (e.g. .claude/loop-<epic-id>.tsv)")
    p.add_argument("--phase", required=True, help="e.g. start, iter-3, stop")
    p.add_argument("--action", required=True, help="what was done, e.g. 'RUN_BEAD <bead-id>'")
    p.add_argument("--result", required=True, help="e.g. ok, pass, fail, blocked")
    p.add_argument("--evidence", default="", help="path or key output line")
    p.add_argument("--verdict", default="", help="e.g. WIN, NO, BLOCKED")
    p.add_argument("--cost-usd", default="", help="approximate cost of this step, a number")
    p.add_argument("--note", default="", help="diagnosis, try folder; tabs and newlines are kept")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=append)
    p = core.subcommand(ls, "show", "print rows unescaped, oldest first",
                        [SHOW, "agent-cli log show .claude/loop-<epic-id>.tsv --phase iter-3 --json"])
    p.add_argument("file", help="the log .tsv")
    p.add_argument("--last", type=int, help="only the last N rows (after --phase)")
    p.add_argument("--phase", help="only rows with exactly this phase")
    p.add_argument("--json", action="store_true", help="one JSON object per row")
    p.set_defaults(func=show)


def escape(s):
    return re.sub(r"[\\\t\n\r]", lambda m: ESC[m[0]], s)


def unescape(s):
    return re.sub(r"\\(.)", lambda m: UNESC.get(m[1], m[0]), s)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def rows_of(text, path, example):
    """The data lines of a log's text (header checked), or exit 2 when it is not a log."""
    lines = text.split("\n")
    if lines[0] != HEADER:
        core.die(Exit.REFUSED, f"{path} is not an agent-cli log (first line is not the {len(FIELDS)}-field header)",
                 example)
    return [ln for ln in lines[1:] if ln]


def append(a, now=utc_now):
    if not a.file.endswith(".tsv"):
        core.die(Exit.REFUSED, f"{a.file} is not a .tsv path", INIT)
    for flag in ("phase", "action", "result"):
        if not getattr(a, flag).strip():
            core.die(Exit.REFUSED, f"--{flag} is empty", APPEND)
    cost = a.cost_usd.strip()
    if cost:
        try:
            ok = math.isfinite(float(cost))
        except ValueError:
            ok = False
        if not ok:
            core.die(Exit.REFUSED, f"--cost-usd {a.cost_usd!r} is not a finite number", APPEND)
    try:
        fd = os.open(a.file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as e:
        core.die(Exit.REFUSED, f"cannot open {a.file}: {e.strerror}", INIT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        text = read_text(a.file, INIT)
        lead = ""
        if not text:
            os.write(fd, (HEADER + "\n").encode())
            text = HEADER + "\n"
        elif not text.endswith("\n"):
            lead = "\n"  # the last line was cut short: end it in the same write, never glue a row onto it
        rows = rows_of(text, a.file, INIT)
        line = "\t".join(escape(str(v)) for v in (now(), a.phase, a.action, a.result, a.evidence, a.verdict,
                                                  cost, a.note))
        if line in rows:
            row = rows.index(line) + 1
            core.out(a, {"status": "already-done", "row": row, "file": a.file}, f"already done: row {row}")
            return Exit.OK
        os.write(fd, (lead + line + "\n").encode())  # one write on an O_APPEND fd: never interleaves
    finally:
        os.close(fd)  # also drops the flock
    if lead:
        print("repaired: missing newline", file=sys.stderr)
    core.out(a, {"status": "appended", "row": len(rows) + 1, "file": a.file}, f"row: {len(rows) + 1}")
    return Exit.OK


def read_text(path, example):
    """The file's text, or exit 2 when it is not UTF-8."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        core.die(Exit.REFUSED, f"{path} is not UTF-8 text, so not an agent-cli log", example)


def show(a):
    if a.last is not None and a.last < 1:
        core.die(Exit.REFUSED, f"--last {a.last} must be 1 or more", SHOW)
    try:
        text = read_text(a.file, SHOW)
    except FileNotFoundError:
        core.die(Exit.REFUSED, f"no such log: {a.file}", SHOW)
    rows = []
    for n, ln in enumerate(rows_of(text, a.file, SHOW), 1):
        parts = ln.split("\t")
        if len(parts) != len(FIELDS):
            core.die(Exit.FAIL, f"{a.file} row {n} has {len(parts)} fields, not {len(FIELDS)}", SHOW)
        rows.append(dict(zip(FIELDS, map(unescape, parts))))
    rows = [r for r in rows if a.phase is None or r["phase"] == a.phase]
    for i, r in enumerate(rows[-a.last:] if a.last else rows):
        if a.json:
            print(json.dumps(r))
        else:
            print(("\n" if i else "") + "".join(f"{k}: {v}".replace("\n", "\n  ") + "\n" for k, v in r.items() if v),
                  end="")
    return Exit.OK
