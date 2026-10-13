"""`agent-cli lib`: the import path for project CLIs."""
from pathlib import Path

from . import core

PKG = Path(__file__).resolve().parent


def register(sub):
    _, ls = core.group(sub, "lib", "import path for project CLIs",
                       ["agent-cli lib path", 'PYTHONPATH="$(agent-cli lib path)" python3 my-cli.py'])
    p = core.subcommand(ls, "path", "print the dir to put on sys.path for `from agent_cli.core import ...`",
                        ["agent-cli lib path"])
    p.set_defaults(func=lambda a: print(PKG.parent))
