"""`agent-cli repeats`: steps typed by hand in 3+ sessions that no command or hook rule covers yet.

Reads the shape recorder's store (record.py), or with --commands a JSONL of raw {ts, session, cmd} rows
(optional script: the body of the first script file the recorder would read for the command) that
it shapes and rule-matches in memory; the raw text is never written anywhere.

Unit: a step (shape.steps, one pipeline of a chain, loop body or $( ) body) by default, so `. .env` in
40 different chains is one row; store rows from before rows carried `steps` are split from their shape.
Those old rows keep the masking they were recorded with, so while old rows are in the window (14 days
by default) one chore can sit under an old and a new step key; accepted, with no shape-version field.
Shell basics (hook.basic) are dropped, except sleeps and on-main git reads (hook.report_only).
The step report lists groups, not steps: every uncovered step (whatever its session count) joins the group
of its word_pair key (`gh pr view`, `git worktree`, `python3 loop.py`; from the first `|` segment that is not
a shell basic, so a feeder `cat <arg> |` never joins two tools; words by their last path part), and a group is
listed when its merged distinct sessions reach --min-sessions, so spellings each under the bar add up; a run
adds once per group. --json keeps each group's top --spellings spellings (default 10) plus spellings_total:
a real 14-day store had a group of 735 spellings.
The cache (cache_of) stays keyed by step. --unit command ranks whole command shapes, as before steps.

Covered (--covered live): a step or shape is covered when (a) a hook rule matched at least half of its runs,
counting a rule a run skipped with `# agent-cli: allow <id>` (a deny rule blocks its command, so the
recorder only sees a denied chore when an agent bypassed it on purpose, and the rule's command already
exists for it). Half, not any: a generic shape (`python3 - <heredoc>`) holds many chores, and one run
that matched a rule must not hide the rest (a 2-day replay had 1 such run in 60). Or (b) its first
command word, after today's command-prefixes seam words are stripped, is agent-cli or a name on the
`commands:` line under `## repeats` in .claude/project-rules.md. Rule coverage is per run, not per
step: a rule that matches any part of a chain counts for every step of that run (a known limit; a later
change records which step a rule hit).

--by effect ranks effect signals instead (record.py's `effects`: hosts, env var names, .env files, db:postgres), so one
chore's many spellings join on what they touch. Rows from before effects add nothing. Weekly count only:
never --write-cache, so never the live warning.

--polls finds a check repeated over separate calls by one agent: per (session, agent), rows in time order, a
sequence is a maximal run of 3+ rows carrying the same poll key (poll_key: a listed step cut to its command and
masked target, so `gh pr view <n> --json <v>` and `gh pr view <n> -q <v>` are one check), other rows in between allowed,
whose gaps are all within [15 s, 30 min]. Per agent, because parallel subagents share their parent's session id
and would chain one busy step into fake sequences. Rows with no session are skipped. Never --write-cache, so
never the live warning.
"""
import collections
import datetime as dt
import json
import re
import statistics
import sys

from . import core, hook, record, shape
from .core import Exit
from .shape import prefix_re

EXAMPLES = ["agent-cli repeats --since 14d",
            "agent-cli repeats --from 2026-04-01 --until 2026-04-07T18:00 --json",
            "agent-cli repeats --commands commands.jsonl --covered none --top 25",
            "agent-cli repeats --since 14d --write-cache",
            "agent-cli repeats --since 7d --unit command --covered none",
            "agent-cli repeats --since 7d --by effect --covered none --top 10",
            "agent-cli repeats --since 7d --polls --covered none --top 10"]
