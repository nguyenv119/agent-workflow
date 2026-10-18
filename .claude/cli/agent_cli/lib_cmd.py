"""`agent-cli lib`: the import path for project CLIs."""
import os
from pathlib import Path

from . import core

PKG = Path(os.path.abspath(__file__)).parent  # unresolved, like core.CLAUDE_DIR


def register(sub):
    _, ls = core.group(sub, "lib", "import path for project CLIs",
                       ["agent-cli lib path", 'PYTHONPATH="$(agent-cli lib path)" python3 my-cli.py'])
    p = core.subcommand(ls, "path", "print the dir to put on sys.path for `from agent_cli.core import ...`",
                        ["agent-cli lib path", "agent-cli lib path --json"])
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=lambda a: core.out(a, {"path": str(PKG.parent)}, str(PKG.parent)))
