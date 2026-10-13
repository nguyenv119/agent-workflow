"""`agent-cli eval`: the verdict library for loop evals."""
from pathlib import Path

from . import core

EVAL_LIB = Path(__file__).resolve().parent / "eval_lib"


def register(sub):
    _, es = core.group(sub, "eval", "verdict library for loop evals",
                       ['source "$(agent-cli eval lib --sh)"', "agent-cli eval lib --py"])
    p = core.subcommand(es, "lib", "print the verdict library path (ok, fail, refuse, blocked, win)",
                        ["agent-cli eval lib --sh", "agent-cli eval lib --py"])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sh", action="store_true", help="bash library, to source")
    g.add_argument("--py", action="store_true", help="python module, to import")
    p.set_defaults(func=lambda a: print(EVAL_LIB / ("verdict.sh" if a.sh else "verdict.py")))
