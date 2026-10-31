"""`agent-cli redact`: blank named credentials in a file; redact_file() is the import for tools that save transcripts."""
import os
import tempfile
from pathlib import Path

from . import core
from .core import Exit
from .textrules import redact

EXAMPLES = ["agent-cli redact transcript.jsonl", "agent-cli redact notes.md --dry-run --json"]


def register(sub):
    p = core.subcommand(sub, "redact", "replace named credentials in a file with [REDACTED], in place; prints "
                        "only the count, never a value", EXAMPLES)
    p.add_argument("file", help="the file to clean; a .jsonl is cleaned line by line so every line stays valid JSON")
    p.add_argument("--dry-run", action="store_true", help="count what would be replaced, write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object")
    p.set_defaults(func=run)


def redact_file(path, write=True):
    """Redact path in place and return how many credentials were replaced; the file ends up mode 600.

    A .jsonl is split on newlines so no pattern spans two records. Bytes that are not UTF-8 round-trip, and a
    file with nothing to replace is left as it was (apart from chmod 600)."""
    path = Path(path)
    text = path.read_bytes().decode("utf-8", "surrogateescape")
    parts = text.split("\n") if path.suffix == ".jsonl" else [text]
    done = [redact(p) for p in parts]
    total = sum(n for _, n in done)
    if write:
        if total:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")  # mkstemp makes it 0600
            with os.fdopen(fd, "wb") as f:
                f.write("\n".join(t for t, _ in done).encode("utf-8", "surrogateescape"))
            os.replace(tmp, str(path))
        os.chmod(path, 0o600)
    return total


def run(a):
    if not os.path.isfile(a.file):
        core.die(Exit.REFUSED, f"{a.file} is not a file", EXAMPLES[0])
    n = redact_file(a.file, write=not a.dry_run)
    core.out(a, {"file": a.file, "redactions": n, "dry_run": a.dry_run}, f"redactions: {n}")
