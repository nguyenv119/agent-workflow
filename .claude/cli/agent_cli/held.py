"""`agent-cli harness held`: did the mistake a harness fix targets come back after the fix merged?

Reads a ledger, one JSON object per line, one per fix: {"bead", "merged_date" (a local date, YYYY-MM-DD), "pattern"
(a regex), "where" (hook | assistant | tool)}. Scans the transcripts (--projects DIR, default ~/.claude/projects):
the session files DIR/*/*.jsonl and every subagent file under DIR/*/*/subagents/ (workflow subagents sit one level
deeper), since most of a session's tool calls live in the subagent files. Per fix it counts the hits of its pattern in
the matching kind of message during the --days (default 14) after merged_date. A hit is
  assistant  one text block of an assistant reply;
  tool       one tool call: for Bash only input.command, for any other tool its name plus every string in its input;
  hook       one hook that caught the mistake: a hook_blocking_error or hook_non_blocking_error attachment (every
             string in it except its `command`), or a PreToolUse block (a user record's tool_result whose text starts
             `PreToolUse:`, or is_error with "hook error" in it). A hook that merely ran (hook_success) is not a hit.
Output per fix: `held N days`, `came back K times` or `no transcripts in window`. Exit 1 when any fix came back, else
5 (partial) when a closed window had no transcript file to read (a mistyped --projects or a pruned store must not
read as "held"), else 0.

merged_date is a local date and the window opens at the next local midnight after it (TZ decides), not at merged_date
itself: the fix landed at an unknown hour that day, so that day's earlier hits are not charged to it; it closes --days
local days later. A window that has not closed yet reports `held N days so far (window open)` with N the full days
elapsed. A file whose modified time is before the window opens is not read. A record seen under the same uuid in two
files (a resumed session copies its history) counts once. A line that is not JSON (a live session's cut-off tail) is
skipped and counted in `bad_lines`. A pattern must not match its own ledger line: a session that reads or edits the
ledger puts the pattern in a tool call and would charge the fix for it (a `where: tool` pattern most of all). Stdlib
only.

Reader reuse: repeats.py reads the shape recorder's store and startsize.py's reader stops at the first reply and
refuses a bad line; neither scans whole transcripts, so this module has its own small reader on core's helpers.
"""
import datetime as dt
import json
import os
import re
import time
from pathlib import Path

from . import core
from .core import Exit

EXAMPLE = "agent-cli harness held --ledger fix-ledger.jsonl --days 14"
KINDS = ("hook", "assistant", "tool")
CAUGHT = ("hook_blocking_error", "hook_non_blocking_error")
KEYS = ("bead", "merged_date", "pattern", "where")
DAY = 86400


def register(hs):
    p = core.subcommand(hs, "held", "per fix in a ledger: did its mistake come back in the N days after the merge, "
                        "counted in saved transcripts, subagents included (exit 1 when any came back, "
                        "5 when a closed window had no transcript to read)",
                        [EXAMPLE, "agent-cli harness held --ledger fix-ledger.jsonl --json",
                         "agent-cli harness held --ledger fix-ledger.jsonl --days 7 --projects ~/.claude/projects"])
    p.add_argument("--ledger", required=True, metavar="FILE", help="JSONL, one fix per line: bead, merged_date "
                   "(a LOCAL date, YYYY-MM-DD; the window opens at the next local midnight), pattern (regex; must not "
                   "match its own ledger line), where (hook|assistant|tool)")
    p.add_argument("--days", type=int, default=14, help="window length in days after merged_date (default 14)")
    p.add_argument("--projects", default="~/.claude/projects", metavar="DIR",
                   help="transcript root: DIR/*/*.jsonl and DIR/*/*/subagents/**/*.jsonl "
                   "(default ~/.claude/projects)")
    p.add_argument("--json", action="store_true", help="one JSON object per fix")
    p.set_defaults(func=held_cmd)


def load_ledger(path):
    """[fix dict] with `re` compiled, `lo`/`hi` epoch window bounds and zeroed counters; exit 2 naming the line."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        core.die(Exit.REFUSED, f"--ledger {path}: {e}", EXAMPLE)
    fixes = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue

        def bad(why):
            core.die(Exit.REFUSED, f"ledger {path} line {n}: {why}", EXAMPLE)
        try:
            row = json.loads(line)
        except ValueError as e:
            bad(f"not JSON: {e}")
        missing = [k for k in KEYS if not isinstance(row, dict) or not isinstance(row.get(k), str)]
        if missing:
            bad(f"want string keys {', '.join(KEYS)}; missing or not a string: {', '.join(missing)}")
        if row["where"] not in KINDS:
            bad(f"where {row['where']!r} is not one of {', '.join(KINDS)}")
        if any(f["bead"] == row["bead"] for f in fixes):
            bad(f"bead {row['bead']!r} is already in the ledger")
        try:
            day = dt.date.fromisoformat(row["merged_date"]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["merged_date"]) \
                else None
        except ValueError:
            day = None
        if day is None:
            bad(f"merged_date {row['merged_date']!r} is not YYYY-MM-DD")
        try:
            rx = re.compile(row["pattern"])
        except re.error as e:
            bad(f"pattern is not a regex: {e}")
        fixes.append({**row, "re": rx, "lo": 0.0, "hi": 0.0, "day": day})
    if not fixes:
        core.die(Exit.REFUSED, f"ledger {path} has no fixes", EXAMPLE)
    return fixes


def set_window(fix, days):
    """lo = next local midnight after merged_date, hi = lo + days local days (epoch seconds)."""
    fix["lo"] = core.local_time(str(fix["day"]), "merged_date", EXAMPLE, end=True)
    fix["hi"] = core.local_time(str(fix["day"] + dt.timedelta(days=days)), "merged_date", EXAMPLE, end=True)
    fix.update(hits=0, files=0, bad_lines=0, first=None, last=None)


def leaves(v, skip=()):
    """Every string in a JSON value, joined by newlines (dict keys left out, and the values under keys in skip)."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        v = [x for k, x in v.items() if k not in skip]
    return "\n".join(x for x in (leaves(y, skip) for y in v) if x) if isinstance(v, list) else ""


