"""Shared helpers for agent-cli and the project CLIs that import it (Python 3.9+, stdlib only).

Stable API: build_parser, Exit, emit, die, parse_duration, find_seam (plus duration, git, child_env,
find_project_file, run_seam, subcommand, group).
"""
import argparse
import enum
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path(__file__).resolve().parents[2]  # the .claude/ this agent-cli is installed in
BIN = CLAUDE_DIR / "bin"
SEAMS = "agent-cli-hooks"


class Exit(enum.IntEnum):
    OK = 0
    FAIL = 1
    REFUSED = 2
    BLOCKED = 3
    TIMEOUT = 4
    PARTIAL = 5


def die(code, msg, example):
    """R3: `error: <msg>` and a correct invocation on stderr, then exit with code."""
    sys.stderr.write(f"error: {msg}\n  example: {example}\n")
    sys.exit(int(code))


def emit(obj, json_mode):
    """R7: one JSON object per line with --json, else `key: value` lines."""
    if json_mode:
        print(json.dumps(obj))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


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
    """argparse with help as `<prog>: <summary>`, usage, options, then an Examples: block (R2);
    usage errors exit 2 with the first example (R3)."""

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
    """A command group: (parser, its subparsers); called bare it prints its own help (R1)."""
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


def path_dirs():
    """The dirs `self env` puts ahead of $PATH: .claude/bin, then the `path` seam's (relative = project root)."""
    f = find_seam("path")
    root = f.parents[2] if f else None
    return [str(BIN)] + [str(root / d) if not os.path.isabs(d) else d for d in seam_lines("path")]


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
