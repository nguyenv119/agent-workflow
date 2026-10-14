"""`agent-cli repeats`: command shapes typed by hand in 3+ sessions that no command or hook rule covers yet.

Reads the shape recorder's store (record.py), or with --commands a JSONL of raw {ts, session, cmd} rows
that it shapes and rule-matches in memory; the raw text is never written anywhere.

Covered (--covered live): a shape is covered when (a) a hook rule matched at least half of its runs,
counting a rule a run skipped with `# agent-cli: allow <id>` (a deny rule blocks its command, so the
recorder only sees a denied chore when an agent bypassed it on purpose, and the rule's command already
exists for it). Half, not any: a generic shape (`python3 - <heredoc>`) holds many chores, and one run
that matched a rule must not hide the rest (a 2-day replay had 1 such run in 60). Or (b) its first
command word, after today's command-prefixes seam words are stripped, is agent-cli or a name on the
`commands:` line under `## repeats` in .claude/project-rules.md.
"""
import datetime as dt
import json
import re
import sys

from . import core, hook, record
from .core import Exit
from .shape import prefix_re

EXAMPLES = ["agent-cli repeats --since 14d",
            "agent-cli repeats --from 2026-04-01 --until 2026-04-07T18:00 --json",
            "agent-cli repeats --commands commands.jsonl --covered none --top 25",
            "agent-cli repeats --since 14d --write-cache"]
CACHE = "repeats-cache.json"
FIELDS = ("sessions", "count", "exit_fail_share", "first", "last", "shape")
TS = "%Y-%m-%dT%H:%M:%SZ"
STORE_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
DESCRIPTION = """\
Lists command shapes run in --min-sessions or more distinct sessions inside the window, minus covered
shapes, ranked by sessions, then runs, then shape. A shape is a command with every value masked
(`gh pr <arg> <n> --json <v>`); see `agent-cli hook posttooluse`.

Source: the recorder's store (${AGENT_CLI_SHAPES_DIR:-~/.claude/agent-cli/shapes}), or with --commands a
JSONL file of {"ts", "session", "cmd"} rows (optional "repo", "exit") shaped in memory; nothing from it
is written. Window: --since back from now (default 14d over the store; over --commands every row counts
unless a window is given), or --from/--until in local time, YYYY-MM-DD or YYYY-MM-DDTHH:MM. --from is
inclusive; --until with a time is exclusive (a run at exactly that minute is out); a bare --until date
includes that whole day.

Covered (--covered live, the default): a hook rule matched at least half of the shape's runs (a run
that skipped the rule with `# agent-cli: allow <id>` counts: the rule's command exists), or its
command word, after the command-prefixes seam words, is agent-cli or a name on the `commands:` line
under `## repeats` in .claude/project-rules.md. --covered none lists them too (a window from before
the commands existed).

Per shape: sessions, count (runs), exit_fail_share (failed runs / runs with a known exit; null when
none is known), first and last (UTC). --write-cache also writes <store>/repeats-cache.json
{shape: {sessions, count}} with every listed shape, --top ignored (the warning hook reads it; so
it takes the store with the live filter, never --commands, --covered none or --repo).

Exit codes (agent-cli's shared table): 0 ok: the report ran, an empty list included; 2 refused: bad
usage; 3 blocked: the store or the --commands file cannot be read, a --commands line is not a
{ts, session, cmd} object, or the cache cannot be written."""


