"""`agent-cli self`: PATH wiring (the export SessionStart evals, and a symlink for other shells)."""
import os
from pathlib import Path

from . import core
from .core import Exit


def register(sub):
    _, ss = core.group(sub, "self", "PATH wiring",
                       ["agent-cli self env", "agent-cli self install --bin-dir ~/.local/bin"])
    p = core.subcommand(ss, "env",
                        "print the PATH export (.claude/bin and project CLI dirs); empty if AGENT_CLI_PATH_OFF=1",
                        ["agent-cli self env", 'eval "$(agent-cli self env)"', "agent-cli self env --json"])
    p.add_argument("--json", action="store_true", help="one JSON object: the dirs and the export line")
    p.set_defaults(func=self_env)
    p = core.subcommand(ss, "install", "symlink agent-cli into a bin dir, for shells outside Claude Code",
                        ["agent-cli self install --bin-dir ~/.local/bin",
                         "agent-cli self install --bin-dir ~/.local/bin --dry-run --json"])
    p.add_argument("--bin-dir", required=True, help="existing dir on your PATH")
    p.add_argument("--dry-run", action="store_true", help="say what would be linked, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=self_install)


def self_env(a):
    dirs = [] if os.environ.get("AGENT_CLI_PATH_OFF") == "1" else core.path_dirs()
    esc = [d.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") for d in dirs]
    line = f'export PATH="{":".join(esc)}:$PATH"' if dirs else ""
    if a.json:
        core.emit({"dirs": dirs, "export": line}, True)
    elif line:
        print(line)


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
