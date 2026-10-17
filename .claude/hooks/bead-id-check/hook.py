#!/usr/bin/env python3
"""PreToolUse Bash hook: refuse a git commit whose staged comment lines add a bead id.

Reads the Claude Code event JSON on stdin. Acts only on `git commit` and `git -C <dir> commit`;
runs check_comment_bead_ids.py --staged in the repo being committed to, which does nothing when
that repo's .claude/project-rules.md sets no bead-id-prefix under ## beads.

Exit codes: 0 allow (also on any internal error: fail open), 2 block with the hits on stderr.
Bypass: append  # agent-cli: allow bead-id-comment  to the command; AGENT_CLI_HOOKS=off skips it.

Example:
  echo '{"tool_input":{"command":"git commit -m x"},"cwd":"."}' | python3 hook.py
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True  # runs inside the repo being committed to; leave no __pycache__ there

# same grammar as agent_cli.hook.BYPASS, for this one rule id
BYPASS = re.compile(r"#\s*agent-cli:\s*allow\s+bead-id-comment(?![\w.-])")
CHECK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_comment_bead_ids.py")


def repo_dir(cmd, cwd):
    m = re.search(r"git\s+-C\s+(\S+)\s+commit\b", cmd)
    if m:
        return os.path.join(cwd, os.path.expanduser(shlex.split(m[1])[0]))
    for m in re.finditer(r"(?:^|&&|;)\s*cd\s+(\S+)", cmd.split("git commit")[0]):
        cwd = os.path.join(cwd, os.path.expanduser(shlex.split(m[1])[0]))
    return cwd


def main():
    if os.environ.get("AGENT_CLI_HOOKS") == "off":
        return 0
    try:
        ev = json.load(sys.stdin)
        cmd = ev.get("tool_input", {}).get("command", "")
        if not re.search(r"\bgit\b(\s+-C\s+\S+)?\s+commit\b", cmd) or BYPASS.search(cmd):
            return 0
        d = repo_dir(cmd, ev.get("cwd") or os.getcwd())
        p = subprocess.run([sys.executable, CHECK, "--staged"], cwd=d, capture_output=True, text=True, timeout=60)
    except Exception:
        return 0
    if p.returncode == 9:
        sys.stderr.write(p.stdout + p.stderr + "\nRemove the bead id from these comments: say the why in plain words; "
                         "the id belongs in the commit message or PR. (bypass: append  # agent-cli: allow bead-id-comment)\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