def result_text(c):
    """The text of a tool_result's content: a string as is, a list's text blocks joined."""
    if isinstance(c, list):
        c = "\n".join(b["text"] for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    return c if isinstance(c, str) else ""


def parts(rec):
    """[(kind, text)] a transcript record offers: assistant text blocks, tool calls, hook attachments."""
    if rec.get("type") == "assistant":
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return [("assistant", content)]
        out = []
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                out.append(("assistant", c["text"]))
            elif isinstance(c, dict) and c.get("type") == "tool_use":
                inp = c.get("input")
                if c.get("name") == "Bash":  # only the command: a heredoc's or description's text is not a call
                    cmd = inp.get("command") if isinstance(inp, dict) else None
                    out.append(("tool", cmd if isinstance(cmd, str) else ""))
                else:
                    out.append(("tool", f"{c.get('name', '')}\n{leaves(inp)}"))
        return out
    att = rec.get("attachment")
    if rec.get("type") == "attachment" and isinstance(att, dict) and att.get("type") in CAUGHT:
        return [("hook", leaves(att, skip=("command",)))]
    msg = rec.get("message") if rec.get("type") == "user" else None
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            t = result_text(c.get("content"))
            if t.startswith("PreToolUse:") or (c.get("is_error") is True and "hook error" in t):
                out.append(("hook", t))
    return out


def epoch(ts):
    """Epoch seconds of an ISO timestamp (Z = UTC), or None when it is not one."""
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, OverflowError, OSError):
        return None


def scan_file(f, active, seen):
    """Count one transcript's hits into the fixes in `active`; `seen` holds the uuids already counted."""
    lo, hi = min(x["lo"] for x in active), max(x["hi"] for x in active)
    try:
        fh = f.open(encoding="utf-8", errors="replace", newline="\n")
    except OSError:
        return
    for fix in active:
        fix["files"] += 1
    with fh:
        for line in fh:
            if not line.rstrip().endswith("}"):
                for fix in active:  # a cut-off tail: counted without parsing
                    fix["bad_lines"] += 1
                continue
            if '"timestamp"' not in line:
                continue  # api-request blobs and bookkeeping rows carry none, and they are the biggest rows
            try:
                rec = json.loads(line)
            except ValueError:
                for fix in active:
                    fix["bad_lines"] += 1
                continue
            t = epoch(rec.get("timestamp")) if isinstance(rec, dict) else None
            if t is None or not lo <= t < hi:
                continue
            uuid = rec.get("uuid")
            if isinstance(uuid, str):
                if uuid in seen:
                    continue
                seen.add(uuid)
            for kind, text in parts(rec):
                for fix in active:
                    if fix["where"] == kind and fix["lo"] <= t < fix["hi"] and fix["re"].search(text):
                        fix["hits"] += 1
                        stamp = rec["timestamp"]
                        fix["first"] = stamp if fix["first"] is None or stamp < fix["first"] else fix["first"]
                        fix["last"] = stamp if fix["last"] is None or stamp > fix["last"] else fix["last"]


def row_of(fix, days, now):
    """The result dict for one fix: status held, came_back or no_transcripts, plus the counters."""
    closed = fix["hi"] <= now
    held = days if closed else max(0, min(days, int((now - fix["lo"]) // DAY)))
    status = "came_back" if fix["hits"] else "held" if fix["files"] or not closed else "no_transcripts"
    return {"bead": fix["bead"], "merged_date": fix["merged_date"], "where": fix["where"], "days": days,
            "status": status, "hits": fix["hits"], "held_days": held, "window_open": not closed,
            "first": fix["first"], "last": fix["last"], "files": fix["files"], "bad_lines": fix["bad_lines"]}


def text_of(r):
    if r["hits"]:
        return (f"{r['bead']}: came back {r['hits']} time{'' if r['hits'] == 1 else 's'} "
                f"(first {r['first']}, last {r['last']})")
    if r["status"] == "no_transcripts":
        return f"{r['bead']}: no transcripts in window"
    return f"{r['bead']}: held {r['held_days']} days" + (" so far (window open)" if r["window_open"] else "")


def held_cmd(a):
    if a.days < 1:
        core.die(Exit.REFUSED, f"--days {a.days} must be 1 or more", EXAMPLE)
    root = Path(os.path.expanduser(a.projects))
    if not root.is_dir():
        core.die(Exit.REFUSED, f"--projects {a.projects} is not a directory", EXAMPLE)
    fixes = load_ledger(a.ledger)
    for fix in fixes:
        set_window(fix, a.days)
    seen = set()
    for f in sorted([*root.glob("*/*.jsonl"), *root.glob("*/*/subagents/**/*.jsonl")]):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        active = [x for x in fixes if mtime >= x["lo"]]
        if active:
            scan_file(f, active, seen)
    now = time.time()
    rows = [row_of(x, a.days, now) for x in fixes]
    for r in rows:
        core.out(a, r, text_of(r))
    if not a.json:
        print(f"scanned {max(r['files'] for r in rows)} transcript files, skipped "
              f"{max(r['bad_lines'] for r in rows)} unreadable lines")
    if any(r["hits"] for r in rows):
        return Exit.FAIL
    return Exit.PARTIAL if any(r["status"] == "no_transcripts" for r in rows) else Exit.OK
