#!/usr/bin/env python3
"""Refuse bead ids in code comments.

Why: a bead id in a comment points at a local tracker the reader cannot open,
and the comment rots once the bead closes. The why belongs in the comment, the
id belongs in the commit message, PR or bead.

Rules come from <repo>/.claude/project-rules.md, section `## beads`, where
<repo> is the git top level of the current directory; when that file does not exist,
from $CLAUDE_PROJECT_DIR/.claude/project-rules.md (the session's harness dir) instead:
  bead-id-prefix:  the tracker's id prefix, e.g. proj-  (no line = nothing to check)
  bead-id-allow:   comma list of words after the prefix that are not ids (tool names)
  bead-id-exclude: one path regex of files to skip
                   (default: (^|/)(__snapshots__|fixtures)/|\\.(snap|md)$)

What counts as an id (inside a comment only; ids are lowercase, keywords any case):
  - full form: the prefix, then 3-5 chars, optional .N children, except allowed words
  - short child: 3-5 chars with at least one letter, then .N (e.g. ab1c.2),
    except versions (v1.2, node20.1, ...) and property paths (items.2.qty)
  - "bead .N" / "epic .N"
  - "bead <id>" / "epic <id>" where the id holds a letter and a digit
A keyword followed by plain letters ("bead would") is English, never a hit.
Lint and type directive lines are skipped.

Modes:
  --staged          lines added in the index (pre-commit). Fails open: any
                    error exits 0, a violation exits 9.
  --base <ref>      lines added since the merge base with <ref> (CI). Fails closed: an
                    unknown ref, a git error or unreadable rules exit non-zero.
  --all [paths...]  every comment line in tracked files.
Files that cannot be read as UTF-8 are skipped in every mode.

No --json: the hook and CI branch on the exit code, and stdout is already one record per line,
`path:line: id | comment`, with paths from the repo root, for editors and grep.

Exit codes:
  0  clean, or nothing to check (no bead-id-prefix)
  2  usage error, unresolvable base ref, no tracked file matching --all paths,
     or unreadable rules (--base/--all)
  9  a bead id was found in a comment

Examples:
  check_comment_bead_ids.py --staged
  check_comment_bead_ids.py --base origin/main
  check_comment_bead_ids.py --all src scripts
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.dont_write_bytecode = True  # agent_cli lives in the repo being committed to; leave no __pycache__ there
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
try:
    from agent_cli.core import names, rules_line
except ImportError:
    rules_line = names = None

NAME = "check_comment_bead_ids"
VIOLATION = 9
DEFAULT_EXCLUDE = r"(^|/)(__snapshots__|fixtures)/|\.(snap|md)$"

DIRECTIVE = re.compile(
    r"eslint-disable|@ts-expect-error|@ts-ignore|biome-ignore|noqa|istanbul ignore"
)
SHORT = re.compile(r"(?<![\w./@-])([a-z0-9]{3,5})(?:\.\d+)+(?!\w|\.\w)")
KW_REL = re.compile(r"\b(?:bead|epic)s?\s+\.\d+\b", re.I)
KW_ID = re.compile(
    r"\b(?:bead|epic)s?\s+(?=[a-z0-9]{0,4}\d)(?=[a-z0-9]{0,4}[a-z])[a-z0-9]{3,5}\b", re.I
)
VERSION = re.compile(r"^(?:v|node|python|es|utf|web|go|php|ruby|java|perl|rust|lua|tsc)\d+$")
PLURALS = {"nodes", "items", "rows", "args"}

# (line markers, block delimiters, quotes, quotes that span lines, "#" needs whitespace before)
C_LIKE = (("//",), ("/*", "*/"), ("`", '"', "'"), ("`",), False)
CSS = ((), ("/*", "*/"), ('"', "'"), (), False)
PY = (("#",), None, ('"""', "'''", '"', "'"), ('"""', "'''"), False)
SHELL = (("#",), None, ('"', "'"), (), True)
CONFIG = (("#",), None, (), (), True)
SQL = (("--",), None, ("'",), (), False)
SYNTAX = {
    **dict.fromkeys(("ts", "tsx", "js", "jsx", "mjs", "cjs", "mts", "cts", "scss"), C_LIKE),
    "css": CSS,
    "py": PY,
    **dict.fromkeys(("sh", "bash"), SHELL),
    **dict.fromkeys(("yml", "yaml", "toml"), CONFIG),
    "sql": SQL,
}


