"""`agent-cli card`: check and write to-do cards, one JSON object per <dir>/*.json, against a rules file.

A card waits on a reader to decide something. The defaults: a card whose status is "needs-you" fills every
decision field (title to detail); prose fields carry no tracker ids, PR numbers, emails or token-shaped
strings; no field has an em dash. A rules file adds per-field shapes (enum, pattern, word range, date, path,
references to other cards, a colour palette for a figure file), the id-equals-file-name rule, fields
`card write` stamps with today's date, and a cleared log whose ids may not come back. Stdlib only.
"""
import datetime
import json
import os
import re
import sys
from pathlib import Path

from . import core, rules as rulesmod
from .core import Exit
from .page import write_atomic
from .textrules import EM_DASH, EMAIL, PR, bead_ids, forbidden, token

DEFAULTS = {
    "id_field": "id",
    "id_is_filename": False,
    "id_pattern": None,
    "required": [],
    "required_when": {"field": "status", "equals": "needs-you",
                      "fields": ["title", "background", "why", "choice", "recommendation", "detail"]},
    "fields": {},          # {name: {type, enum, pattern, words: [min, max], date, path, ext, base, refs, palette}}
    "prose": ["title", "background", "why", "choice", "recommendation"],
    "prefixes": [],
    "no_tracker_ids": True,
    "no_pr_numbers": True,
    "no_tokens": True,
    "no_emails": False,
    "forbid_text": [{"pattern": EM_DASH, "why": "em dash"}],
    "cleared_log": None,   # {"path", "pattern": regex with groups (timestamp, id)}
    "reopen_field": "reopened",
    "stamp_today": [],     # fields `card write` sets to today's YYYY-MM-DD
}
TYPES = {"str": str, "int": int, "list": list, "bool": bool}
CHECK = "agent-cli card check .claude/cards --rules .claude/card-rules.json"
WRITE = "agent-cli card write .claude/cards --rules .claude/card-rules.json --file /tmp/cache-size.json"


def register(sub):
    _, cs = core.group(sub, "card", "to-do cards (one JSON per decision): format check and a checked write",
                       [CHECK, WRITE])
    p = core.subcommand(cs, "check", "print each violation in every <dir>/*.json card (exit 1) or PASS (exit 0)",
                        [CHECK, "agent-cli card check .claude/cards --prefix acme"])
    p.add_argument("dir", help="folder of *.json cards")
    _common(p)
    p.set_defaults(func=check_cmd)
    p = core.subcommand(cs, "write", "check one card against the rules and the folder (refs, cleared ids), then "
                        "write <dir>/<id>.json atomically; nothing is written on a violation", [WRITE])
    p.add_argument("dir", help="folder of *.json cards")
    p.add_argument("--file", required=True, help="the card JSON, or - for stdin")
    _common(p)
    p.set_defaults(func=write_cmd)


def _common(p):
    p.add_argument("--rules", help="rules JSON (default: the card-rules.json seam, else built-in defaults)")
    p.add_argument("--prefix", action="append", default=[], help="tracker id prefix to flag even when letter-only")
    p.add_argument("--json", action="store_true", help="one JSON object")


def load_rules(path=None, example=CHECK):
    return rulesmod.load("card", DEFAULTS, path, example)


def cleared_ids(r, base):
    """{id: date of its last clear} from the rules' cleared log ({} when there is none)."""
    spec = r["cleared_log"]
    if not spec:
        return {}
    path = rulesmod.resolve(spec["path"], base)
    out = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    rx = re.compile(spec.get("pattern") or r"^(\S+)\s+(\S+)\s")
    for ln in lines:
        m = rx.match(ln)
        if m:
            out[m.group(2)] = m.group(1)[:10]
    return out


def is_date(v):
    try:
        datetime.date.fromisoformat(v)
        return bool(re.fullmatch(r"\d{4}-\d\d-\d\d", v))
    except (TypeError, ValueError):
        return False


