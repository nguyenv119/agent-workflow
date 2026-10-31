"""agent-cli entry: argparse root with hook, eval, self, lib, wt, wait, lock, pr, log, page, card, redact, repeats and harness."""
import os
import subprocess
from pathlib import Path

from . import card, core, eval_cmd, harness, hook, lib_cmd, lock, log, page, pr, redact, repeats, self_cmd, wait, wt
from .core import Exit

# Top-level commands in help order; each module's register(sub) adds its own. A new command is one entry.
COMMANDS = (hook, eval_cmd, self_cmd, lib_cmd, wt, wait, lock, pr, log, page, card, redact, repeats, harness)


class Root(core.Parser):
    def extra_help(self):
        return "\nProject CLIs:\n" + ("".join(f"  {n}  {s}\n" for n, s in project_clis())
                                       or "  (none: list their dirs in .claude/agent-cli-hooks/path)\n")

    def format_help(self):
        """The help, ending with the one exit-code table (house-rules "exit-code table")."""
        rows = "".join(f"  {int(e)}  {e.name.lower():<8} {core.EXIT_MEANING[e]}\n" for e in Exit)
        return f"{super().format_help()}\nExit codes (every agent-cli command):\n{rows}"


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
                summary="shared chores for agent sessions (worktrees, PR checks, waits, locks, eval verdicts, page and card checks)",
                examples=["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'",
                          'source "$(agent-cli eval lib --sh)"', "agent-cli self env",
                          "agent-cli harness drift --json"])
    sub = root.add_subparsers(title="Commands", metavar="<command>", parser_class=core.Parser)
    # a bare group shows its help, never prompts (house-rules "Never prompt")
    root.set_defaults(func=lambda a: root.print_help())
    for module in COMMANDS:
        module.register(sub)
    return root


def main(argv=None):
    root = build()
    args = root.parse_args(argv)
    return int(args.func(args) or Exit.OK)