def register(sub):
    p = core.subcommand(sub, "repeats", "command shapes typed in 3+ sessions that no command or hook rule "
                        "covers yet (read-only unless --write-cache)", EXAMPLES)
    p.description = DESCRIPTION
    p.add_argument("--since", help="window back from now, e.g. 14d (not with --from/--until)")
    p.add_argument("--from", dest="from_", metavar="WHEN", help="local YYYY-MM-DD[THH:MM], inclusive")
    p.add_argument("--until", metavar="WHEN", help="local YYYY-MM-DD (whole day) or YYYY-MM-DDTHH:MM (exclusive)")
    p.add_argument("--min-sessions", type=int, default=3, help="distinct sessions a shape needs (default 3)")
    p.add_argument("--top", type=int, default=25, help="print the first N shapes (default 25)")
    p.add_argument("--repo", help="only runs from this repo (the row's repo name)")
    p.add_argument("--covered", choices=("live", "none"), default="live",
                   help="live: drop shapes a rule or command covers (default); none: keep them")
    p.add_argument("--commands", metavar="FILE", help="JSONL of raw {ts, session, cmd} rows instead of the store")
    p.add_argument("--write-cache", action="store_true", help="also write <store>/repeats-cache.json")
    p.add_argument("--json", action="store_true", help="one JSON object per shape")
    p.set_defaults(func=run)


def run(a):
    if a.min_sessions < 1 or a.top < 1:
        core.die(Exit.REFUSED, "--min-sessions and --top must be 1 or more", EXAMPLES[0])
    if a.write_cache and (a.commands or a.covered == "none" or a.repo is not None):
        # the warning hook reads the cache as "the store's uncovered repeats"; any other set would mislead it
        core.die(Exit.REFUSED, "--write-cache takes the store with the live filter: not with --commands, "
                 "--covered none or --repo", EXAMPLES[3])
    lo, hi = window(a)
    live = a.covered == "live"
    rows, skipped = from_commands(a.commands, lo, hi, live) if a.commands else from_store(lo, hi)
    if a.repo is not None:
        rows = [r for r in rows if r.get("repo") == a.repo]
    groups = group(rows)
    words = set(core.names(core.rules_line("repeats", "commands") or "")) | {"agent-cli"}
    lead = prefix_re(tuple(core.seam_lines("command-prefixes")))
    repeated = [(s, g) for s, g in groups.items() if len(g["sessions"]) >= a.min_sessions]
    listed = [(s, g) for s, g in repeated
              if not (live and (2 * g["ruled"] >= g["count"] or command_word(s, lead) in words))]
    listed.sort(key=lambda sg: (-len(sg[1]["sessions"]), -sg[1]["count"], sg[0]))
    if a.write_cache:
        cache = {s: {"sessions": len(g["sessions"]), "count": g["count"]} for s, g in listed}
        try:
            core.write_atomic(record.store_dir() / CACHE, json.dumps(cache, sort_keys=True) + "\n")
        except OSError as e:
            core.die(Exit.BLOCKED, f"cannot write {record.store_dir() / CACHE}: {e.strerror}", EXAMPLES[3])
    out = [report_row(s, g) for s, g in listed[:a.top]]
    if not a.json:
        print("  ".join(FIELDS))
    for row in out:
        share = "-" if row["exit_fail_share"] is None else f"{row['exit_fail_share']:.2f}"
        core.out(a, row, f"{row['sessions']}  {row['count']}  {share}  {row['first']}  {row['last']}  {row['shape']}")
    sys.stderr.write(f"repeats: {len(rows)} runs in window, {len(repeated)} shapes in {a.min_sessions}+ sessions, "
                     f"{len(repeated) - len(listed)} covered, {len(out)} printed"
                     + (f", {skipped} unreadable store lines skipped" if skipped else "") + "\n")
    return Exit.OK


def window(a):
    """(lo, hi) as UTC `TS` strings, lo <= ts < hi ("" and "~" for an open end): core.window, whose default
    is 14d over the store and no window over --commands (replay files hold old commands)."""
    lo, hi = core.window(a, None if a.commands else "14d", EXAMPLES[1])
    return (dt.datetime.fromtimestamp(lo, dt.timezone.utc).strftime(TS) if lo != float("-inf") else "",
            dt.datetime.fromtimestamp(hi, dt.timezone.utc).strftime(TS) if hi != float("inf") else "~")


def norm_ts(v):
    """An ISO-8601 time (Z, an offset, or none = UTC) as a TS string; raises ValueError or TypeError."""
    if STORE_TS.fullmatch(v):  # the recorder's own format: no parse needed
        return v
    t = dt.datetime.fromisoformat(v[:-1] + "+00:00" if v.endswith("Z") else v)
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(dt.timezone.utc).strftime(TS)


