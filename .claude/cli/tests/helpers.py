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


class HookCase(TmpCase):
    """A temp project (self.p) with agent-cli installed; hook(stdin) runs its `hook pretooluse`."""

    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.bin = str(self.p / ".claude/bin")

    def hook(self, stdin, env=None, project=None):
        return agent_cli(project or self.p, "hook", "pretooluse", stdin=stdin,
                         env={"PATH": self.bin + os.pathsep + os.environ["PATH"], **(env or {})})

    def decision(self, p):
        """The hookSpecificOutput the hook printed, or None when it printed nothing (exit must be 0)."""
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


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


def bash_event(command, tool="Bash", cwd=None):
    """A PreToolUse payload as Claude Code sends it on stdin (cwd: the session's, left out when None)."""
    e = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}}
    if cwd is not None:
        e["cwd"] = str(cwd)
    return json.dumps(e)


def edit_event(path, tool="Edit", cwd=None):
    """A PreToolUse payload for an edit tool; NotebookEdit carries notebook_path, the others file_path."""
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    e = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: str(path)}}
    if cwd is not None:
        e["cwd"] = str(cwd)
    return json.dumps(e)


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


# REVIEW: mocking core dependency (gh / the GitHub API, which has no local or test-mode equivalent).
# It replays $FAKE_GH/view.json and checks.json (no checks.json = gh's "no checks reported", exit 1;
# view.err = a failing gh pr view; a `slow` file = sleep that many seconds first; `gh repo view` prints the
# `repo` file, no file = a failing call), appends every argv to calls.log and each `gh pr merge` argv to merge.log.
# `gh api` (an api.err file makes every one fail): .../pulls/N/files applies pr.py's --jq to files.json (REST
# objects, none = []); .../contents/<path>?ref= prints a sha when <path> is a line of contents.txt, else HTTP 404.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys, time
d, a = os.environ["FAKE_GH"], sys.argv[1:]
open(f"{d}/calls.log", "a").write(json.dumps(a) + "\n")
read = lambda name, default: open(f"{d}/{name}").read() if os.path.exists(f"{d}/{name}") else default
if os.path.exists(f"{d}/slow"):
    time.sleep(float(open(f"{d}/slow").read()))
if a[:2] == ["pr", "view"]:
    if os.path.exists(f"{d}/view.err"):
        sys.stderr.write(open(f"{d}/view.err").read()); sys.exit(1)
    print(open(f"{d}/view.json").read())
elif a[:2] == ["pr", "checks"]:
    if not os.path.exists(f"{d}/checks.json"):
        sys.stderr.write("no checks reported on the 'feature' branch\n"); sys.exit(1)
    print(open(f"{d}/checks.json").read())
elif a[:2] == ["pr", "merge"]:
    open(f"{d}/merge.log", "a").write(json.dumps(a) + "\n")
elif a[:2] == ["repo", "view"]:
    if not os.path.exists(f"{d}/repo"):
        sys.stderr.write("no git remotes found\n"); sys.exit(1)
    print(open(f"{d}/repo").read())
elif a[:1] == ["api"]:
    if os.path.exists(f"{d}/api.err"):
        sys.stderr.write(open(f"{d}/api.err").read()); sys.exit(1)
    path, jq = next(x for x in a if x.startswith("repos/")), a[a.index("--jq") + 1] if "--jq" in a else None
    if path.split("?")[0].endswith("/files") and jq == ".[] | .filename, (.previous_filename // empty)":
        for f in json.loads(read("files.json", "[]")):
            print(f["filename"]); f.get("previous_filename") and print(f["previous_filename"])
    elif "/contents/" in path:
        if path.split("/contents/", 1)[1].split("?")[0] not in read("contents.txt", "").split():
            sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
        print("0123abcd")
    else:
        sys.exit(2)
else:
    sys.exit(2)
'''


def view(state="OPEN", merge="CLEAN", head="a" * 40, fork=False):
    """A `gh pr view --json` object for PR 42 of o/r (fork: opened from another repo)."""
    return {"baseRefName": "main", "headRefOid": head, "isCrossRepository": fork, "mergeStateStatus": merge,
            "state": state, "url": "https://github.com/o/r/pull/42"}


def check(name, bucket, link="", state=None, description=""):
    """A `gh pr checks --json` row (state defaults to the bucket in capitals)."""
    return {"name": name, "bucket": bucket, "state": state or bucket.upper(), "description": description,
            "link": link or f"https://ci.example/{name}"}


class GhCase(TmpCase):
    """A project (self.p) with a fake gh on PATH; set_pr() writes what gh will report."""

    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.gh = self.tmp / "gh-data"
        self.gh.mkdir()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(FAKE_GH)
        (bin_ / "gh").chmod(0o755)
        self.env = {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_GH": str(self.gh)}

    def set_pr(self, view_obj, checks=None):
        (self.gh / "view.json").write_text(json.dumps(view_obj))
        if checks is not None:
            (self.gh / "checks.json").write_text(json.dumps(checks))

    def rules(self, text):
        (self.p / ".claude/project-rules.md").write_text(text)

    def cli(self, *args):
        return agent_cli(self.p, "pr", *args, env=self.env)

    def calls(self):
        f = self.gh / "calls.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def merges(self):
        f = self.gh / "merge.log"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
