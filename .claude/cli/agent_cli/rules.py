"""Rules files for `agent-cli page` and `agent-cli card`: built-in defaults overlaid by a project's JSON.

A rules file is one JSON object. Each top-level key replaces the default's, except when both are objects:
then the file's keys replace the default object's keys one by one. Lookup order: --rules FILE, then the
`<kind>-rules.json` seam (.claude/agent-cli-hooks/), then the defaults alone. Paths in a rules file may
start with ~ and are otherwise relative to the rules file's folder.

A file is refused (exit 2) when a key is unknown, at the top level or inside an object the defaults spell out
(an empty default object such as layer.lead or card fields is free-form), when a value's type differs from the
default's (null may turn a top-level rule off), or when a regex does not compile.
"""
import copy
import json
import os
import re
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
    bad = shape_errors({k: v for k, v in extra.items() if k != "comment"}, defaults, "")
    if bad:
        core.die(Exit.REFUSED, f"rules file {src}: {bad[0]}", example)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(rules.get(k), dict):
            rules[k].update(v)
        else:
            rules[k] = v
    bad = regex_errors(rules, "")
    if bad:
        core.die(Exit.REFUSED, f"rules file {src}: {bad[0]}", example)
    return rules, Path(src).resolve()


REGEX_KEYS = {"pattern", "heading", "line", "number", "draft", "legacy_classes", "id_pattern", "needs"}


def shape_errors(extra, defaults, where):
    """Unknown keys and type mismatches of extra against defaults; recurses into objects the defaults spell out."""
    out = []
    for k, v in extra.items():
        name = f"{where}{k}"
        if k not in defaults:
            out.append(f"unknown key {name}")
            continue
        d = defaults[k]
        if d is None or (v is None and not where):
            continue
        if type(v) is not type(d) and not (isinstance(d, float) and type(v) is int):
            out.append(f"{name} must be a {type(d).__name__}, not {type(v).__name__}")
        elif isinstance(d, dict) and d:
            out += shape_errors(v, d, name + ".")
    return out


def regex_errors(value, where):
    """Strings under REGEX_KEYS (at any depth, in lists too) that do not compile."""
    out = []
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        name = f"{where}{k}"
        if (k in REGEX_KEYS or where.endswith("lead.")) and isinstance(v, str):
            try:
                re.compile(v)
            except re.error as e:
                out.append(f"{name}: bad regex {v!r} ({e})")
        else:
            out += regex_errors(v, name + ".")
    return out


def resolve(path, base):
    """An absolute path: ~ expanded, a relative path joined to base (a folder), or None when base is None."""
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return p
    return os.path.join(str(base), p) if base else None