def from_store(lo, hi):
    """(rows in the window, malformed lines skipped) from the day files that can hold the window."""
    d = record.store_dir()
    if not d.is_dir():
        core.die(Exit.BLOCKED, f"no shape store at {d}: the recorder hook (agent-cli hook posttooluse) has not "
                 "run here, or AGENT_CLI_SHAPES_DIR points elsewhere", "agent-cli repeats --commands <file.jsonl>")
    rows, skipped = [], 0
    try:
        files = sorted(d.iterdir())
    except OSError as e:
        core.die(Exit.BLOCKED, f"cannot list {d}: {e.strerror}", EXAMPLES[0])
    for f in files:
        m = record.DAY_FILE.fullmatch(f.name)
        if not m or not lo[:10] <= m[1] <= hi[:10]:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            core.die(Exit.BLOCKED, f"cannot read {f}: {e.strerror}", EXAMPLES[0])
        for ln in text.splitlines():
            try:
                row = json.loads(ln)
                row["ts"] = norm_ts(row["ts"])
            except (ValueError, KeyError, TypeError, AttributeError):  # e.g. a line cut short by a crash
                skipped += 1
                continue
            if lo <= row["ts"] < hi:
                rows.append(row)
    return rows, skipped


def from_commands(path, lo, hi, live):
    """(rows, 0) from a raw-command JSONL: each shaped, and rule-matched when live, in memory only."""
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError as e:
        core.die(Exit.BLOCKED, f"cannot read --commands {path}: {e.strerror}", EXAMPLES[2])
    prefixes, tools = core.seam_lines("command-prefixes"), core.seam_lines("subcommand-tools")
    rules = hook.load_rules() if live else []  # no rules: fields() finds no hits, and --covered none needs none
    rows = []
    with f:
        for n, ln in enumerate(f, 1):
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
                cmd, session, ts = obj["cmd"], obj["session"], norm_ts(obj["ts"])
                if not (isinstance(cmd, str) and isinstance(session, str) and session):
                    raise TypeError
            except (ValueError, KeyError, TypeError, AttributeError):  # the line itself is never echoed
                core.die(Exit.BLOCKED, f"line {n} of {path} is not a {{\"ts\", \"session\", \"cmd\"}} JSON object",
                         EXAMPLES[2])
            if not lo <= ts < hi:
                continue
            row = {"ts": ts, "session": session, "repo": obj.get("repo"),
                   **record.fields(cmd, rules, prefixes, tools)}
            if "exit" in obj:
                row["exit"] = obj["exit"]
            rows.append(row)
    return rows, 0


def group(rows):
    """{shape: {sessions, count, first, last, failed, known, ruled}} over rows (ruled: runs a rule matched,
    bypassed or not; a row with no session adds to count but not to sessions)."""
    groups = {}
    for r in rows:
        s = r.get("shape")
        if not s:
            continue
        g = groups.get(s)
        if g is None:
            g = groups[s] = {"sessions": set(), "count": 0, "first": r["ts"], "last": r["ts"], "failed": 0,
                             "known": 0, "ruled": 0}
        if r.get("session"):
            g["sessions"].add(r["session"])
        g["count"] += 1
        g["first"], g["last"] = min(g["first"], r["ts"]), max(g["last"], r["ts"])
        if "exit" in r:
            g["known"] += 1
            g["failed"] += r["exit"] != 0
        g["ruled"] += bool(r.get("rules") or r.get("bypassed"))
    return groups


def command_word(s, lead):
    """The shape's first command name, past seam wrapper words (`wrapx <path>/agent-cli wt` -> agent-cli)."""
    rest = s[lead.match(s + " ").end():]  # cmd_re always matches, at worst the empty string
    return rest.split(" ", 1)[0].rsplit("/", 1)[-1]


def report_row(s, g):
    return {"shape": s, "sessions": len(g["sessions"]), "count": g["count"], "first": g["first"], "last": g["last"],
            "exit_fail_share": round(g["failed"] / g["known"], 4) if g["known"] else None}
