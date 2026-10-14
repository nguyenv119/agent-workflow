"""Text shapes that page and card checks forbid: tracker ids, PR numbers, token-shaped strings, emails.

Shared by `agent-cli page` and `agent-cli card`; stdlib only.
"""
import re

EM_DASH = "—"
WORD = re.compile(r"[A-Za-z0-9]+(?:[-.][A-Za-z0-9]+)*")
ID = re.compile(r"[a-z0-9]{3,5}")
# Hyphen-only ids need a letter+digit mix that is not a common word shape: units and decades
# (10k, 2fa, 90s, 1990s) or numeronyms (e2e, k8s). Letter-only ids need .N or a known prefix.
NOT_ID = re.compile(r"\d+[a-z]+|[a-z]\d+[a-z]+")
VERSION = re.compile(r"[a-z]+\d+")  # web3.0, python3.12: a name and a version, not an id
PR = re.compile(r"(?<![&\w])#\d+\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
TOKENS = (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
          re.compile(r"\bAKIA[A-Z0-9]{16}\b"), re.compile(r"\b[0-9a-fA-F]{32,}\b"),
          # base64-ish: 32+ chars mixing upper, lower and digit (a slug or path has no such run)
          # (- and _ split the run, so a dated Mixed-Case-File-2026-10-13.html is not one)
          re.compile(r"(?<![A-Za-z0-9+=])(?=[A-Za-z0-9+=]*[A-Z])(?=[A-Za-z0-9+=]*[a-z])"
                     r"(?=[A-Za-z0-9+=]*\d)[A-Za-z0-9+=]{32,}"))


def bead_ids(text, prefixes=()):
    """Yield tracker-id-shaped strings: prefix-ID, prefix-ID.N, or a bare 4-5 char ID.N."""
    for m in WORD.finditer(text):
        parts = m.group().split("-")
        for i, part in enumerate(parts):
            base, *rest = part.split(".")
            if len(rest) > 1 or (rest and not rest[0].isdigit()) or not ID.fullmatch(base):
                continue
            dotted, letter = bool(rest), any(c.isalpha() for c in base)
            if i == 0:
                hit = dotted and len(base) >= 4 and letter and not VERSION.fullmatch(base)
            else:
                hit = ("-".join(parts[:i]) in prefixes
                       or letter and (dotted or any(c.isdigit() for c in base) and not NOT_ID.fullmatch(base)))
            if hit:
                yield "-".join(parts[:i + 1])


def token(text):
    """The first token-shaped string in text, or None."""
    for rx in TOKENS:
        m = rx.search(text)
        if m:
            return m.group()
    return None


def forbidden(text, rules):
    """(why, matched text) for each {"pattern", "why"} rule whose regex matches text."""
    out = []
    for r in rules:
        m = re.search(r["pattern"], text)
        if m:
            out.append((r.get("why", f"matches {r['pattern']!r}"), m.group()))
    return out