CACHE = "repeats-cache.json"
FIELDS = ("sessions", "count", "exit_fail_share", "first", "last", "shape")
EFFECT_FIELDS = ("sessions", "count", "first", "last", "effect", "steps")
POLL_FIELDS = ("sessions", "sequences", "median_gap_s", "first", "last", "step", "steps")
POLL_GAP = (15, 30 * 60)  # seconds between two runs of a sequence, both ends inclusive
TS = "%Y-%m-%dT%H:%M:%SZ"
STORE_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
DESCRIPTION = """\
Lists groups of uncovered steps run in --min-sessions or more distinct sessions inside the window (see
below). A step is one pipeline of a command (each part of a `&&` or
`;` chain, a loop body, a $( ) body) with every value masked (`gh pr view <n> --json <v>`); see
`agent-cli hook posttooluse`. A run adds once to each distinct step it holds. Shell basics and generic
reads (cat, export, set -a, mkdir, git add, `[ -n ... ]`, cut -c, inline python) are never listed,
except waits (`sleep <n>`) and git log/show of origin/<arg>. Steps are listed in groups: every uncovered
step joins the group of its tool and subcommand, from the command word of its first `|` segment that is not a
shell basic, else its first (`git worktree list | head <n>` and `git worktree remove <arg> --force` are `git
worktree`; `cat <arg> | jq <str>` is `jq`; gh and npm/yarn run keep one word more: `gh pr view`, `gh pr
checks`; an interpreter keeps its script: `python3 loop.py`; a flag or masked value ends the key: `pkill -f <v>`
is `pkill`; each word by its last path part: `<path>/gh pr view` is `gh pr view`, `. <path>/.env` is `. .env`).
A group is listed when its merged distinct sessions reach --min-sessions, even if no one spelling does; a run
adds once to each group it holds. Groups rank by merged sessions, then runs, then key; --top counts groups.
Each row's fields are the group's, plus `group` (the key), `shape` (its top spelling) and, with --json,
`spellings`: the group's top --spellings N (default 10) uncovered spellings with their own fields, ranked by
sessions, runs, then text, and `spellings_total`: how many uncovered spellings the group has. In text mode a
group of 2+ spellings shows its key in the shape column, then its top 3 spellings indented 4 spaces. --unit command ranks whole command shapes instead, ungrouped, basics included; the `shape`
field holds the command shape there.

Source: the recorder's store (${AGENT_CLI_SHAPES_DIR:-~/.claude/agent-cli/shapes}), or with --commands a
JSONL file of {"ts", "session", "cmd"} rows (optional "repo", "exit", "agent": the subagent id, missing or
empty for the main agent, and "script": the body of the
first script file the command runs, as it stood then, read for its steps and effects as the recorder
reads the file; env files and files a shell sources without a .sh/.bash/.zsh name are skipped, so
`. .env; python3 q.py` takes q.py's body, and a command that runs no file reads none)
shaped in memory; nothing from it is written. Window: --since back from now (default 14d over the store;
over --commands every row counts unless a window is given), or --from/--until in local time, YYYY-MM-DD
or YYYY-MM-DDTHH:MM. --from is inclusive; --until with a time is exclusive (a run at exactly that minute
is out); a bare --until date includes that whole day.

Covered (--covered live, the default): a hook rule matched at least half of the step's runs (a run
that skipped the rule with `# agent-cli: allow <id>` counts: the rule's command exists), or its
command word, after the command-prefixes seam words, is agent-cli or a name on the `commands:` line
under `## repeats` in .claude/project-rules.md. --covered none lists them too (a window from before
the commands existed).

Per step: sessions, count (runs), exit_fail_share (failed runs / runs with a known exit; null when
none is known), first and last (UTC). --write-cache also writes <store>/repeats-cache.json
{step: {sessions, count}} with every listed step but sleeps and origin reads, a step piped only into quiet
reads counted under its first segment (`x <arg> | head <n>` and `x <arg> | grep <str>` as `x <arg>`; the
report keeps the whole pipeline), --top ignored (the warning
hook reads it; so it takes the store with the live filter and the step unit, never --commands,
--covered none, --repo or --unit command).

--by effect: one row per effect signal the recorder stored (`host:<h>`, `env:<NAME>`, `envfile:<basename>`,
`db:postgres` for any Postgres access; names, never values) in --min-sessions or more sessions: sessions, count, first, last, effect, and steps,
the 3 most common listed steps of the runs that carried it (a run with only basic steps, e.g. inline
`python3 - <heredoc>`, counts its own). Under --covered live an effect is covered when at least half its
runs are: every listed step of the run passes the step test above, or, with no listed step, a rule matched
the run. --min-sessions, --top, --repo, the window and --commands apply; not with --write-cache or --unit
command (effects never feed the warning).

--polls: checks repeated over separate calls minutes apart. The key is a run's check: of each listed step,
the first `|` segment from its command word (past VAR=<v> words, shell keywords and git's global options),
cut after the first later word holding a masked value (`gh pr view <n> --repo <arg> --json <v>` -> `gh pr
view <n>`, `bd show <arg> | head <n>` -> `bd show <arg>`, `FOO=<v> npx tsx <arg> --x` -> `npx tsx <arg>`),
or the whole segment when none does (`tsc --noEmit`). The target stays masked, so checks of different PRs
share one key. A key that is a shell basic (not a sleep or origin read) is dropped, and a run adds once to
each distinct key. Per session and agent (parallel subagents share
their parent's session), in time order, a sequence is a maximal run of 3 or more runs carrying the same key
(other runs in between allowed) whose gaps are all from 15 s to 30 min inclusive; a gap outside that ends the
run. Rows with no session are skipped. One row per key: sessions (with a sequence, each counted once however
many of its agents polled), sequences, median_gap_s (median of the gaps inside its sequences, whole seconds),
first and last (UTC, runs inside a sequence), step (the key), and steps (the 3 most common full steps behind
the key in those runs); ranked by sessions, then sequences, then key. --min-sessions counts sessions with a
sequence. Under --covered live a key is covered when its command word is covered (as above), or a rule matched at
least half of all the window's runs that carry the key. The window, --commands, --repo and --top apply; not
with --by, --unit command or --write-cache.

Exit codes (agent-cli's shared table): 0 ok: the report ran, an empty list included; 2 refused: bad
usage; 3 blocked: the store or the --commands file cannot be read, a --commands line is not a
{ts, session, cmd} object (or its script or agent is not a string), or the cache cannot be
written."""


