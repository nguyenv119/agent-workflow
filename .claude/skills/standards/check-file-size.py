#!/usr/bin/env python3
"""List files this branch pushes from under 1,000 lines to 1,000 or more.

Usage: python3 .claude/skills/standards/check-file-size.py <base-ref>   (run inside the repo)

"Before" is the file at merge-base(<base-ref>, HEAD); "after" is the working tree, so uncommitted
and untracked files count. Choices:
- A new file has before = 0, so a file created at 1,000+ lines counts as crossing.
- A renamed file is compared with its old path, so moving a large file is not flagged.
- Deleted files are skipped.
Exit 1 and print "path: before -> after" per crossing file; exit 0 if none; exit 2 on a git or usage error or a non-UTF-8 path.
"""
import subprocess
import sys
from pathlib import Path

LIMIT = 1000


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def count(data):
    return len(data.splitlines())


def main(base):
    root = Path(git("rev-parse", "--show-toplevel").decode().strip())
    mb = git("merge-base", base, "HEAD").decode().strip()
    # -z output: status NUL path [NUL new-path for renames] ...; paths are relative to the repo root
    fields = git("-C", str(root), "diff", "--name-status", "-M", "-z", mb).decode().split("\0")
    pairs, i = [], 0
    while i < len(fields) - 1:
        status = fields[i]
        if status[0] == "R":
            pairs.append((fields[i + 1], fields[i + 2]))
            i += 3
        else:
            if status[0] != "D":
                pairs.append((fields[i + 1], fields[i + 1]))
            i += 2
    untracked = git("-C", str(root), "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
    pairs += [(p, p) for p in untracked if p]

    crossed = []
    for old, new in pairs:
        if not (root / new).is_file():  # submodule or other non-file entry
            continue
        try:
            before = count(git("show", f"{mb}:{old}"))
        except subprocess.CalledProcessError:
            before = 0
        after = count((root / new).read_bytes())
        if before < LIMIT <= after:
            crossed.append(f"{new}: {before} -> {after}")
    for line in crossed:
        print(line)
    return 1 if crossed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1]))
    except subprocess.CalledProcessError as e:
        print(f"git {' '.join(e.cmd[1:])} failed: {e.stderr.decode().strip()}", file=sys.stderr)
        sys.exit(2)
    except UnicodeDecodeError:
        print("a changed path is not valid UTF-8; check it by hand", file=sys.stderr)
        sys.exit(2)
