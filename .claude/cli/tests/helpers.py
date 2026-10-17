"""Shared test helpers for .claude/cli tests (stdlib only).

make_project() builds a temp git repo with agent-cli installed in its .claude/ (a copy of this
checkout's), so seam lookups see that project and never this repo's own .claude/.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLAUDE = Path(__file__).resolve().parents[2]  # this checkout's .claude/
CORPUS = Path(__file__).resolve().parent / "hook_allow_corpus.txt"
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]
# Env vars that change agent-cli behaviour; tests start from none of them.
CLEAN = ("CLAUDE_PROJECT_DIR", "AGENT_CLI_HOOKS", "AGENT_CLI_PATH_OFF")


class TmpCase(unittest.TestCase):
    """A test case with self.tmp, a fresh temp dir (symlinks resolved) removed after each test."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# REVIEW: mocking core dependency (time). A wait loop's interval and cap run in minutes; this clock lets a
# 40-minute wait run instantly. sleep() advances it and records each sleep.
class FakeClock:
    """A fake monotonic clock: call it for the time, sleep(s) advances it and appends s to sleeps."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run(cmd, cwd, check=True):
    """Run cmd in cwd and return the CompletedProcess (text output captured)."""
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def make_project(root, seams=None, install=True, hooks=()):
    """A git repo at root, agent-cli installed in root/.claude unless install=False.

    seams: {name: text} written under root/.claude/agent-cli-hooks/. hooks: dir names under this
    checkout's .claude/hooks/ copied into root/.claude/hooks/. Returns root as a Path.
    """
    root = Path(root)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        run(GIT + ["init", "-q"], root)
    if install:
        (root / ".claude/bin").mkdir(exist_ok=True)
        shutil.copy2(CLAUDE / "bin/agent-cli", root / ".claude/bin/agent-cli")
        shutil.copytree(CLAUDE / "cli/agent_cli", root / ".claude/cli/agent_cli",
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    for name in hooks:
        shutil.copytree(CLAUDE / "hooks" / name, root / ".claude/hooks" / name,
                        ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
    for name, text in (seams or {}).items():
        f = root / ".claude/agent-cli-hooks" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


def agent_cli(project, *args, stdin=None, env=None, cwd=None):
    """Run project's installed agent-cli; env adds to (or with None values removes from) a clean os.environ."""
    e = {k: v for k, v in os.environ.items() if k not in CLEAN}
    for k, v in (env or {}).items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return subprocess.run([sys.executable, str(Path(project) / ".claude/bin/agent-cli"), *args],
                          cwd=str(cwd or project), input=stdin, env=e, text=True, capture_output=True)


def bash_event(command, tool="Bash"):
    """A PreToolUse payload as Claude Code sends it on stdin."""
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}})


def corpus_entries(path=CORPUS):
    """Commands in an allow corpus: one per line; a multi-line command sits between two `---` lines.

    Blank lines and lines starting with `#` are skipped. Returns [(command, expected_warn_id or None)].
    """
    out, block = [], None
    for line in Path(path).read_text().splitlines():
        if line.strip() == "---":
            if block is not None:
                out.append("\n".join(block))
            block = None if block is not None else []
        elif block is not None:
            block.append(line)
        elif line.strip() and not line.startswith("#"):
            out.append(line)
    res = []
    for cmd in out:
        warn = cmd.rsplit("  # expect-warn: ", 1)
        res.append((cmd, warn[1].strip() if len(warn) == 2 else None))
    return res


def post_event(command, name="PostToolUse", session="sess-a", cwd="/", **extra):
    """A PostToolUse (or PostToolUseFailure) payload as Claude Code sends it on stdin.

    cwd=None leaves cwd out, so the recorded row's repo is null and record() runs no git."""
    e = {"session_id": session, "hook_event_name": name, "tool_name": "Bash",
         "tool_input": {"command": command}, "tool_use_id": "toolu_demo"}
    if cwd is not None:
        e["cwd"] = str(cwd)
    if name == "PostToolUse":
        e["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False}
    e.update(extra)
    return e


def store_rows(store):
    """Every row in every day file of a shape store dir."""
    return [json.loads(ln) for f in sorted(Path(store).glob("*.jsonl")) for ln in f.read_text().splitlines()]


def set_env(case, **values):
    """Set (or, for None, remove) env vars for one test; case's cleanup restores them."""
    for k, v in values.items():
        old = os.environ.get(k)
        case.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None
                        else os.environ.pop(k, None))
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
