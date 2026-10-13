"""agent-cli entry: argparse root with hook, eval, self, lib, wt, wait, lock and pr (later: log)."""
import os
import subprocess
from pathlib import Path

from . import core, eval_cmd, hook, lib_cmd, lock, pr, self_cmd, wait, wt
from .core import Exit

# Top-level commands in help order; each module's register(sub) adds its own. A new command is one entry.
COMMANDS = (hook, eval_cmd, self_cmd, lib_cmd, wt, wait, lock, pr)


class Root(core.Parser):
    def extra_help(self):
        return "\nProject CLIs:\n" + ("".join(f"  {n}  {s}\n" for n, s in project_clis())
                                       or "  (none: list their dirs in .claude/agent-cli-hooks/path)\n")


def project_clis():
    """(name, first line of `<cli> --help`) for each executable on the path seam's dirs."""
    out = []
    for d in core.path_dirs()[1:]:
        for f in sorted(Path(d).iterdir()) if os.path.isdir(d) else []:
            if f.is_file() and os.access(f, os.X_OK) and f.name != "agent-cli":
                try:
                    r = subprocess.run([str(f), "--help"], capture_output=True, text=True, timeout=5)
                    first = (r.stdout.splitlines() or ["(no --help output)"])[0]
                except (OSError, subprocess.SubprocessError):
                    first = "(no --help output)"
                out.append((f.name, first))
    return out


def build():
    root = Root(prog="agent-cli",
                summary="shared chores for agent sessions (worktrees, PR checks, waits, locks, eval verdicts)",
                examples=["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'",
                          'source "$(agent-cli eval lib --sh)"', "agent-cli self env"])
    sub = root.add_subparsers(title="Commands", metavar="<command>", parser_class=core.Parser)
    root.set_defaults(func=lambda a: root.print_help())  # a bare group shows its help, never prompts (R1)
    for module in COMMANDS:
        module.register(sub)
    return root


def main(argv=None):
    root = build()
    args = root.parse_args(argv)
    return int(args.func(args) or Exit.OK)
