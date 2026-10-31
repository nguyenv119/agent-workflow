"""`agent-cli harness`: what an installed harness changed since install (drift) and what a session loads (inventory).

drift reads <root>/.claude/HARNESS_FILES.tsv (written by harness-loop/install_harness.py: `path origin sha256`, origin
`base`, `addon`, or `base+addon` for a settings.json with the add-on's settings.add.json merged in) and sorts every
difference: base_edited (port upstream), addon_edited (edit the add-on instead; a `base+addon` row always lands here,
never in base_edited, because its project hooks must never be ported), missing, and files under .claude nobody
installed: owned by a line in .claude/harness-owners.txt (`<glob> <owner> <reason>`, owner
local-data|user|addon-source|project) or unowned. With --user-dir, user-level
skills/commands named like a project one are shadows; a shadow no owner line matches is unexplained.

inventory lists skills, commands, agents, the sha256 of the instruction files and every settings.json hook with the
programs it really runs (guards `[ -x X ]` / `test -f X` resolved, `$CLAUDE_PROJECT_DIR`, `$HOME`, `~` and the
command's own NAME=value assignments expanded, the builtins `exec`/`command` looked through to the program they run):
ok, inert (a guard target is missing, so it silently does nothing) or unresolved. An unparsable settings.json gives an
`error` field, no hooks, and exit 1. `hooks` (hookreg.py) checks every settings.json hook against a registry of
reasons and review dates; `startsize` (startsize.py) counts what a session loads before its first reply, per piece,
from its transcript. Stdlib only.
"""
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from . import canary, core, hookreg, startsize
from .core import Exit

FILES = "HARNESS_FILES.tsv"
BOOKKEEPING = {f".claude/{n}" for n in ("HARNESS_VERSION", "HARNESS_MANIFEST", FILES)}
OWNERS = ("local-data", "user", "addon-source", "project")
OWNER_FILE = ".claude/harness-owners.txt"
DRIFT = "agent-cli harness drift --json"
INVENTORY = "agent-cli harness inventory --json"
FIX = {"base_edited": "port upstream: loop.py port",
       "missing": "reinstall with install_harness.py --force",
       "unowned": f"add an owner line to {OWNER_FILE} (through the add-on) or remove it",
       "unexplained": "add a `user` owner line or remove the user copy"}
MERGED_FIX = "edit the add-on's settings.add.json (or .skipbase) instead, then reinstall"
ORIGINS = ("base", "addon", "base+addon")
SEPARATORS = {"&&", "||", ";", "|", "&", "|&"}
SKIP_WORDS = {"[", "test", "exit", "echo", "sleep", "cat", "true", ":"}
PREFIX_BUILTINS = ("exec", "command")
REDIRECT = re.compile(r"[<>&]*[<>][<>&]*")
GUARD_TESTS = {"-x": lambda p: os.path.isfile(p) and os.access(p, os.X_OK), "-f": os.path.isfile,
               "-e": os.path.exists}


def register(sub):
    _, hs = core.group(sub, "harness", "an installed harness: drift since install, and what a session loads",
                       [DRIFT, INVENTORY, hookreg.EXAMPLE, canary.EXAMPLE, startsize.EXAMPLE])
    p = core.subcommand(hs, "drift", "compare installed files with HARNESS_FILES.tsv and sort every difference "
                        "(exit 1 on unowned, addon_edited, missing or unexplained)",
                        [DRIFT, "agent-cli harness drift --target ../app/.claude --user-dir ~/.claude",
                         "agent-cli harness drift"])
    p.add_argument("--target", default=".", help="project root or its .claude dir (default: .)")
    p.add_argument("--user-dir", help="user-level dir (e.g. ~/.claude) whose skills/commands may shadow project ones")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=drift_cmd)
    p = core.subcommand(hs, "inventory", "skills, commands, agents, instruction-file hashes and hooks with their "
                        "resolved programs (exit 1 if a hook is inert or unresolved, or settings.json is not "
                        "valid JSON: the output then carries an `error` field)",
                        [INVENTORY, "agent-cli harness inventory --target ~ --json", "agent-cli harness inventory"])
    p.add_argument("--target", default=".", help="project root, its .claude dir, or $HOME for the user layer")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=inventory_cmd)
    canary.register(hs)
    hookreg.register(hs)
    startsize.register(hs)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def glob_match(pattern, path):
    """True when posix path matches pattern: `**` = zero or more whole segments, `*`/`?` within one segment."""
    def m(ps, ss):
        if not ps:
            return not ss
        if ps[0] == "**":
            return m(ps[1:], ss) or bool(ss) and m(ps, ss[1:])
        return bool(ss) and fnmatch.fnmatchcase(ss[0], ps[0]) and m(ps[1:], ss[1:])
    return m(pattern.split("/"), path.split("/"))


