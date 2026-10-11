#!/usr/bin/env python3
"""Delete old try transcripts under TRIES_ROOT (no AI; rules from decision D7).

Only transcript.jsonl files are ever deleted. note.md, diff.patch, the shas,
gates.txt and index.tsv rows stay forever.

1. Delete transcripts of passed tries older than 90 days and failed tries older
   than 365 days. A try's result comes from its repo's index.tsv; a folder with
   no row counts as failed (kept longer).
2. Skip any try folder holding a PIN file.
3. Then, if TRIES_ROOT is over the cap (default 10 GB), delete the oldest
   unpinned transcripts until it is under.

Every deletion is logged to <TRIES_ROOT>/cut.log. --dry-run prints and deletes
nothing. TRIES_ROOT = $HARNESS_TRIES_DIR, default ~/.claude/tries.
"""
import argparse
import datetime
import os
import time
from pathlib import Path

DAY = 86400
KEEP_DAYS = {"pass": 90, "fail": 365}

# ponytail: ages count from the try date (note.md's mtime, written once at save),
# not the epic's close date as D7 said; epics last days, so the error is small.
# Switch to bead close dates via `bd show --json` if that matters.


def total_bytes(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cap-bytes", type=int, default=10 * 1024**3)
    a = ap.parse_args()
    root = Path(os.environ.get("HARNESS_TRIES_DIR", Path.home() / ".claude/tries"))
    if not root.is_dir():
        return

    results = {}
    for index in root.glob("*/index.tsv"):
        for line in index.read_text().splitlines():
            cols = line.split("\t")
            if len(cols) >= 7:
                results[index.parent / cols[6]] = cols[3]

    tries = []  # (date, folder, result) for unpinned tries that still have a transcript
    for tx in root.glob("*/*/try-*/transcript.jsonl"):
        folder = tx.parent
        if (folder / "PIN").exists():
            continue
        date = (folder / "note.md").stat().st_mtime if (folder / "note.md").exists() else tx.stat().st_mtime
        tries.append((date, folder, results.get(folder, "fail")))
    tries.sort(key=lambda t: t[0])

    now = time.time()
    log = []
    gone = set()

    def delete(folder, reason):
        tx = folder / "transcript.jsonl"
        size = tx.stat().st_size
        print(("would delete " if a.dry_run else "deleted ") + f"{tx.relative_to(root)} ({reason})")
        if not a.dry_run:
            tx.unlink()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.append(f"{stamp}\t{tx.relative_to(root)}\t{reason}\t{size}\n")
        gone.add(folder)
        return size

    for date, folder, result in tries:
        days = (now - date) / DAY
        if days > KEEP_DAYS.get(result, KEEP_DAYS["fail"]):
            delete(folder, f"age {days:.0f}d > {KEEP_DAYS.get(result, 365)}d ({result})")

    size = total_bytes(root)
    if a.dry_run:  # nothing was really deleted; subtract what would have been
        size -= sum(int(l.rsplit("\t", 1)[1]) for l in log)
    for date, folder, _ in tries:
        if size <= a.cap_bytes:
            break
        if folder not in gone:
            size -= delete(folder, f"cap {a.cap_bytes} bytes")

    if log and not a.dry_run:
        with open(root / "cut.log", "a") as fh:
            fh.writelines(log)


if __name__ == "__main__":
    main()