class Rules(NamedTuple):
    full: re.Pattern
    exclude: re.Pattern


def make_rules(prefix: str, allow=(), exclude: str = DEFAULT_EXCLUDE) -> Rules:
    """Compile the per-project patterns; raises re.error when exclude does not compile."""
    skip = rf"(?!(?:{'|'.join(map(re.escape, allow))})\b)" if allow else ""
    full = re.compile(rf"\b{re.escape(prefix)}{skip}[a-z0-9]{{3,5}}(?:\.\d+)*\b")
    return Rules(full, re.compile(exclude))


def load_rules(repo) -> Rules | None:
    """Rules from <repo>/.claude/project-rules.md ## beads; None when no prefix is set.

    Raises ValueError when the rules cannot be read: the parser is missing or the exclude is bad.
    """
    f = Path(repo) / ".claude/project-rules.md"
    text = f.read_text(encoding="utf-8") if f.is_file() else ""
    if rules_line is None:
        raise ValueError(f"cannot import agent_cli from {Path(__file__).resolve().parents[2] / 'cli'}")
    prefix = rules_line("beads", "bead-id-prefix", text)
    if not prefix:
        return None
    allow = names(rules_line("beads", "bead-id-allow", text) or "")
    exclude = rules_line("beads", "bead-id-exclude", text) or DEFAULT_EXCLUDE
    try:
        return make_rules(prefix, allow, exclude)
    except re.error as e:
        raise ValueError(f"bead-id-exclude {exclude!r} is not a valid regex ({e})") from e


def syntax_for(path: str, rules: Rules):
    if rules.exclude.search(path):
        return None
    if path.startswith(".githooks/"):
        return SHELL
    return SYNTAX.get(path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else "")


def comment_lines(path: str, text: str, rules: Rules) -> dict[int, str]:
    """Map line number -> the comment text on that line, for one file."""
    syntax = syntax_for(path, rules)
    if syntax is None:
        return {}
    markers, block, quotes, multiline, ws_before = syntax
    out: dict[int, str] = {}
    state = None
    for n, line in enumerate(text.split("\n"), 1):
        if state not in (None, "block") and state not in multiline:
            state = None
        parts: list[str] = []
        i = 0
        while i < len(line):
            if state == "block":
                end = line.find(block[1], i)
                parts.append(line[i:] if end < 0 else line[i:end])
                if end < 0:
                    break
                state, i = None, end + len(block[1])
            elif state:
                if line[i] == "\\":
                    i += 2
                elif line.startswith(state, i):
                    i, state = i + len(state), None
                else:
                    i += 1
            elif block and line.startswith(block[0], i):
                state, i = "block", i + len(block[0])
            elif any(line.startswith(m, i) for m in markers) and not (
                ws_before and i > 0 and not line[i - 1].isspace()
            ):
                parts.append(line[i:])
                break
            else:
                q = next((q for q in quotes if line.startswith(q, i)), None)
                state, i = (q, i + len(q)) if q else (state, i + 1)
        comment = " ".join(parts).strip()
        if comment:
            out[n] = comment
    return out


def ids_in(comment: str, rules: Rules) -> list[str]:
    if DIRECTIVE.search(comment):
        return []
    matches = [m for rx in (rules.full, KW_REL, KW_ID) for m in rx.finditer(comment)]
    for m in SHORT.finditer(comment):
        base = m.group(1)
        if re.search(r"[a-z]", base) and not VERSION.match(base) and base not in PLURALS:
            matches.append(m)
    found, taken = [], []
    for m in matches:
        if not any(m.start() < end and start < m.end() for start, end in taken):
            taken.append(m.span())
            found.append(m.group())
    return found


def find_hits(path: str, text: str, rules: Rules, only: set[int] | None = None):
    """(line, id, comment) for every id in a comment line, optionally only on `only` lines."""
    return [
        (n, hit, c)
        for n, c in comment_lines(path, text, rules).items()
        if only is None or n in only
        for hit in ids_in(c, rules)
    ]


