"""agent-cli entry: argparse root with hook, eval, self and lib (later: wt, pr, lock, wait, log)."""
import os
import subprocess
from pathlib import Path

from . import core, hook
from .core import Exit, subcommand

PKG = Path(__file__).resolve().parent
EVAL_LIB = PKG / "eval_lib"


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

    h = subcommand(sub, "hook", "PreToolUse rules that point hand-rolled chores at the right command",
                   ["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'", "agent-cli hook rules --json"])
    h.set_defaults(func=lambda a, p=h: p.print_help())
    hs = h.add_subparsers(title="Commands", metavar="<command>")
    p = subcommand(hs, "pretooluse", "read a PreToolUse event on stdin; print a deny or warn envelope, or nothing",
                   ["echo '{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"}}' | agent-cli hook pretooluse"])
    p.set_defaults(func=lambda a: hook.pretooluse())
    p = subcommand(hs, "test", "show which rules fire on a command (exit 2 when one denies)",
                   ["agent-cli hook test --cmd 'echo \"<promise>WIN</promise>\"'",
                    "agent-cli hook test --cmd 'gh pr checks 12' --json"])
    p.add_argument("--cmd", required=True, help="the Bash command to check")
    p.add_argument("--json", action="store_true", help="one JSON object: decision and rules")
    p.set_defaults(func=lambda a: hook.test(a.cmd, a.json))
    p = subcommand(hs, "rules", "list built-in and project rules and whether each is active",
                   ["agent-cli hook rules", "agent-cli hook rules --json"])
    p.add_argument("--json", action="store_true", help="one JSON object per rule")
    p.set_defaults(func=lambda a: hook.list_rules(a.json))

    e = subcommand(sub, "eval", "verdict library for loop evals",
                   ['source "$(agent-cli eval lib --sh)"', "agent-cli eval lib --py"])
    e.set_defaults(func=lambda a, p=e: p.print_help())
    es = e.add_subparsers(title="Commands", metavar="<command>")
    p = subcommand(es, "lib", "print the verdict library path (ok, fail, refuse, blocked, win)",
                   ["agent-cli eval lib --sh", "agent-cli eval lib --py"])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sh", action="store_true", help="bash library, to source")
    g.add_argument("--py", action="store_true", help="python module, to import")
    p.set_defaults(func=lambda a: print(EVAL_LIB / ("verdict.sh" if a.sh else "verdict.py")))

    s = subcommand(sub, "self", "PATH wiring", ["agent-cli self env", "agent-cli self install --bin-dir ~/.local/bin"])
    s.set_defaults(func=lambda a, p=s: p.print_help())
    ss = s.add_subparsers(title="Commands", metavar="<command>")
    p = subcommand(ss, "env", "print the PATH export (.claude/bin and project CLI dirs); empty if AGENT_CLI_PATH_OFF=1",
                   ["agent-cli self env", 'eval "$(agent-cli self env)"'])
    p.set_defaults(func=lambda a: self_env())
    p = subcommand(ss, "install", "symlink agent-cli into a bin dir, for shells outside Claude Code",
                   ["agent-cli self install --bin-dir ~/.local/bin",
                    "agent-cli self install --bin-dir ~/.local/bin --dry-run --json"])
    p.add_argument("--bin-dir", required=True, help="existing dir on your PATH")
    p.add_argument("--dry-run", action="store_true", help="say what would be linked, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=self_install)

    lb = subcommand(sub, "lib", "import path for project CLIs",
                    ["agent-cli lib path", 'PYTHONPATH="$(agent-cli lib path)" python3 my-cli.py'])
    lb.set_defaults(func=lambda a, p=lb: p.print_help())
    ls = lb.add_subparsers(title="Commands", metavar="<command>")
    p = subcommand(ls, "path", "print the dir to put on sys.path for `from agent_cli.core import ...`",
                   ["agent-cli lib path"])
    p.set_defaults(func=lambda a: print(PKG.parent))
    return root


def self_env():
    if os.environ.get("AGENT_CLI_PATH_OFF") == "1":
        return
    esc = [d.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
           for d in core.path_dirs()]
    print(f'export PATH="{":".join(esc)}:$PATH"')


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


def main(argv=None):
    root = build()
    args = root.parse_args(argv)
    return int(args.func(args) or Exit.OK)
