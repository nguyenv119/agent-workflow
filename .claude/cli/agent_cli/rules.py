"""Rules files for `agent-cli page` and `agent-cli card`: built-in defaults overlaid by a project's JSON.

A rules file is one JSON object. Each top-level key replaces the default's, except when both are objects:
then the file's keys replace the default object's keys one by one. Lookup order: --rules FILE, then the
`<kind>-rules.json` seam (.claude/agent-cli-hooks/), then the defaults alone. Paths in a rules file may
start with ~ and are otherwise relative to the rules file's folder.
"""
import copy
import json
import os
from pathlib import Path

from . import core
from .core import Exit


def load(kind, defaults, path, example):
    """(rules dict, the file it came from or None). A bad file exits 2."""
    src = Path(path) if path else core.find_seam(f"{kind}-rules.json")
    rules = copy.deepcopy(defaults)
    if src is None:
        return rules, None
    try:
        extra = json.loads(Path(src).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        core.die(Exit.REFUSED, f"cannot read rules file {src}: {e}", example)
    if not isinstance(extra, dict):
        core.die(Exit.REFUSED, f"rules file {src} is not a JSON object", example)
    unknown = sorted(set(extra) - set(defaults) - {"comment"})
    if unknown:
        core.die(Exit.REFUSED, f"rules file {src} has unknown keys: {', '.join(unknown)}", example)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(rules.get(k), dict):
            rules[k].update(v)
        else:
            rules[k] = v
    return rules, Path(src).resolve()


def resolve(path, base):
    """An absolute path: ~ expanded, a relative path joined to base (a folder), or None when base is None."""
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return p
    return os.path.join(str(base), p) if base else None