def load_owners(root):
    """[(glob, owner, reason)] from <root>/.claude/harness-owners.txt ([] when absent); exit 2 on a bad line."""
    f = Path(root) / OWNER_FILE
    rules = []
    for n, line in enumerate(f.read_text().splitlines() if f.is_file() else [], 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            core.die(Exit.REFUSED, f"{f} line {n}: want `<glob> <owner> <reason>`, got {line.strip()!r}",
                     '.claude/** project "why this file is here"')
        if parts[1] not in OWNERS:
            core.die(Exit.REFUSED, f"{f} line {n}: owner {parts[1]!r} is not one of {', '.join(OWNERS)}",
                     '.claude/discover-kb/** local-data "research notes"')
        reason = parts[2].strip()
        rules.append((parts[0], parts[1], reason[1:-1] if len(reason) > 1 and reason[0] == reason[-1] == '"'
                      else reason))
    return rules


def owner_of(rules, path):
    return next((o for g, o, _ in rules if glob_match(g, path)), None)


def root_of(target, example):
    """The project root for --target: the dir itself, or its parent when it is a `.claude` dir with no `.claude`
    child; exit 2 when it is not a directory."""
    t = Path(target)
    if not t.is_dir():
        core.die(Exit.REFUSED, f"--target {target} is not a directory", example)
    t = t.resolve()
    return t.parent if t.name == ".claude" and not (t / ".claude").is_dir() else t


def drift_root(target):
    root = root_of(target, DRIFT)
    if not (root / ".claude" / FILES).is_file():
        core.die(Exit.BLOCKED, f"no .claude/{FILES} under {target}: the install predates it; reinstall with "
                 "harness-loop/install_harness.py --force", "python3 harness-loop/install_harness.py --commit HEAD "
                 "--target <project> --addon <project>/.claude/harness-addon --force")
    return root


def read_tsv(root):
    f = root / ".claude" / FILES
    lines = f.read_text().splitlines()
    if not lines or lines[0] != "path\torigin\tsha256":
        core.die(Exit.REFUSED, f"{f} has no `path origin sha256` header; reinstall", DRIFT)
    rows = {}
    for n, line in enumerate(lines[1:], 2):
        parts = line.split("\t")
        if len(parts) != 3 or parts[1] not in ORIGINS:
            core.die(Exit.REFUSED, f"{f} line {n} is not `path<TAB>{'|'.join(ORIGINS)}<TAB>sha256`; reinstall",
                     "python3 harness-loop/install_harness.py --commit HEAD --target <project> --force")
        rows[parts[0]] = (parts[1], parts[2])
    return rows


def addon_dir(root):
    """HARNESS_VERSION's addon, or `.claude/harness-addon` when it is null, missing or unreadable."""
    try:
        addon = json.loads((root / ".claude/HARNESS_VERSION").read_text()).get("addon")
    except (OSError, ValueError, AttributeError):
        addon = None
    return addon or ".claude/harness-addon"


def drift(root, user_dir=None, rows=None):
    """The drift report for an install rooted at root (see the module doc); rows = read_tsv(root) when given."""
    rows, rules = read_tsv(root) if rows is None else rows, load_owners(root)
    out = {"addon_edited": [], "base_edited": [], "missing": [], "owned": {}, "shadow": [], "unexplained": [],
           "unowned": []}
    for path, (origin, want) in rows.items():
        f = root / path
        try:
            have = sha256(f) if f.is_file() else None
        except OSError:  # unreadable, or a link whose target went away between the check and the read
            have = None
        if have is None:
            out["missing"].append(path)
        elif have != want:
            out["base_edited" if origin == "base" else "addon_edited"].append(path)

    def entry(path):
        owner = owner_of(rules, path)
        if owner:
            out["owned"][owner] = out["owned"].get(owner, 0) + 1
        else:
            out["unowned"].append(path)

    prunes = [g[:-3] for g, _, _ in rules if g.endswith("/**")]
    for d, dirs, files in os.walk(root / ".claude"):
        rel = Path(d).relative_to(root).as_posix()
        for name in sorted(dirs):
            p = f"{rel}/{name}"
            if os.path.islink(os.path.join(d, name)):
                dirs.remove(name)
                entry(p)
            elif any(glob_match(g, p) for g in prunes):
                dirs.remove(name)
                entry(p)
        for name in files:
            p = f"{rel}/{name}"
            if p not in rows and p not in BOOKKEEPING:
                entry(p)
    if user_dir:
        u = Path(user_dir)
        skills = [f"skills/{s.parent.name}" for s in u.glob("skills/*/SKILL.md")
                  if (root / ".claude/skills" / s.parent.name / "SKILL.md").is_file()]
        commands = [f"commands/{c.name}" for c in u.glob("commands/*.md")
                    if (root / ".claude/commands" / c.name).is_file()]
        for s in skills + commands:
            out["shadow"].append(s)
            if not any(glob_match(g, s) for g, _, _ in rules):
                out["unexplained"].append(s)
    for k, v in out.items():
        if isinstance(v, list):
            v.sort()
    return out


def drift_cmd(a):
    if a.user_dir and not Path(a.user_dir).is_dir():
        core.die(Exit.REFUSED, f"--user-dir {a.user_dir} is not a directory", f"{DRIFT} --user-dir ~/.claude")
    root = drift_root(a.target)
    rows = read_tsv(root)
    out = drift(root, a.user_dir, rows)
    bad = any(out[k] for k in ("unowned", "addon_edited", "missing", "unexplained"))
    if a.json:
        print(json.dumps(out, sort_keys=True))
        return Exit.FAIL if bad else Exit.OK
    addon = addon_dir(root)
    merged = {p for p, (origin, _) in rows.items() if origin == "base+addon"}
    for key in ("base_edited", "addon_edited", "missing", "unowned", "shadow", "unexplained"):
        for path in out[key]:
            print(f"{key}: {path}")
            fix = (FIX.get(key) if key != "addon_edited" else MERGED_FIX if path in merged
                   else f"edit {addon}/{path.removeprefix('.claude/')} instead, then reinstall")
            if fix:
                print(f"  fix: {fix}")
    print("owned: " + (", ".join(f"{k} {v}" for k, v in sorted(out["owned"].items())) or "none"))
    print(f"drift: {'problems found' if bad else 'clean'}")
    return Exit.FAIL if bad else Exit.OK


def frontmatter_name(f, fallback):
    """The `name:` of a markdown file's leading `---` block (quotes stripped), else fallback (also for a file that
    cannot be read, e.g. a dangling symlink: the session would skip it the same way)."""
    try:
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return fallback
    if lines[:1] == ["---"]:
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"name:\s*(.+)$", line)
            if m:
                return m[1].strip().strip("\"'") or fallback
    return fallback


