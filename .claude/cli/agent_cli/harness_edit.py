"""The harness-edit-on-main rule: harness files change only on a branch, through a PR.

where(path, cwd) answers for one file: "on <branch>" when its checkout is on the default branch
(refs/remotes/origin/HEAD, fallback main), "on a detached HEAD", "outside git" for a non-git path under
$CLAUDE_PROJECT_DIR, or None (allow: another branch, or a path no harness-paths glob matches). Symlinks are
followed first, so a dir linking into a main checkout is that checkout. bash_targets(code) lists the files a
command's code view writes (>, >>, tee, sed -i, cp/mv/ln/rm targets). git errors raise: the hook fails open.
"""
import os
import re
import subprocess

from . import core
from .harness import glob_match

RULE_ID = "harness-edit-on-main"
DEFAULT_PATHS = (".claude/skills/**", ".claude/commands/**", ".claude/agents/**", ".claude/hooks/**",
                 ".claude/bin/**", ".claude/cli/**", ".claude/settings.json", ".claude/CLAUDE.md",
                 ".claude/project-rules.md", ".claude/harness-addon/**", "AGENTS.md", "CLAUDE.md")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
MESSAGE = ("Harness file {where}: make a worktree (agent-cli wt new <bead-id> <slug> --base origin/main), edit "
           "there, open a PR; it merges with agent-cli pr land --verified-head once CI is green.")
# `>`/`>>`/`>|` at a word start, optionally fd-prefixed (`2>`, `&>`); `>&2` and `>(...)` write no file, and a
# `>` inside a word (`</promise>`) is no redirect
REDIRECT = re.compile(r"(?<![^\s;&|(])(?:\d+|&)?>>?\|?(?![&(>])\s*([^\s;&|()<>]+)")
SEGMENT = re.compile(r"[;&|()\n]")
LEAD = frozenset("do then else elif if while until ! { sudo env command time nohup".split())
ALL_ARGS = frozenset(("tee", "rm", "mv"))  # every operand is written (mv also removes its sources)
LAST_ARG = frozenset(("cp", "ln"))  # only the destination is written


def rule(where="on <branch>"):
    return {"id": RULE_ID, "mode": "deny", "source": "builtin", "message": MESSAGE.format(where=where)}


def bash_targets(code):
    """Paths a command's code view writes. Quoted operands are "" in the code view, so they never match:
    ponytail: a quoted target slips through; resolve the raw words with shlex if that becomes a real gap."""
    out = REDIRECT.findall(code)
    for seg in SEGMENT.split(code):
        words = seg.split()
        while words and (words[0] in LEAD or re.match(r"[A-Za-z_]\w*=", words[0])):
            words.pop(0)
        if not words:
            continue
        name, args = words[0].rsplit("/", 1)[-1], [w for w in words[1:] if not w.startswith("-")]
        if name == "sed" and any(w.startswith("-i") or w.startswith("--in-place") for w in words[1:]):
            out += args
        elif name in ALL_ARGS:
            out += args
        elif name in LAST_ARG:
            out += args[-1:]
    return [t for t in out if t != '""']


def _git(d, *args):
    """git -C d args: stripped stdout, or None when git exits non-zero (OSError/timeout propagate)."""
    r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, timeout=2)
    return r.stdout.strip() if r.returncode == 0 else None


def _harness(rel):
    value = core.rules_line("harness", "harness-paths")
    return any(glob_match(g, rel) for g in (core.names(value) if value else DEFAULT_PATHS))


def where(path, cwd):
    """Why writing path breaks the rule ("on main", "on a detached HEAD", "outside git"), or None to allow."""
    p = os.path.realpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(path))))
    d = p
    while not os.path.isdir(d):
        d = os.path.dirname(d)
    top = _git(d, "rev-parse", "--show-toplevel")
    if top is None:
        base = os.environ.get("CLAUDE_PROJECT_DIR")
        rel = os.path.relpath(p, os.path.realpath(base)) if base else None
        return "outside git" if rel and _harness(rel) else None
    if not _harness(os.path.relpath(p, os.path.realpath(top))):
        return None
    branch = _git(d, "symbolic-ref", "--short", "-q", "HEAD")
    if branch is None:
        return "on a detached HEAD"
    default = _git(d, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main"
    return f"on {branch}" if branch == default.removeprefix("origin/") else None


def first(paths, cwd):
    """where() of the first path in paths that breaks the rule, or None."""
    return next((w for w in (where(t, cwd) for t in paths) if w), None)
