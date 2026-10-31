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
# Names of lookups, not secrets, as a whole word before _KEY. A word that can hold a salt or an encryption key
# (HASH, CACHE, GROUP, UNIQUE, ORDER, IDEMPOTENCY) is not listed: a missed lookup costs a word, a missed secret leaks.
_LOOKUP_KEYS = ("PRIMARY", "FOREIGN", "SORT", "PARTITION", "SHARD", "INDEX", "LOOKUP", "PUBLIC", "OBJECT")
# Named secret shapes only: never a bare hex or base64 run (TOKENS' patterns are not used), so git shas and
# path@sha lines survive. Each is (regex, replacement), applied in order; every bound stops at a quote or
# backslash so a replaced span never eats the escape of a JSON string.
REDACT = (
    # a whole PEM private key; a truncated one (no END) loses its body up to the first space or other non-base64 char
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:(?:(?!-----BEGIN)(?:[^\"\\]|\\.)){0,8192}?-----END [A-Z ]*PRIVATE KEY-----"
                r"|(?:[A-Za-z0-9+/=]|\r?\n|\\+[nrt]){0,8192})", re.S), REDACTED),
    # an AWS secret key (40 base64 chars with upper and lower case) within 80 chars after a key id, same line
    (re.compile(rf"(AKIA[A-Z0-9]{{16}}\b.{{0,80}}?)(?<![{_B64}])(?=[{_B64}]*[A-Z])(?=[{_B64}]*[a-z])"
                rf"[{_B64}]{{40}}(?![{_B64}])"), rf"\1{REDACTED}"),
    (re.compile(r"AKIA[A-Z0-9]{16}\b"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                r"|xox[abp]-[A-Za-z0-9-]{10,}|ntn_[A-Za-z0-9]{20,}"), REDACTED),
    # "Bearer <ENV_VAR_NAME>" is a name, not a token
    (re.compile(r"(Bearer )(?![A-Z0-9_]+(?![A-Za-z0-9._-]))[A-Za-z0-9._-]{20,}"), rf"\1{REDACTED}"),
    # the whole assignment of a credential-named variable goes (DB_API_KEY=..., GH_TOKEN="..."): a value left
    # behind as NAME=<anything> would still read as a secret
    (re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*=(?:\\?[\"'])?[^\s\"'\\]+"), REDACTED),
    # NAME_KEY=value for any other uppercase name ending _KEY (ACME_IO_KEY=...). A quoted value goes whole, spaces
    # included. Left alone: whole-word lookup names (PRIMARY_KEY, DB_SORT_KEY; a SUBSORT_KEY is not one) and
    # placeholders ($VAR, ${VAR}, $(cmd), <value>). Not covered, like the rule above: NAME_KEY: value, NAME_KEY = value
    # and "NAME_KEY": "value" (JSON or YAML)
    (re.compile(r"\b[A-Z][A-Z0-9_]*" + "".join(rf"(?<!\b{w})(?<!_{w})" for w in _LOOKUP_KEYS) + r"_KEY="
                r"(?:\\\"(?![${<*])[^\"\\\n]*\\\"|\"(?![${<*])[^\"\n]*\"|'(?![${<*])[^'\n]*'|(?:\\?[\"'])?(?![${<*])[^\s\"'\\]+)"),
     REDACTED),
    # a JWT: three base64url segments, the first the encoded header (eyJ...), each long enough that a dotted
    # name or version string never matches
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{7,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), REDACTED),
    # keep scheme, user and host; a ${PASS}-style placeholder has no secret. <REDACTED>, not [REDACTED]:
    # a password starting with [ still reads as one to the desk's secret check
    (re.compile(r"(postgres(?:ql)?://[^:\s\"'\\/@]+:)(?![${<*])[^@\s\"'\\]+(?=@)"), r"\1<REDACTED>"),
)


def redact(text):
    """(text with named credentials replaced by [REDACTED], how many substitutions were made).

    The count is substitutions, so an AKIA id and its secret count 2. Matching follows the desk's check: a lowercase
    "bearer" or an all-caps token after Bearer is left alone, and a credential-named NAME= value ends at whitespace
    (a quoted NAME_KEY= value, at its closing quote). JWTs go too. Bare hex and base64 runs survive on purpose, so
    token() can still flag a redacted file. The desk's secret check is a subset of these shapes, kept as a copy
    in test_redact.py, so add a shape here first."""
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