def register(sub):
    p = core.subcommand(sub, "repeats", "command steps typed in 3+ sessions that no command or hook rule "
                        "covers yet (read-only unless --write-cache)", EXAMPLES)
    p.description = DESCRIPTION
    p.add_argument("--since", help="window back from now, e.g. 14d (not with --from/--until)")
    p.add_argument("--from", dest="from_", metavar="WHEN", help="local YYYY-MM-DD[THH:MM], inclusive")
    p.add_argument("--until", metavar="WHEN", help="local YYYY-MM-DD (whole day) or YYYY-MM-DDTHH:MM (exclusive)")
    p.add_argument("--min-sessions", type=int, default=3, help="distinct sessions a row needs (default 3)")
    p.add_argument("--top", type=int, default=25, help="print the first N rows (default 25)")
    p.add_argument("--spellings", type=int, default=10, help="with --json, each group's top N spellings (default 10); "
                   "spellings_total counts them all")
    p.add_argument("--repo", help="only runs from this repo (the row's repo name)")
    p.add_argument("--covered", choices=("live", "none"), default="live",
                   help="live: drop steps a rule or command covers (default); none: keep them")
    p.add_argument("--unit", choices=("step", "command"), default="step",
                   help="step: each step of a command, basics dropped (default); command: whole command shapes")
    p.add_argument("--by", choices=("effect",), help="effect: rank effect signals (hosts, env var names, .env "
                   "files, db:postgres) instead of steps")
    p.add_argument("--polls", action="store_true", help="checks (a step's command and masked target) re-run 3+ times "
                   "15 s to 30 min apart by one agent, ranked by sessions with such a run")
    p.add_argument("--commands", metavar="FILE",
                   help="JSONL of raw {ts, session, cmd, agent?, script?} rows instead of the store")
    p.add_argument("--write-cache", action="store_true", help="also write <store>/repeats-cache.json")
    p.add_argument("--json", action="store_true", help="one JSON object per row")
    p.set_defaults(func=run)


