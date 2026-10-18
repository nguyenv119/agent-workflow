"""The shape recorder: one JSON line per Bash call, holding the command's shape and never the command.

Row: {ts, session, agent, repo, shape, steps, effects, exit, rules, bypassed} appended to
${AGENT_CLI_SHAPES_DIR:-~/.claude/agent-cli/shapes}/<UTC YYYY-MM-DD>.jsonl with one os.write on an
O_APPEND fd (atomic for a line this short). `steps` = shape.steps() of the command, at most MAX_STEPS
(rows from before it have none; readers split `shape` instead). `effects` = effects.effects() of the
command: names only (`host:<h>`, `env:<NAME>`, `envfile:<basename>`, `db:postgres`), never values (rows from before it
have none, and add nothing to `repeats --by effect`). Scripts the command runs (effects.ran: `python3 q.py`,
`bash x.sh`, `./x.sh`; never an env file (.env names, .envrc), and a file a shell runs or sources only when
named <name>.sh/.bash/.zsh, so `source secrets` reads nothing) are read too, never stored: a shell script's
steps that read as commands (SCRIPT_STEP, then a known command word) follow the command's in `steps` (same
cap), and any script's effects join `effects` (a shell script read as shell, others as code). The
file is read only when the event has a cwd: at most MAX_SCRIPTS per command, `~` expanded, relative to the
cwd, a regular file of at most MAX_SCRIPT bytes whose real path is under the cwd, $TMPDIR, /tmp or
/private/tmp; any failure adds nothing. A replay (`repeats --commands`) passes the row's `script` body
instead, as the body of the first of those files (none: the body is not read). `rules` = ids of the active hook rules
that match the raw command, computed in memory, so a report can tell covered shapes from uncovered ones
without the raw text; `bypassed` = the ids of rules that match but were skipped by `# agent-cli: allow <id>` (a deny
rule's chore is only ever recorded when bypassed). Day files older than AGENT_CLI_SHAPES_KEEP_DAYS
(default 45) are deleted, at most once a day, and so are the repeat warning's <store>/warned/ marks
last written before that.
"""
import datetime as dt
import functools
import itertools
import json
import os
import re
import stat
import sys
from pathlib import Path

from . import core, hook
from .effects import MAX_EFFECTS, effects, ran
from .shape import ENV_FILE, SCRIPT, shape, steps

MAX_SHAPE = 1024
MAX_STEPS = 32
MAX_SCRIPT = 256 * 1024
MAX_SCRIPTS = 4
TMP_ROOTS = ("/tmp", "/private/tmp")
# A shell script's step is kept only when it reads as a command: a lowercase command name (or `.`, `[`, `[[`,
# or a path-run lowercase name) followed by at least one word, or a script run by path alone (`<path>/x.sh`).
# A line of one bare word (a token, a key body) or a mixed-case first word is a value the script holds,
# and the shape keeps a command word as written, so such a step would store it. Lost: bare `make`/`exit`.
# On top of that shape, the first word must be a command (_script_step): a BUILTINS word, a path run whose
# last part is a script name or on PATH, or an executable on PATH. An array element on its own line (`sk_x
# two`, `ab/sk_x two`), a case label (`prod) deploy now`) or any token followed by a word names no command.
SCRIPT_STEP = re.compile(r"(?:\.|\[\[?|(?:<path>/)?[a-z_][a-z0-9_.+-]{0,31}) .+|<path>/" + SCRIPT.pattern)
BUILTINS = frozenset(("cd", "echo", "printf", "export", "set", "unset", "source", ".", "[", "[[", "test", "read",
                      "local", "declare", "eval", "exec", "exit", "return", "trap", "shift", "wait", "true", "false"))
SHELL_EXT = (".sh", ".bash", ".zsh")
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
    exit_code = 0
    if data.get("hook_event_name") == "PostToolUseFailure":
        m = EXIT_CODE.search(str(data.get("error") or ""))
        exit_code = int(m[1]) if m else None
    row = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "session": _short(data.get("session_id")),
           "agent": _short(data.get("agent_id")), "repo": _repo(data.get("cwd")),
           "exit": exit_code, **fields(command, hook.load_rules(), cwd=data.get("cwd"))}
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    append_line(d / f"{now:%Y-%m-%d}.jsonl", json.dumps(row))
    _sweep(d, now)
    return row


def append_line(path, line):
    """Append line + newline to path (created 0600) with one os.write on an O_APPEND fd: atomic for a short
    line, so parallel sessions never interleave."""
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def fields(command, rules, prefixes=None, tools=None, cwd=None, script_text=None):
    """{shape, steps, effects, rules, bypassed} for a raw command, the part of a row that comes from its text
    (the report calls this too, for raw commands it replays): the shape cut to MAX_SHAPE, the first MAX_STEPS
    step shapes (each already cut to MAX_SHAPE), then those of the shell scripts it runs, its effect signals
    and its scripts' (at most MAX_EFFECTS), the ids of the rules that fire, and those of the rules that match
    but a `# agent-cli: allow <id>` comment skips. prefixes/tools: as shape()'s (None reads the seams).
    cwd: the event's cwd, which allows reading the scripts from disk (None: no disk read). script_text: the
    body of the first script file _scripts would read, read instead of the disk."""
    s = shape(command, prefixes, tools)
    hits = [r["id"] for r in hook.evaluate(command, rules, honor_bypass=False)]
    allowed = set(hook.BYPASS.findall(command)) if hits else set()
    st, fx = steps(command, prefixes, tools), _effects(command, prefixes)
    for text, shell in _scripts(command, prefixes, cwd, script_text):
        try:
            st += list(itertools.islice(filter(_script_step, steps(text, prefixes, tools)), MAX_STEPS)) if shell else []
            fx += effects(text, prefixes, code=not shell)
        except Exception:  # a script we cannot read adds nothing; the row stays
            continue
    return {"shape": s[:MAX_SHAPE] if s else None, "steps": list(dict.fromkeys(st))[:MAX_STEPS],
            "effects": sorted(set(fx))[:MAX_EFFECTS],
            "rules": [i for i in hits if i not in allowed],
            "bypassed": [i for i in hits if i in allowed]}


