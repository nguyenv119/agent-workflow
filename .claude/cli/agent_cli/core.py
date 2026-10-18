"""Shared helpers for agent-cli and the project CLIs that import it (Python 3.9+, stdlib only).

Stable API: build_parser, Exit, emit, out, die, parse_duration, find_seam (plus duration, window, git,
child_env, find_project_file, run_seam, subcommand, group).
"""
import argparse
import datetime
import enum
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# the .claude/ this agent-cli is installed in, links left as they are: a project whose .claude/cli is a
# dir symlink into a shared checkout still gets its own .claude/ (bin, seams), not that checkout's
CLAUDE_DIR = Path(os.path.abspath(__file__)).parents[2]
BIN = CLAUDE_DIR / "bin"
SEAMS = "agent-cli-hooks"


class Exit(enum.IntEnum):
    OK = 0
    FAIL = 1
    REFUSED = 2
    BLOCKED = 3
    TIMEOUT = 4
    PARTIAL = 5


# What each code means for every command (house-rules "exit-code table"); the root --help prints it.
EXIT_MEANING = {Exit.OK: "done, or the check passed", Exit.FAIL: "checked and failed",
                Exit.REFUSED: "usage error or refused; the error names a correct invocation",
                Exit.BLOCKED: "blocked by something outside the command (a lock holder, gh, a setup hook)",
                Exit.TIMEOUT: "the --cap ran out first, or paused: a usage limit or no plan token (harness canary)",
                Exit.PARTIAL: "not finished: still waiting, partly done, or could not run (harness canary); retry"}


def die(code, msg, example):
    """cli-for-agents "Fail fast with actionable errors": `error: <msg>` and a correct invocation on stderr, then
    exit with code."""
    sys.stderr.write(f"error: {msg}\n  example: {example}\n")
    sys.exit(int(code))


def emit(obj, json_mode):
    """house-rules "--json": one JSON object per line with --json, else `key: value` lines."""
    if json_mode:
        print(json.dumps(obj))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def out(a, row, text):
    """house-rules "--json" for one result: `row` as JSON with --json (a.json), else the one-line `text`."""
    if a.json:
        emit(row, True)
    else:
        print(text)


def write_atomic(path, text):
    """Replace path's content in one rename; an existing file keeps its mode, a new one gets 0666 minus umask."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if path.exists():
        mode = path.stat().st_mode & 0o7777
    else:
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    os.chmod(tmp, mode)
    os.replace(tmp, str(path))


def parse_duration(text):
    """'40m' -> 2400 seconds; suffix s, m, h or d, bare number = seconds."""
    m = re.fullmatch(r"(\d+)([smhd]?)", text.strip())
    if not m:
        raise ValueError(f"bad duration {text!r}: use e.g. 90, 40m, 2h, 1d")
    return int(m[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def duration(text, example):
    """parse_duration(text), or exit 2 with the parse error and example."""
    try:
        return parse_duration(text)
    except ValueError as e:
        die(Exit.REFUSED, str(e), example)


def window(a, default, example):
    """(lo, hi) epoch bounds, lo <= t < hi, from a's --since or --from/--until (a.since, a.from_, a.until):
    --since back from now, else local --from (YYYY-MM-DD or YYYY-MM-DDTHH:MM, inclusive) up to --until
    (a time is exclusive; a bare date includes that whole day). Either end may be open; with neither flag,
    --since default (None: no window at all). Usage errors exit 2 with example."""
    if a.since is not None and (a.from_ or a.until):
        die(Exit.REFUSED, "give --since or --from/--until, not both", example)
    if not (a.from_ or a.until):
        since = a.since or default
        return (time.time() - duration(since, example), float("inf")) if since else (float("-inf"), float("inf"))
    lo = local_time(a.from_, "--from", example) if a.from_ else float("-inf")
    hi = local_time(a.until, "--until", example, end=True) if a.until else float("inf")
    if lo >= hi:
        die(Exit.REFUSED, f"--until {a.until} is not after --from {a.from_}", example)
    return lo, hi


def local_time(text, flag, example, end=False):
    """Epoch of a local YYYY-MM-DD (end: the next midnight) or YYYY-MM-DDTHH:MM, or exit 2 naming the flag."""
    for fmt, days in (("%Y-%m-%d", 1 if end else 0), ("%Y-%m-%dT%H:%M", 0)):
        try:  # naive local time, so a DST change is handled; year 1 or 9999 overflows: refused, not a traceback
            return (datetime.datetime.strptime(text, fmt) + datetime.timedelta(days=days)).timestamp()
        except (ValueError, OverflowError, OSError):
            continue
    die(Exit.REFUSED, f"{flag} {text!r} is not a usable time: use YYYY-MM-DD or YYYY-MM-DDTHH:MM", example)


def git(*args, cwd=".", check=True, example="agent-cli --help", input=None, strip=True, timeout=600):
    """git's stdout (stripped unless strip=False), or None when it fails (non-zero exit, git missing,
    or past timeout seconds); with check, a failure exits 1 with git's error and example instead.
    Bytes that are not UTF-8 round-trip (surrogateescape), so a diff fed back to git through input
    is unchanged."""
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], input=input, capture_output=True,
                           encoding="utf-8", errors="surrogateescape", timeout=timeout)
        err = r.stderr.strip() if r.returncode else None
    except (OSError, subprocess.SubprocessError) as e:
        r, err = None, str(e)
    if err is not None:
        if check:
            die(Exit.FAIL, f"git {' '.join(args)}: {err}", example)
        return None
    return r.stdout.strip() if strip else r.stdout


class Parser(argparse.ArgumentParser):
    """argparse with help as `<prog>: <summary>`, usage, options, then an Examples: block
    (cli-for-agents "`--help` that works"); usage errors exit 2 with the first example
    (cli-for-agents "Fail fast with actionable errors")."""

    def __init__(self, *args, summary="", examples=(), **kw):
        kw.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        if sys.version_info >= (3, 14):
            kw.setdefault("color", False)  # agents read help as plain text, even under FORCE_COLOR
        super().__init__(*args, **kw)
        self.summary, self.examples = summary, list(examples)

    def extra_help(self):
        return ""

    def format_help(self):
        ex = "".join(f"  {e}\n" for e in self.examples)
        return f"{self.prog}: {self.summary}\n{super().format_help()}{self.extra_help()}\nExamples:\n{ex}"

    def error(self, message):
        die(Exit.REFUSED, message, self.examples[0] if self.examples else f"{self.prog} --help")


def build_parser(name, summary, examples):
    return Parser(prog=name, summary=summary, examples=examples)


def subcommand(sub, name, summary, examples):
    """A subparser whose one-line summary also shows in its parent's command list."""
    return sub.add_parser(name, help=summary, summary=summary, examples=examples)