def git(*args: str, errors: str = "strict") -> str:
    return subprocess.run(["git", *args], capture_output=True, encoding="utf-8", errors=errors,
                          check=True).stdout


HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def header_path(line: str) -> str | None:
    """The path in a `+++ b/<path>` diff header; git C-quotes odd names and tab-ends spaced ones."""
    p = line[4:].rstrip("\t")
    if p.startswith('"'):
        p = codecs.decode(p[1:-1], "unicode_escape").encode("latin-1").decode("utf-8")
    return p[2:] if p.startswith("b/") else None


def added_lines(diff_args: list[str]) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    path = None
    # added text in a non-UTF-8 file must not abort the whole diff; that file is skipped on read
    # pin prefixes and root-relative paths: diff.noprefix/mnemonicPrefix/relative would change the headers
    for line in git("diff", "-U0", "--no-color", "--no-ext-diff", "--diff-filter=ACMR",
                    "--src-prefix=a/", "--dst-prefix=b/", "--no-relative",
                    *diff_args, errors="replace").splitlines():
        if line.startswith("+++ "):
            path = header_path(line)
        elif path and (m := HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(path, set()).update(range(start, start + count))
    return out


def scan(files: dict[str, set[int] | None], read, rules: Rules,
         strict: bool = False) -> list[tuple[str, int, str, str]]:
    """Hits in each file; strict re-raises a git error reading a file instead of skipping it."""
    hits = []
    for path, only in sorted(files.items()):
        if syntax_for(path, rules) is None:
            continue
        try:
            text = read(path)
        except subprocess.CalledProcessError:
            if strict:
                raise
            continue
        except (OSError, UnicodeDecodeError):
            continue
        hits += [(path, n, hit, c) for n, hit, c in find_hits(path, text, rules, only)]
    return hits


def report(hits) -> int:
    for path, n, hit, c in hits:
        print(f"{path}:{n}: {hit} | {c[:120]}")
    if hits:
        print(f"\n{len(hits)} bead id(s) in comments. Say why in the comment instead; put the "
              "id in the commit message or PR.", file=sys.stderr)
        return VIOLATION
    return 0


def run_mode(mode: str, argv: list[str], rules: Rules, top: str) -> int:
    if mode == "--staged":
        files = added_lines(["--cached"])
        return report(scan(files, lambda p: git("show", f":{p}"), rules))
    if mode == "--base":
        ref = argv[1]
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                          capture_output=True).returncode:
            print(f"{NAME}: cannot resolve base ref {ref!r}\n"
                  f"  e.g. {NAME}.py --base origin/main", file=sys.stderr)
            return 2
        files = added_lines([f"{ref}...HEAD"])
        return report(scan(files, lambda p: git("show", f"HEAD:{p}"), rules, strict=True))
    paths = git("ls-files", "-z", "--full-name", "--", *argv[1:]).split("\0")
    files = {p: None for p in paths if p}
    if argv[1:] and not files:
        print(f"{NAME}: no tracked files match {' '.join(argv[1:])}", file=sys.stderr)
        return 2
    return report(scan(files, lambda p: (Path(top) / p).read_text(encoding="utf-8"), rules))


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    if mode in ("-h", "--help"):
        print(__doc__)
        return 0
    if not (mode == "--staged" and len(argv) == 1 or mode == "--base" and len(argv) == 2
            or mode == "--all"):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        top = git("rev-parse", "--show-toplevel").strip()
        own = (Path(top) / ".claude/project-rules.md").is_file()
        rules = load_rules(top if own else os.environ.get("CLAUDE_PROJECT_DIR") or top)
        if rules is None:
            print(f"{NAME}: no bead-id-prefix in .claude/project-rules.md, nothing to check",
                  file=sys.stderr)
            return 0
        return run_mode(mode, argv, rules, top)
    except Exception as e:
        if mode != "--staged":
            print(f"{NAME}: {e}", file=sys.stderr)
            return 2
        # fail open: a broken guard must not block commits
        print(f"{NAME}: skipped ({e})", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