def _effects(command, prefixes):
    """effects() of command, or [] when it fails (e.g. RecursionError on ~1000 nested `$(`): the row still
    keeps its shape and steps."""
    try:
        return effects(command, prefixes)
    except Exception:  # the newest reader must never cost the row
        return []


def _script_step(x):
    """True when a script's step shape x reads as a command: SCRIPT_STEP's shape and a command first word."""
    w = x.split(" ", 1)[0]
    run = w[len("<path>/"):] if w.startswith("<path>/") else None  # a path run: its last part must name one
    return bool(SCRIPT_STEP.fullmatch(x)) and (
        w in BUILTINS or bool(run and SCRIPT.fullmatch(run)) or _on_path(run or w))


@functools.lru_cache(maxsize=256)
def _on_path(word):
    """core.on_path(word), cached: the recording machine's PATH plus the dirs `self env` adds, so a step whose
    tool is not installed here is dropped."""
    return core.on_path(word)


def _candidate(path, shell):
    """True when the file at path, run (by a shell when shell), may be read: never an env file (ENV_FILE
    basenames, direnv's .envrc), and one a shell runs or sources only when named <name>.sh/.bash/.zsh
    (`source secrets` loads secrets, it is no script)."""
    b = os.path.basename(path)
    if b == ".envrc" or ENV_FILE.fullmatch(b):
        return False
    return not shell or bool(SCRIPT.fullmatch(b)) and b.endswith(SHELL_EXT)


def _scripts(command, prefixes, cwd, script_text):
    """[(text, read as shell)] of the scripts command runs. The candidates are ran()'s files that _candidate
    allows, at most MAX_SCRIPTS; none, nothing is read. script_text, cut to MAX_SCRIPT, is the first
    candidate's body (read as shell when a shell runs it); with no script_text, each candidate _read_script
    allows, when there is a cwd."""
    try:
        found = ran(command, prefixes)
    except Exception:  # e.g. RecursionError: no scripts, the row stays
        found = []
    runs = [(p, sh) for p, sh in dict.fromkeys(found) if _candidate(p, sh)][:MAX_SCRIPTS]
    if not runs:
        return []
    if script_text is not None:
        return [(script_text[:MAX_SCRIPT], runs[0][1])]
    if not cwd:
        return []
    texts = ((_read_script(path, cwd), shell) for path, shell in runs)
    return [(t, shell) for t, shell in texts if t is not None]


def _read_script(path, cwd):
    """The text of the script at path (`~` expanded, relative to cwd), or None unless its real path is a
    regular file of at most MAX_SCRIPT bytes under cwd, $TMPDIR, /tmp or /private/tmp (a symlink out is
    refused), and it decodes as UTF-8."""
    try:
        real = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
        roots = {os.path.realpath(r) for r in (cwd, os.environ.get("TMPDIR"), *TMP_ROOTS) if r}
        st = os.stat(real)
        if not (stat.S_ISREG(st.st_mode) and st.st_size <= MAX_SCRIPT
                and any(os.path.commonpath((real, r)) == r for r in roots)):
            return None  # checked before open: a FIFO would block the read
        with open(real, encoding="utf-8") as f:
            return f.read(MAX_SCRIPT)
    except (OSError, ValueError):  # missing, unreadable, undecodable (UnicodeDecodeError), NUL in the path
        return None


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
    """Delete day files older than the keep window, and warned/ marks last written before it, once per UTC
    day (the marker holds the last day)."""
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
    cutoff = now - dt.timedelta(days=max(keep, 1))
    day = f"{cutoff:%Y-%m-%d}"
    old = [f for f in d.iterdir() if (m := DAY_FILE.fullmatch(f.name)) and m[1] < day]
    if (d / "warned").is_dir():  # the repeat warning's once-per-session marks (hook.py), by last write
        old += [f for f in (d / "warned").iterdir() if _mtime(f) < cutoff.timestamp()]
    for f in old:
        try:
            f.unlink()
        except FileNotFoundError:  # another session's sweep got it first
            pass


def _mtime(f):
    try:
        return f.stat().st_mtime
    except FileNotFoundError:  # deleted since the listing: nothing to sweep
        return float("inf")


def hook_verb(event_name):
    """Read a PostToolUse/PostToolUseFailure event on stdin and record it. Prints nothing, never raises:
    a recorder must never fail a tool call."""
    try:
        data = json.loads(sys.stdin.read())
        data.setdefault("hook_event_name", event_name)
        record(data)
    except Exception:
        return
