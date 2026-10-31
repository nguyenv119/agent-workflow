"""`agent-cli harness hooks`: every settings.json hook needs a registry entry with a reason, an owner and a review date.

The registry is one JSON file: {"<hook command, exactly as in settings.json>": {"reason": "...", "owner": "...",
"review_by": "YYYY-MM-DD"}}. Each hook found in the --settings files is listed with its entry and sorted: ok,
missing (no entry), past (review_by before today; the day itself still counts), invalid (blank reason or owner, or
review_by not YYYY-MM-DD). Registry keys that match no hook are printed as `unused` and never fail. A command wired
to two events is two rows and one key. Plugin hooks are not read. Stdlib only.
"""
import datetime
import json
import re
from pathlib import Path

from . import core
from .core import Exit

EXAMPLE = "agent-cli harness hooks --registry hook-reasons.json"
STATUSES = ("ok", "missing", "past", "invalid")
SHOWN = 90


def register(hs):
    p = core.subcommand(hs, "hooks", "list every settings.json hook with its reason and review date from a registry "
                        "(exit 1 on a missing entry, a past review_by or an invalid entry)",
                        [EXAMPLE, f"{EXAMPLE} --settings ~/.claude/settings.json --settings .claude/settings.json "
                         "--json"])
    p.add_argument("--registry", required=True, metavar="FILE", help="JSON file keyed by hook command")
    p.add_argument("--settings", action="append", metavar="FILE",
                   help="a settings.json to read, repeatable (default: ~/.claude/settings.json and "
                        "./.claude/settings.json, whichever exist)")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=hooks_cmd)


def load_json(path, what):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        core.die(Exit.REFUSED, f"{what} {path}: {e}", EXAMPLE)


def settings_hooks(path):
    """[(settings path, event, matcher, command)] for every command hook in a settings.json."""
    data = load_json(path, "--settings")
    try:
        return [(str(path), event, g.get("matcher"), h["command"])
                for event, gs in (data.get("hooks") or {}).items() for g in gs
                for h in g.get("hooks", []) if "command" in h]
    except (AttributeError, TypeError) as e:
        core.die(Exit.REFUSED, f"--settings {path} has a hooks block of an unexpected shape: {e}", EXAMPLE)


def problem(e, today):
    """None when e is a complete entry whose review_by has not passed, else `past` or `invalid`."""
    if not (isinstance(e, dict) and all(isinstance(e.get(k), str) and e[k].strip() for k in ("reason", "owner"))):
        return "invalid"
    try:
        date = datetime.date.fromisoformat(e.get("review_by"))
    except (TypeError, ValueError):
        return "invalid"
    return "past" if date < today else None


def check(hooks, registry, today):
    """One row per (settings, event, matcher, command) in hooks, with its registry entry and status."""
    rows = []
    for settings, event, matcher, command in hooks:
        e = registry.get(command)
        status = "missing" if e is None else problem(e, today) or "ok"
        e = e if isinstance(e, dict) else {}
        rows.append({"settings": settings, "event": event, "matcher": matcher, "command": command, "status": status,
                     "reason": e.get("reason"), "owner": e.get("owner"), "review_by": e.get("review_by")})
    return rows


def one_line(text):
    return re.sub(r"\s+", " ", text)


def default_settings():
    found = {p.resolve(): p for p in (Path.home() / ".claude/settings.json", Path(".claude/settings.json"))
             if p.is_file()}
    return list(found.values())


def hooks_cmd(a):
    registry = load_json(a.registry, "--registry")
    if not isinstance(registry, dict):
        core.die(Exit.REFUSED, f"--registry {a.registry} must be a JSON object keyed by hook command", EXAMPLE)
    files = a.settings or default_settings()
    if not files:
        core.die(Exit.REFUSED, "no settings.json found in ~/.claude or ./.claude", f"{EXAMPLE} --settings <file>")
    rows = check([h for f in files for h in settings_hooks(f)], registry, datetime.date.today())
    unused = sorted(set(registry) - {r["command"] for r in rows})
    counts = {s: sum(r["status"] == s for r in rows) for s in STATUSES}
    bad = any(counts[s] for s in STATUSES[1:])
    if a.json:
        print(json.dumps({"hooks": rows, "unused": unused, "counts": {"listed": len(rows), **counts}}))
        return Exit.FAIL if bad else Exit.OK
    for r in rows:
        cmd = one_line(r["command"])
        cmd = cmd if len(cmd) <= SHOWN else cmd[:SHOWN - 3] + "..."
        print(f"{r['status']} {r['review_by'] or '-'} {r['owner'] or '-'} {r['event']} {r['matcher'] or '-'} {cmd}")
        if r["status"] == "missing":
            stub = {r["command"]: {"reason": "", "owner": "", "review_by": ""}}
            print(f"  fix: add to the registry: {json.dumps(stub)}")
        elif r["reason"]:
            print(f"  reason: {r['reason']}")
    for c in unused:
        print(f"unused: {one_line(c)}")
    print(f"hooks: {len(rows)} listed, {counts['ok']} ok, {counts['missing']} missing, {counts['past']} past review_by, "
          f"{counts['invalid']} invalid, {len(unused)} unused")
    return Exit.FAIL if bad else Exit.OK
