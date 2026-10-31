"""Text shapes that page and card checks forbid: tracker ids, PR numbers, token-shaped strings, emails.

Shared by `agent-cli page` and `agent-cli card`; stdlib only. redact() blanks named credential shapes
(used by `agent-cli redact` and anything that saves or pushes transcripts).
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


REDACTED = "[REDACTED]"
_B64 = "A-Za-z0-9/+"  # AWS secret keys: no =, so KEY=secret splits
# Named secret shapes only: never a bare hex or base64 run (TOKENS' patterns are not used), so git shas and
# path@sha lines survive. Each is (regex, replacement), applied in order; every bound stops at a quote or
# backslash so a replaced span never eats the escape of a JSON string.
REDACT = (
    # a whole PEM private key; a truncated one (no END) loses its body up to the first space or other non-base64 char
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:[^\"\\]|\\.)*?-----END [A-Z ]*PRIVATE KEY-----"
                r"|(?:[A-Za-z0-9+/=]|\r?\n|\\+[nrt])*)", re.S), REDACTED),
    # an AWS secret key (40 base64 chars with upper and lower case) within 80 chars after a key id, same line
    (re.compile(rf"(AKIA[A-Z0-9]{{16}}\b.{{0,80}}?)(?<![{_B64}])(?=[{_B64}]*[A-Z])(?=[{_B64}]*[a-z])"
                rf"[{_B64}]{{40}}(?![{_B64}])"), rf"\1{REDACTED}"),
    (re.compile(r"AKIA[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|xox[abp]-[A-Za-z0-9-]{10,}|ntn_[A-Za-z0-9]{20,}"), REDACTED),
    # "Bearer <ENV_VAR_NAME>" is a name, not a token
    (re.compile(r"(Bearer )(?![A-Z0-9_]+(?![A-Za-z0-9._-]))[A-Za-z0-9._-]{20,}"), rf"\1{REDACTED}"),
    # the whole assignment goes: a value left behind as NEON_X=<anything> would still read as a secret
    (re.compile(r"NEON_[A-Z_]*=(?:\\?[\"'])?[^\s\"'\\]+"), REDACTED),
    # keep scheme, user and host; a ${PASS}-style placeholder has no secret. <REDACTED>, not [REDACTED]:
    # a password starting with [ still reads as one to the desk's secret check
    (re.compile(r"(postgres(?:ql)?://[^:\s\"'\\/@]+:)(?![${<*])[^@\s\"'\\]+(?=@)"), r"\1<REDACTED>"),
)


def redact(text):
    """(text with named credentials replaced by [REDACTED], how many were replaced)."""
    total = 0
    for rx, repl in REDACT:
        text, n = rx.subn(repl, text)
        total += n
    return text, total


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