def run(a):
    if a.min_sessions < 1 or a.top < 1 or a.spellings < 1:
        core.die(Exit.REFUSED, "--min-sessions, --top and --spellings must be 1 or more", EXAMPLES[0])
    if a.write_cache and (a.commands or a.covered == "none" or a.repo is not None or a.unit == "command"):
        # the warning hook reads the cache as "the store's uncovered repeated steps"; any other set misleads it
        core.die(Exit.REFUSED, "--write-cache takes the store with the live filter and the step unit: not with "
                 "--commands, --covered none, --repo or --unit command", EXAMPLES[3])
    if a.by and (a.write_cache or a.unit == "command"):  # effects never feed the warning; no command unit
        core.die(Exit.REFUSED, "--by effect takes neither --write-cache nor --unit command", EXAMPLES[5])
    if a.polls and (a.by or a.unit == "command" or a.write_cache):  # per step, never the warning
        core.die(Exit.REFUSED, "--polls takes neither --by, --unit command nor --write-cache", EXAMPLES[6])
    lo, hi = window(a)
    live = a.covered == "live"
    rows, skipped = from_commands(a.commands, lo, hi, live) if a.commands else from_store(lo, hi)
    if a.repo is not None:
        rows = [r for r in rows if r.get("repo") == a.repo]
    by_command = command_covered()
    if a.by:
        groups = group_effects(rows, step_test(group(rows), by_command) if live else None)
        covered = lambda s, g: 2 * g["covered"] >= g["count"]  # noqa: E731
    else:
        groups = poll_groups(rows) if a.polls else group(rows, a.unit == "step")
        ok = step_test(groups, by_command)
        covered = lambda s, g: ok(s)  # noqa: E731
    repeated = [(s, g) for s, g in groups.items() if len(g["sessions"]) >= a.min_sessions]
    listed = [(s, g) for s, g in repeated if not (live and covered(s, g))]
    covered_n, pair = len(repeated) - len(listed), None
    if not (a.by or a.polls) and a.unit == "step":  # list word-pair groups of every uncovered spelling
        pair = {s: word_pair(s) for s in groups if not (live and ok(s))}
        listed = [(k, g) for k, g in group(rows, key=pair.get).items() if len(g["sessions"]) >= a.min_sessions]
    rank = "sequences" if a.polls else "count"
    listed.sort(key=lambda sg: (-len(sg[1]["sessions"]), -sg[1][rank], sg[0]))
    if a.write_cache:
        cache = cache_of(rows, a.min_sessions, by_command)
        try:
            core.write_atomic(record.store_dir() / CACHE, json.dumps(cache, sort_keys=True) + "\n")
        except OSError as e:
            core.die(Exit.BLOCKED, f"cannot write {record.store_dir() / CACHE}: {e.strerror}", EXAMPLES[3])
    if pair is not None:
        spellings = collections.defaultdict(list)
        for s, k in pair.items():
            spellings[k].append(s)
        cap = a.spellings if a.json else 3  # text shows the top 3 under the group line
        out = [group_row(k, g, [report_row(s, groups[s]) for s in spellings[k]], cap) for k, g in listed[:a.top]]
    else:
        out = [(poll_row if a.polls else effect_row if a.by else report_row)(s, g) for s, g in listed[:a.top]]
    if not a.json:
        print("  ".join(POLL_FIELDS if a.polls else EFFECT_FIELDS if a.by else FIELDS))
    for row in out:
        if a.polls:
            text = (f"{row['sessions']}  {row['sequences']}  {row['median_gap_s']}  {row['first']}  {row['last']}  "
                    f"{row['step']}  {'  //  '.join(row['steps'])}")
        elif a.by:
            text = (f"{row['sessions']}  {row['count']}  {row['first']}  {row['last']}  {row['effect']}  "
                    f"{'  //  '.join(row['steps'])}")
        elif row.get("spellings_total", 0) > 1:
            text = "\n".join([step_line(row, row["group"])] + ["    " + step_line(x) for x in row["spellings"]])
        else:
            text = step_line(row)
        core.out(a, row, text)
    unit = "checks with sequences" if a.polls else "effects" if a.by else "steps" if a.unit == "step" else "shapes"
    sys.stderr.write(f"repeats: {len(rows)} runs in window, {len(repeated)} {unit} in {a.min_sessions}+ sessions, "
                     f"{covered_n} covered, "
                     + (f"{len(listed)} groups in {a.min_sessions}+ sessions, " if pair is not None else "")
                     + f"{len(out)} printed"
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
                if not (row.get("shape") is None or isinstance(row["shape"], str)) or any(
                        k in row and not (isinstance(row[k], list) and all(isinstance(x, str) for x in row[k]))
                        for k in ("steps", "effects")):
                    raise TypeError  # a hand-edited or foreign row: skipped like a cut-short one
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
                cmd, session, ts, script = obj["cmd"], obj["session"], norm_ts(obj["ts"]), obj.get("script")
                if not (isinstance(cmd, str) and isinstance(session, str) and session
                        and isinstance(script, (str, type(None))) and isinstance(obj.get("agent"), (str, type(None)))):
                    raise TypeError
            except (ValueError, KeyError, TypeError, AttributeError):  # the line itself is never echoed
                core.die(Exit.BLOCKED, f"line {n} of {path} is not a {{\"ts\", \"session\", \"cmd\"}} JSON object "
                         "(script and agent, when present, must be strings)",
                         EXAMPLES[2])
            if not lo <= ts < hi:
                continue
            row = {"ts": ts, "session": session, "agent": obj.get("agent") or None, "repo": obj.get("repo"),
                   **record.fields(cmd, rules, prefixes, tools, script_text=script)}
            if "exit" in obj:
                row["exit"] = obj["exit"]
            rows.append(row)
    return rows, 0


def group(rows, by_step=True, key=None):
    """{key: {sessions, count, first, last, failed, known, ruled}} over rows (ruled: runs a rule matched,
    bypassed or not; a row with no session adds to count but not to sessions). key: by_step, each distinct
    step of the row that the report may list (`not basic or report_only`; a row without `steps` is split
    from its shape, once per distinct shape), mapped through key when given (each distinct result once),
    else the row's whole shape."""
    groups, split = {}, {}
    for r in rows:
        s = r.get("shape")
        if not s:
            continue
        keys = listed_steps(r, split) if by_step else (s,)
        for k in dict.fromkeys(map(key, keys)) if key else keys:
            if k is None:  # key maps a step it leaves out (a covered spelling) to None
                continue
            g = groups.get(k)
            if g is None:
                g = groups[k] = {"sessions": set(), "count": 0, "first": r["ts"], "last": r["ts"], "failed": 0,
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


def listed_steps(r, split):
    """A row's distinct steps the step report may list (`not basic or report_only`); a row without `steps`
    is split from its shape (split: a {shape: steps} cache shared across rows)."""
    keys = r.get("steps")
    if not isinstance(keys, list):  # a row recorded before rows carried steps
        s = r.get("shape") or ""
        if s not in split:
            split[s] = shape.steps_of_shape(s)
        keys = split[s]
    return [k for k in dict.fromkeys(keys) if not hook.basic(k) or hook.report_only(k)]


def cache_of(rows, min_sessions, by_command):
    """The warning cache {key: {sessions, count}}: rows' listed steps grouped by hook.chore (`x <arg> | head
    <n>` adds to `x <arg>`, a run once per distinct key), keys in min_sessions+ sessions that step_test does
    not cover and that are not basic (so no sleeps or origin reads)."""
    groups = group(rows, key=hook.chore)
    ok = step_test(groups, by_command)
    return {k: {"sessions": len(g["sessions"]), "count": g["count"]} for k, g in groups.items()
            if len(g["sessions"]) >= min_sessions and not ok(k) and not hook.basic(k)}


def step_test(groups, by_command):
    """The step report's covered test over step groups: a rule matched half the step's runs, or its command
    word is covered."""
    return lambda k: k in groups and (2 * groups[k]["ruled"] >= groups[k]["count"] or by_command(k))


def group_effects(rows, step_ok=None):
    """{effect: {sessions, count, first, last, steps, covered}} over rows that carry effects. steps: a Counter
    of each carrying run's listed steps (all its steps when none is listed); covered: runs whose listed steps
    all pass step_ok, or with none listed, runs a rule matched; step_ok None (--covered none) counts none."""
    groups, split = {}, {}
    for r in rows:
        if not r.get("effects"):
            continue
        listed = listed_steps(r, split)
        ok = step_ok is not None and (all(map(step_ok, listed)) if listed
                                      else bool(r.get("rules") or r.get("bypassed")))
        for e in dict.fromkeys(r["effects"]):
            g = groups.get(e)
            if g is None:
                g = groups[e] = {"sessions": set(), "count": 0, "first": r["ts"], "last": r["ts"], "covered": 0,
                                 "steps": collections.Counter()}
            if r.get("session"):
                g["sessions"].add(r["session"])
            g["count"] += 1
            g["first"], g["last"] = min(g["first"], r["ts"]), max(g["last"], r["ts"])
            g["covered"] += ok
            g["steps"].update(listed or list(dict.fromkeys(r.get("steps") or [])))
    return groups


def poll_key(step):
    """A step's check (see --polls): its first `|` segment from the command word, cut after the first later word
    holding a masked value; "" when the segment has no command word."""
    words = hook._words(hook.SEGMENT_SEP.split(step)[0])
    cut = next((i for i, w in enumerate(words[1:], 2) if hook.PLACEHOLDER.search(w)), len(words))
    return " ".join(words[:cut])


def poll_keys(r, split):
    """{key: [the row's listed steps behind it]} for a row, basic keys (not sleeps or origin reads) dropped."""
    keys = collections.defaultdict(list)
    for step in listed_steps(r, split):
        k = poll_key(step)
        if k and (not hook.basic(k) or hook.report_only(k)):
            keys[k].append(step)
    return keys


def poll_groups(rows):
    """{key: {sessions, sequences, gaps, first, last, steps, count, ruled}}: sequences (see --polls) found per
    (session, agent), rows sorted by ts once, then one pass keeping each key's open run (time, ts, steps);
    count and ruled: every run in rows carrying the key, and those a rule matched (bypassed or not)."""
    by_agent, split, groups, tally = collections.defaultdict(list), {}, {}, collections.defaultdict(lambda: [0, 0])
    for r in rows:
        keys = poll_keys(r, split) if r.get("shape") else {}
        for k in keys:
            tally[k][0] += 1
            tally[k][1] += bool(r.get("rules") or r.get("bypassed"))
        if r.get("session") and keys:
            by_agent[(r["session"], r.get("agent"))].append((r["ts"], keys))

    def close(k, run, session):
        if len(run) < 3:
            return
        g = groups.get(k)
        if g is None:
            g = groups[k] = {"sessions": set(), "sequences": 0, "gaps": [], "first": run[0][1], "last": run[-1][1],
                             "steps": collections.Counter(), "count": tally[k][0], "ruled": tally[k][1]}
        g["sessions"].add(session)
        g["sequences"] += 1
        g["gaps"] += [int((q[0] - p[0]).total_seconds()) for p, q in zip(run, run[1:])]
        g["first"], g["last"] = min(g["first"], run[0][1]), max(g["last"], run[-1][1])
        for _, _, steps in run:
            g["steps"].update(steps)

    for (session, _), rs in by_agent.items():
        rs.sort(key=lambda tk: tk[0])  # TS strings sort in time order
        runs = {}
        for ts, keys in rs:
            t = dt.datetime.strptime(ts, TS)
            for k, steps in keys.items():
                run = runs.get(k)
                if run and POLL_GAP[0] <= (t - run[-1][0]).total_seconds() <= POLL_GAP[1]:
                    run.append((t, ts, steps))
                else:
                    if run:
                        close(k, run, session)
                    runs[k] = [(t, ts, steps)]
        for k, run in runs.items():
            close(k, run, session)
    return groups


def poll_row(s, g):
    return {"step": s, "sessions": len(g["sessions"]), "sequences": g["sequences"],
            "median_gap_s": round(statistics.median(g["gaps"])), "first": g["first"], "last": g["last"],
            "steps": top_steps(g["steps"])}


def top_steps(counter):
    """The 3 most common steps of a Counter, ties by step text."""
    return [k for k, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]


def effect_row(e, g):
    return {"effect": e, "sessions": len(g["sessions"]), "count": g["count"], "first": g["first"],
            "last": g["last"], "steps": top_steps(g["steps"])}


def command_covered():
    """A test: is a shape's command word agent-cli or a name on this project's `commands:` line under
    `## repeats` (read once, here)? The warning hook asks it again at read time: the cache is shared."""
    words = set(core.names(core.rules_line("repeats", "commands") or "")) | {"agent-cli"}
    lead = prefix_re(tuple(core.seam_lines("command-prefixes")))
    return lambda s: command_word(s, lead) in words


def command_word(s, lead):
    """The shape's first command name, past seam wrapper words (`wrapx <path>/agent-cli wt` -> agent-cli)."""
    rest = s[lead.match(s + " ").end():]  # cmd_re always matches, at worst the empty string
    return rest.split(" ", 1)[0].rsplit("/", 1)[-1]


def report_row(s, g):
    return {"shape": s, "sessions": len(g["sessions"]), "count": g["count"], "first": g["first"], "last": g["last"],
            "exit_fail_share": round(g["failed"] / g["known"], 4) if g["known"] else None}


def group_row(k, g, spellings, cap):
    """A word-pair group's row: report_row over the merged group, shape its top spelling, group its key,
    spellings: the top cap spellings (report_row dicts) ranked by their own sessions, runs, then text, and
    spellings_total: how many there are."""
    spellings.sort(key=lambda x: (-x["sessions"], -x["count"], x["shape"]))
    return dict(report_row(spellings[0]["shape"], g), group=k, spellings=spellings[:cap],
                spellings_total=len(spellings))


def step_line(row, label=None):
    """A step report text line: the FIELDS of row, the shape column replaced by label when given."""
    share = "-" if row["exit_fail_share"] is None else f"{row['exit_fail_share']:.2f}"
    return f"{row['sessions']}  {row['count']}  {share}  {row['first']}  {row['last']}  {label or row['shape']}"


NAME = re.compile(r"(?:<path>/)?[\w.:+-]+")  # a literal name word, maybe under a masked path: `<path>/.env`
PAIR_THIRD = {t: g for t, g in shape.THIRD.items() if t != "git"}  # `gh pr view`, `npm run <script>`


def word_pair(step):
    """The steps report's group key of a listed step, from the command word (hook._words) of its first `|`
    segment that is not a shell basic, or its first segment when all are (`cat <arg> | jq <str>` -> `jq`, `git
    show origin/<arg> | head <n>` -> `git show`): the tool plus the next word, or the next two after a PAIR_THIRD
    group word (`gh pr view`), each taken only while it is a NAME not starting with `-` (`pkill -f <v>` ->
    `pkill`, `git worktree list | head <n>` -> `git worktree`). An interpreter keys on its first non-flag word
    when that is a script (`python3 -u loop.py` -> `python3 loop.py`, `python3 -m <v>` -> `python3`). Every
    word is keyed by its last path part (`<path>/gh pr view` -> `gh pr view`, `. <path>/.env` -> `. .env`).
    A step with no literal command word is its own key."""
    segs = hook.SEGMENT_SEP.split(step)
    words = hook._words(next((s for s in segs if not hook.basic(s)), segs[0]))
    tool = words[0].rsplit("/", 1)[-1] if words else ""
    if not tool or hook.PLACEHOLDER.fullmatch(tool):
        return step
    rest = words[1:]
    if shape.INTERPRETER.fullmatch(tool):
        script = next((w for w in rest if not w.startswith("-")), "")
        base = script.rsplit("/", 1)[-1]
        return f"{tool} {base}" if NAME.fullmatch(script) and shape.SCRIPT.fullmatch(base) else tool
    key = [tool]
    for w in rest[:2 if rest[:1] and rest[0] in PAIR_THIRD.get(tool, ()) else 1]:
        if w.startswith("-") or not NAME.fullmatch(w):
            break
        key.append(w.rsplit("/", 1)[-1])
    return " ".join(key)
