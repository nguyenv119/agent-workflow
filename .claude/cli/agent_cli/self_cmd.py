"""`agent-cli self`: PATH wiring (the export SessionStart evals, and a symlink for other shells)."""
import os
from pathlib import Path

from . import core
from .core import Exit


def register(sub):
    _, ss = core.group(sub, "self", "PATH wiring",
                       ["agent-cli self env", "agent-cli self install --bin-dir ~/.local/bin"])
    p = core.subcommand(ss, "env",
                        "print the PATH export (.claude/bin and project CLI dirs), then CLAUDE_PROJECT_DIR's"
                        " when set; empty if AGENT_CLI_PATH_OFF=1",
                        ["agent-cli self env", 'eval "$(agent-cli self env)"', "agent-cli self env --json"])
    p.add_argument("--json", action="store_true", help="one JSON object: the dirs and both export lines")
    p.set_defaults(func=self_env)
    p = core.subcommand(ss, "install", "symlink agent-cli into a bin dir, for shells outside Claude Code",
                        ["agent-cli self install --bin-dir ~/.local/bin",
                         "agent-cli self install --bin-dir ~/.local/bin --dry-run --json"])
    p.add_argument("--bin-dir", required=True, help="existing dir on your PATH")
    p.add_argument("--dry-run", action="store_true", help="say what would be linked, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=self_install)


def self_env(a):
    def esc(v):  # safe inside double quotes when the line is sourced
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    off = os.environ.get("AGENT_CLI_PATH_OFF") == "1"
    dirs = [] if off else core.path_dirs()
    line = f'export PATH="{":".join(esc(d) for d in dirs)}:$PATH"' if dirs else ""
    pd = "" if off else os.environ.get("CLAUDE_PROJECT_DIR", "")
    pd_line = f'export CLAUDE_PROJECT_DIR="{esc(pd)}"' if pd else ""
    if a.json:
        core.emit({"dirs": dirs, "export": line, "project_dir": pd_line}, True)
    else:
        for ln in (line, pd_line):
            if ln:
                print(ln)


def self_install(a):
    shim = core.BIN / "agent-cli"
    bindir = Path(os.path.expanduser(a.bin_dir))
    example = "agent-cli self install --bin-dir ~/.local/bin"
    if not bindir.is_dir():
        core.die(Exit.FAIL, f"--bin-dir {bindir} is not a directory", example)
    link = bindir / "agent-cli"
    if link.is_symlink() and os.readlink(link) == str(shim):
        status = "exists"
    elif link.exists() or link.is_symlink():
        core.die(Exit.REFUSED, f"{link} exists and is not a link to {shim}; remove it first", example)
    elif a.dry_run:
        status = "would-link"
    else:
        link.symlink_to(shim)
        status = "linked"
    if a.json:
        core.emit({"status": status, "path": str(link), "target": str(shim)}, True)
    else:
        print(f"{status}: {link} -> {shim}")