def check_field(name, v, spec, card, others, base):
    """Violations (without the file prefix) for one field value against its spec."""
    out = []
    want = TYPES.get(spec.get("type", "str"))
    if want and (not isinstance(v, want) or (want is int and isinstance(v, bool))):
        return [f"{name}: must be a {spec.get('type', 'str')}"]
    if "enum" in spec and v not in spec["enum"]:
        out.append(f"{name}: {v!r} is not one of {', '.join(map(str, spec['enum']))}")
    if isinstance(v, str):
        if spec.get("pattern") and not re.search(spec["pattern"], v):
            out.append(f"{name}: does not match {spec['pattern']!r}")
        if "words" in spec:
            lo, hi = spec["words"]
            n = len(v.split())
            if not lo <= n <= hi:
                out.append(f"{name}: {n} words, want {lo} to {hi}")
        if spec.get("date") and not is_date(v):
            out.append(f"{name}: {v!r} is not a YYYY-MM-DD date")
        if spec.get("path"):
            raw = v.split(" (")[0]  # a trailing " (note)" is allowed
            p = rulesmod.resolve(raw, rulesmod.resolve(spec["base"], base) if spec.get("base") else None)
            if p is None:
                out.append(f"{name}: {raw!r} must be an absolute path")
            elif spec["path"] == "file" and not os.path.isfile(p) or spec["path"] == "dir" and not os.path.isdir(p) \
                    or spec["path"] == "any" and not os.path.exists(p):
                out.append(f"{name}: {raw!r} does not exist (want a {spec['path']})")
            elif spec.get("ext") and not any(p.endswith(e) for e in spec["ext"]):
                out.append(f"{name}: {raw!r} must end in {' or '.join(spec['ext'])}")
            elif spec.get("palette") and os.path.isfile(p):
                allowed = {c.lower() for c in spec["palette"]}
                text = Path(p).read_text(encoding="utf-8", errors="replace")
                extra = sorted({c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", text)} - allowed)
                if extra:
                    out.append(f"{name}: colours outside the palette: {', '.join(extra[:5])}")
    if spec.get("refs"):
        same = spec["refs"].split(":", 1)[1] if spec["refs"].startswith("same:") else None
        for ref in v if isinstance(v, list) else [v]:
            other = others.get(ref)
            if other is None:
                out.append(f"{name}: {ref!r} is not a card in this folder")
            elif same and other.get(same) != card.get(same):
                out.append(f"{name}: {ref!r} has a different {same}")
    return out


def check_card(fname, card, r, others, cleared, base, prefixes=()):
    """Violations for one card; others = {id: card} of the folder (this one included)."""
    if not isinstance(card, dict):
        return [f"{fname}: card is not a JSON object"]
    out = []
    cid = card.get(r["id_field"])
    if r["id_is_filename"] and cid != Path(fname).stem:
        out.append(f"{fname}: {r['id_field']}: {cid!r} must equal the file name {Path(fname).stem!r}")
    if r["id_pattern"] and not (isinstance(cid, str) and re.fullmatch(r["id_pattern"], cid)):
        out.append(f"{fname}: {r['id_field']}: {cid!r} does not match {r['id_pattern']!r}")
    need = list(r["required"])
    rw = r["required_when"]
    if rw and card.get(rw["field"]) == rw["equals"]:
        need += [f for f in rw["fields"] if f not in need]
    for field in need:
        v = card.get(field)
        if v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, dict)) and not v):
            why = f"{rw['equals']} card is missing this field" if rw and field in rw["fields"] else "missing"
            out.append(f"{fname}: {field}: {why}")
    for field, spec in r["fields"].items():
        if field in card and card[field] not in (None, ""):
            out += [f"{fname}: {x}" for x in check_field(field, card[field], spec, card, others, base)]
    if isinstance(cid, str) and cid in cleared:
        reopened = card.get(r["reopen_field"])
        if not (isinstance(reopened, str) and is_date(reopened) and reopened >= cleared[cid]):
            out.append(f"{fname}: {r['id_field']}: {cid!r} was cleared on {cleared[cid]}; a cleared card stays gone "
                       f"unless the reader reopens it (then set \"{r['reopen_field']}\": \"YYYY-MM-DD\")")
    prefixes = tuple(r["prefixes"]) + tuple(prefixes)
    for field, v in card.items():
        if not isinstance(v, str):
            continue
        for why, _ in forbidden(v, r["forbid_text"]):
            out.append(f"{fname}: {field}: {why}")
        if field in r["prose"]:
            if r["no_tracker_ids"]:
                bead = next(bead_ids(v, prefixes), None)
                if bead:
                    out.append(f"{fname}: {field}: bead id '{bead}' in prose, name the work in words")
            if r["no_pr_numbers"]:
                m = PR.search(v)
                if m:
                    out.append(f"{fname}: {field}: PR number '{m.group()}' in prose, say what it changes")
        if r["no_emails"] and EMAIL.search(v):
            out.append(f"{fname}: {field}: email address, leave it out")
        if r["no_tokens"]:
            t = token(v)
            if t:
                out.append(f"{fname}: {field}: token-shaped string '{t[:4]}...'")
    return out