def expand(word, env):
    """word with $NAME/${NAME} from env and a leading ~ as $HOME; None when a variable is unknown."""
    if word.startswith("~"):
        word = "$HOME" + word[1:]
    unknown = []

    def sub(m):
        name = m[1] or m[2]
        if name not in env:
            unknown.append(name)
            return ""
        return env[name]
    out = re.sub(r"\$\{(\w+)\}|\$(\w+)", sub, word)
    return None if unknown else out


def resolve_hook(command, root):
    """(status, programs) for one hook command: see the module doc."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return "unresolved", []
    env = {"CLAUDE_PROJECT_DIR": str(root), "HOME": os.environ.get("HOME", "")}
    segments, cur = [], []
    for t in tokens + [";"]:
        if t in SEPARATORS:
            segments.append(cur)
            cur = []
        else:
            cur.append(t)
    programs, inert, unresolved = set(), False, False
    for seg in segments:
        seg = [t for t in seg if t not in ("(", ")", "{", "}", "!")]
        while seg and re.match(r"[A-Za-z_]\w*=", seg[0]):
            name, value = seg.pop(0).split("=", 1)
            value = expand(value, env)
            if value is None:
                env.pop(name, None)  # a later $name is then unknown, so unresolved
            else:
                env[name] = value
        if not seg:
            continue
        if seg[0] in ("[", "test") and len(seg) > 2 and seg[1] in GUARD_TESTS:
            path = expand(seg[2], env)
            programs.add((seg[2], path if path is not None and GUARD_TESTS[seg[1]](path) else None))
            unresolved |= path is None
            inert |= path is not None and not GUARD_TESTS[seg[1]](path)
            continue
        while seg and seg[0] in PREFIX_BUILTINS:  # the program is the word after exec/command and their options
            if seg[0] == "command" and seg[1:2] in (["-v"], ["-V"]):
                seg = []  # only asks whether a program exists
                break
            seg = seg[1:]
            while seg and seg[0].startswith("-"):
                seg = seg[2:] if seg[0] == "-a" else seg[1:]
            # leading redirections (`>f`, `2>/dev/null`): a bare `exec 2>/dev/null` runs nothing
            while seg and (REDIRECT.fullmatch(seg[0]) or seg[0].isdigit() and REDIRECT.fullmatch("".join(seg[1:2]))):
                seg = seg[2:] if REDIRECT.fullmatch(seg[0]) else seg[3:]
        if not seg or seg[0] in SKIP_WORDS:
            continue
        word = expand(seg[0], env)
        if word is None:
            found = None
        elif "/" in word:
            found = os.path.normpath(word) if os.path.isfile(word) and os.access(word, os.X_OK) else None
        else:
            found = shutil.which(word)
        programs.add((seg[0], found))
        unresolved |= found is None
    rows = [{"word": w, "path": os.path.normpath(p) if p else None} for w, p in sorted(programs, key=lambda x:
                                                                                         (x[0], x[1] or ""))]
    return ("inert" if inert else "unresolved" if unresolved else "ok"), rows


def inventory(root):
    """The inventory of the harness at <root>/.claude (see the module doc)."""
    c = root / ".claude"
    out = {
        "skills": sorted(frontmatter_name(f, f.parent.name) for f in c.glob("skills/*/SKILL.md")),
        "commands": sorted(":".join(f.relative_to(c / "commands").with_suffix("").parts)
                           for f in c.glob("commands/**/*.md")),
        "agents": sorted(frontmatter_name(f, f.stem) for f in c.glob("agents/*.md")),
        "instructions": {n: sha256(root / n) if (root / n).is_file() else None
                         for n in ("AGENTS.md", "CLAUDE.md", ".claude/CLAUDE.md")},
        "hooks": [],
    }
    settings = c / "settings.json"
    if not settings.is_file():
        return out
    try:
        groups = json.loads(settings.read_text()).get("hooks") or {}
        for event, gs in groups.items():
            for g in gs:
                for h in g.get("hooks", []):
                    if "command" in h:
                        status, programs = resolve_hook(h["command"], root)
                        out["hooks"].append({"event": event, "matcher": g.get("matcher"), "command": h["command"],
                                             "status": status, "programs": programs})
    except (ValueError, AttributeError, TypeError) as e:
        out["error"] = f"{settings} is not a valid settings.json: {e}"
        out["hooks"] = []
    out["hooks"].sort(key=lambda h: (h["event"], h["matcher"] or "", h["command"]))
    return out


def inventory_cmd(a):
    out = inventory(root_of(a.target, INVENTORY))
    bad = "error" in out or any(h["status"] != "ok" for h in out["hooks"])
    if a.json:
        print(json.dumps(out, sort_keys=True))
        return Exit.FAIL if bad else Exit.OK
    for key in ("skills", "commands", "agents"):
        print(f"{key}: {', '.join(out[key]) or 'none'}")
    for name, digest in sorted(out["instructions"].items()):
        print(f"instruction: {name} {digest or 'missing'}")
    for h in out["hooks"]:
        print(f"hook: {h['status']} {h['event']} {h['matcher'] or '-'} {h['command']}")
        for p in h["programs"]:
            if p["path"] is None:
                print(f"  not found: {p['word']}")
    if "error" in out:
        print(f"error: {out['error']}")
    return Exit.FAIL if bad else Exit.OK
