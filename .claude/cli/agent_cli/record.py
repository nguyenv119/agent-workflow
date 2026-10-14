"""The shape recorder: one JSON line per Bash call, holding the command's shape and never the command.

Row: {ts, session, agent, repo, shape, exit, rules, bypassed} appended to
${AGENT_CLI_SHAPES_DIR:-~/.claude/agent-cli/shapes}/<UTC YYYY-MM-DD>.jsonl with one os.write on an
O_APPEND fd (atomic for a line this short). `rules` = ids of the active hook rules that match the raw
command, computed in memory, so a report can tell covered shapes from uncovered ones without the raw
text; `bypassed` = the ids of rules that match but were skipped by `# agent-cli: allow <id>` (a deny
rule's chore is only ever recorded when bypassed). Day files older than AGENT_CLI_SHAPES_KEEP_DAYS
(default 45) are deleted, at most once a day.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from . import core, hook
from .shape import shape

MAX_SHAPE = 1024
KEEP_DAYS = 45
DAY_FILE = re.compile(r"(\d{4}-\d\d-\d\d)\.jsonl")
EXIT_CODE = re.compile(r"Exit code (\d+)")
MARKER = ".last-sweep"


def store_dir():
    return Path(os.environ.get("AGENT_CLI_SHAPES_DIR") or Path.home() / ".claude/agent-cli/shapes")


def record(event, now=None):
    """Append event's row (event: the hook's JSON text or the parsed dict) and return it; None when the
    event is not a Bash call. Raises on a bad event or an unwritable store (the hook verb swallows it)."""
    data = json.loads(event) if isinstance(event, str) else event
    if data.get("tool_name") != "Bash":
        return None
    command = data["tool_input"]["command"]
    now = now or dt.datetime.now(dt.timezone.utc)
    s = shape(command)
    hits = [r["id"] for r in hook.evaluate(command, hook.load_rules(), bypass=False)]
    allowed = set(hook.BYPASS.findall(command))
    exit_code = 0
    if data.get("hook_event_name") == "PostToolUseFailure":
        m = EXIT_CODE.search(str(data.get("error") or ""))
        exit_code = int(m[1]) if m else None
    row = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "session": _short(data.get("session_id")),
           "agent": _short(data.get("agent_id")), "repo": _repo(data.get("cwd")),
           "shape": s[:MAX_SHAPE] if s else None, "exit": exit_code,
           "rules": [i for i in hits if i not in allowed], "bypassed": [i for i in hits if i in allowed]}
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(d / f"{now:%Y-%m-%d}.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(row) + "\n").encode())
    finally:
        os.close(fd)
    _sweep(d, now)
    return row


def _short(v):
    return str(v)[:128] if v else None


def _repo(cwd):
    """The main repo's dir name for cwd (a linked worktree maps to its main repo), else cwd's name."""
    if not cwd:
        return None
    common = core.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd, check=False, timeout=5)
    if common and Path(common).name == ".git":
        return Path(common).parent.name[:128]
    top = core.git("rev-parse", "--show-toplevel", cwd=cwd, check=False, timeout=5)
    return Path(top or cwd).name[:128] or None


def _sweep(d, now):
    """Delete day files older than the keep window, once per UTC day (the marker holds the last day)."""
    today = f"{now:%Y-%m-%d}"
    marker = d / MARKER
    try:
        if marker.read_text().strip() == today:
            return
    except OSError:
        pass
    marker.write_text(today + "\n")
    try:
        keep = int(os.environ.get("AGENT_CLI_SHAPES_KEEP_DAYS") or KEEP_DAYS)
    except ValueError:
        keep = KEEP_DAYS
    cutoff = f"{now - dt.timedelta(days=max(keep, 1)):%Y-%m-%d}"
    for f in d.iterdir():
        m = DAY_FILE.fullmatch(f.name)
        if m and m[1] < cutoff:
            try:
                f.unlink()
            except FileNotFoundError:  # another session's sweep got it first
                pass


def hook_verb(event_name):
    """Read a PostToolUse/PostToolUseFailure event on stdin and record it. Prints nothing, never raises:
    a recorder must never fail a tool call."""
    try:
        data = json.loads(sys.stdin.read())
        data.setdefault("hook_event_name", event_name)
        record(data)
    except Exception:
        return