def load_folder(folder):
    """[(file name, card or None, error or None)] for every *.json in folder, sorted."""
    res = []
    for f in sorted(Path(folder).glob("*.json")):
        try:
            res.append((f.name, json.loads(f.read_text(encoding="utf-8")), None))
        except (OSError, ValueError) as e:
            res.append((f.name, None, f"{f.name}: not readable JSON ({e})"))
    return res


def check_cards(folder, prefixes=(), rules=None, base=None):
    """Every violation in folder's cards."""
    r = rules or load_rules()[0]
    loaded = load_folder(folder)
    others = {c.get(r["id_field"]): c for _, c, _ in loaded if isinstance(c, dict)}
    cleared = cleared_ids(r, base)
    out = []
    for name, card, err in loaded:
        out += [err] if err else check_card(name, card, r, others, cleared, base, prefixes)
    return out


def check_cmd(a):
    r, src = load_rules(a.rules)
    if not Path(a.dir).is_dir():
        core.die(Exit.REFUSED, f"no such folder: {a.dir}", CHECK)
    if not any(Path(a.dir).glob("*.json")):
        core.die(Exit.REFUSED, f"no cards found: {a.dir} has no *.json files", CHECK)
    out = check_cards(a.dir, a.prefix, r, src.parent if src else None)
    if a.json:
        core.emit({"pass": not out, "violations": out}, True)
    else:
        print("\n".join(out) if out else "PASS")
    return Exit.FAIL if out else Exit.OK


def write_cmd(a):
    r, src = load_rules(a.rules, WRITE)
    base = src.parent if src else None
    folder = Path(a.dir)
    if not folder.is_dir():
        core.die(Exit.REFUSED, f"no such folder: {a.dir}", WRITE)
    try:
        text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
        card = json.loads(text)
    except (OSError, ValueError) as e:
        core.die(Exit.REFUSED, f"cannot read the card: {e}", WRITE)
    if not isinstance(card, dict):
        core.die(Exit.REFUSED, "the card is not a JSON object", WRITE)
    cid = card.get(r["id_field"])
    if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", cid):
        core.die(Exit.REFUSED, f"{r['id_field']} {cid!r} is not usable as a file name", WRITE)
    today = datetime.date.today().isoformat()
    for f in r["stamp_today"]:
        card[f] = today
    name = f"{cid}.json"
    others = {c.get(r["id_field"]): c for _, c, _ in load_folder(folder) if isinstance(c, dict)}
    others[cid] = card
    out = check_card(name, card, r, others, cleared_ids(r, base), base, a.prefix)
    if out:
        if a.json:
            core.emit({"written": False, "violations": out}, True)
        else:
            print("\n".join(out))
        return Exit.FAIL
    dest = folder / name
    write_atomic(dest, json.dumps(card, indent=2, ensure_ascii=False) + "\n")
    core.out(a, {"written": True, "path": str(dest)}, f"written: {dest}")
    return Exit.OK