def group(sub, name, summary, examples):
    """A command group: (parser, its subparsers); called bare it prints its own help (house-rules "Never prompt")."""
    p = subcommand(sub, name, summary, examples)
    p.set_defaults(func=lambda a: p.print_help())
    return p, p.add_subparsers(title="Commands", metavar="<command>")


@functools.lru_cache(maxsize=None)
def _claude_dirs():
    """Candidate .claude/ dirs, first hit wins: the install's, $CLAUDE_PROJECT_DIR's, then walking up
    from the cwd and from the git main-worktree root, stopping before $HOME (never $HOME/.claude or above)."""
    out = [CLAUDE_DIR]
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        out.append(Path(os.environ["CLAUDE_PROJECT_DIR"]) / ".claude")
    home = Path.home().resolve()
    starts = [Path.cwd().resolve()]
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5)
        if common.returncode == 0:
            starts.append(Path(common.stdout.strip()).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        pass
    for start in starts:
        for d in (start, *start.parents):
            if d == home or d in home.parents:
                break
            out.append(d / ".claude")
    return out


def find_project_file(name):
    """The first .claude/<name> in _claude_dirs() order, or None."""
    for d in _claude_dirs():
        if (d / name).is_file():
            return d / name
    return None


def find_seam(name):
    """The first .claude/agent-cli-hooks/<name> in _claude_dirs() order, or None."""
    return find_project_file(f"{SEAMS}/{name}")


def seam_lines(name):
    """Non-blank, non-# lines of a seam file ([] when there is none)."""
    f = find_seam(name)
    lines = f.read_text().splitlines() if f else []
    return [s.strip() for s in lines if s.strip() and not s.lstrip().startswith("#")]


def _scope(text):
    return re.sub(r"\s", "", text).lower()


def _rules_lines(section, key, text):
    """{scope: value} of the `key:` (scope None) and `key[scope]:` lines under `## <section>`; first line wins."""
    if text is None:
        f = find_project_file("project-rules.md")
        text = f.read_text() if f else ""
    m = re.search(rf"^##[ \t]+{re.escape(section)}[ \t]*$(.*?)(?=^##[ \t]|\Z)", text, re.M | re.S)
    out = {}
    for v in re.finditer(rf"^[ \t]*(?:[-*][ \t]+)?`?{re.escape(key)}(?:\[([^\]\n]*)\])?:`?[ \t]*(.*)$",
                         m[1] if m else "", re.M):
        out.setdefault(None if v[1] is None else _scope(v[1]), v[2].strip().strip("`").strip())
    return out


def rules_line(section, key, text=None, scope=None):
    """The value of a `key: value` line under `## <section>` in project-rules.md (text, or the file
    find_project_file finds), stripped of backticks; None when the section or line is missing.
    With scope, a `key[scope]: value` line wins (case and whitespace in the brackets ignored)."""
    lines = _rules_lines(section, key, text)
    return lines.get(_scope(scope) if scope else None, lines.get(None))


def rules_scopes(section, key, text=None):
    """The scopes of the `key[scope]:` lines under `## <section>`, normalised as rules_line compares them."""
    return set(_rules_lines(section, key, text)) - {None}


def names(value):
    """A comma-separated rules line as a list, empty items and backticks dropped."""
    return [n.strip().strip("`") for n in value.split(",") if n.strip().strip("`")]


def path_dirs():
    """The dirs `self env` puts ahead of $PATH: .claude/bin, then the `path` seam's (relative = project root)."""
    f = find_seam("path")
    root = f.parents[2] if f else None
    return [str(BIN)] + [str(root / d) if not os.path.isabs(d) else d for d in seam_lines("path")]


def on_path(word):
    """True when word is an executable on $PATH or in path_dirs(), the dirs `self env` adds for the agent's
    own commands (a hook process does not get them)."""
    return bool(shutil.which(word) or shutil.which(word, path=os.pathsep.join(path_dirs())))


def child_env(drop=()):
    """A copy of os.environ for a child process: the names in drop removed, path_dirs() ahead of $PATH."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PATH"] = os.pathsep.join(path_dirs() + [env.get("PATH", "")])
    return env


def run_seam(path, args, **kw):
    """Run a seam executable with path_dirs() ahead of $PATH; returns the CompletedProcess.

    Output streams to ours by default (worktree setup can run for minutes); kw pass to subprocess.run,
    e.g. capture_output=True, text=True.
    """
    return subprocess.run([str(path), *args], env=child_env(), **kw)
