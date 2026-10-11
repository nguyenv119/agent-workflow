#!/usr/bin/env python3
"""Install the agent-workflow harness exactly as it was at one commit.

    install_harness.py --commit <ref> --target <dir> [--addon <dir>] [--repo <path>] [--force]

Installs the committed `.claude/` (minus `.claude/loop-evals/`) and `AGENTS.md`
from <ref> via `git archive`, so a dirty worktree never leaks in. An optional
add-on dir is copied over `<target>/.claude/` (add-on files replace core files
at the same relative path). Then it writes:

  .claude/HARNESS_VERSION   {"commit": sha, "addon": abs path|null, "addon_sha256": hex|null}
  .claude/HARNESS_MANIFEST  every file this install wrote, one target-relative path per line

addon_sha256 = sha256 over the add-on's files sorted by posix relpath, each fed
as `relpath \\0 len(bytes) \\0 bytes`.

A previous manifest blocks a reinstall (exit 2) unless --force, which deletes
ONLY the files that manifest lists. Without a manifest, files are overlaid and
nothing is deleted. Before touching anything it refuses if any component of
a path it would write or delete (every directory down to and including the
file itself) is a symlink on disk, or lies under a symlink shipped in the
harness commit. Every refusal or failure exits 2 with a message.
"""
import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

VERSION = ".claude/HARNESS_VERSION"
MANIFEST = ".claude/HARNESS_MANIFEST"
EXCLUDED = ".claude/loop-evals"


def die(msg):
    print(f"install_harness: {msg}", file=sys.stderr)
    sys.exit(2)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        die(f"git {' '.join(args)} failed: {result.stderr.decode().strip()}")
    return result.stdout


def addon_files(addon):
    """Sorted (posix relpath, Path) of every file in the add-on tree."""
    files = [p for p in addon.rglob("*") if p.is_file()]
    return sorted((p.relative_to(addon).as_posix(), p) for p in files)


def addon_sha256(files):
    h = hashlib.sha256()
    for rel, p in files:
        data = p.read_bytes()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


def inside(path, root):
    return path == root or root in path.parents


def install(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--addon")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    sha = git(args.repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    target = Path(args.target).resolve()

    addon = files = None
    if args.addon:
        addon = Path(args.addon).resolve()
        if not addon.is_dir():
            die(f"add-on dir not found: {addon}")
        files = addon_files(addon)

    # Read the whole archive first: every guard runs before anything is written.
    tar = tarfile.open(fileobj=io.BytesIO(git(args.repo, "archive", sha, ".claude", "AGENTS.md")))
    members = [m for m in tar.getmembers()
               if not inside(Path(m.name), Path(EXCLUDED)) and not m.isdir()]
    planned = {m.name for m in members}
    planned |= {f".claude/{rel}" for rel, _ in files or []}
    planned |= {VERSION, MANIFEST}

    old_manifest = target / MANIFEST
    previous = []
    if old_manifest.exists():
        if not args.force:
            die(f"{old_manifest} exists from a previous install; rerun with --force to replace it")
        previous = [line for line in old_manifest.read_text().splitlines() if line]
    for rel in previous:
        if not inside((target / rel).resolve(), target):
            die(f"previous manifest lists a path outside the target: {rel}")
    # Writing or deleting through a symlink would change files the manifest
    # does not own (e.g. skills linked into a nested repo), so refuse outright.
    core_links = {m.name for m in members if m.issym() or m.islnk()}
    for rel in [*previous, *planned]:
        path = target
        for part in Path(rel).parts:
            path = path / part
            if path.is_symlink():
                die(f"refusing to write or delete through a symlink: {path}")
        if any(a.as_posix() in core_links for a in Path(rel).parents):
            die(f"refusing to write through a symlink in the harness itself: {rel}")
    if addon:
        for rel in [*previous, *planned]:
            if inside((target / rel).resolve(), addon):
                die(f"refusing to touch the add-on dir: {rel} resolves inside {addon}")

    # The old manifest is never deleted here; it is rewritten below.
    for rel in previous:
        path = target / rel
        if rel == MANIFEST:
            continue
        if path.is_file():
            path.unlink()
        parent = path.parent
        while parent != target and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    for rel in sorted(planned):
        if (target / rel).exists() and rel not in previous:
            print(f"replaced {rel}")

    # Track every path this run may write before writing any, so a failure
    # mid-install leaves nothing untracked; on success it shrinks to `planned`.
    (target / MANIFEST).parent.mkdir(parents=True, exist_ok=True)
    (target / MANIFEST).write_text("".join(f"{rel}\n" for rel in sorted(set(previous) | planned)))
    tar.extractall(target, members=members, filter="data")
    for rel, src in files or []:
        dest = target / ".claude" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)  # keeps the executable bit for add-on hooks

    version = {"commit": sha, "addon": str(addon) if addon else None,
               "addon_sha256": addon_sha256(files) if addon else None}
    (target / VERSION).write_text(json.dumps(version) + "\n")
    (target / MANIFEST).write_text("".join(f"{rel}\n" for rel in sorted(planned)))

    print(f"installed agent-workflow {sha} into {target / '.claude'} "
          f"({len(planned) - 2} files), addon: {addon or 'none'}")


def main(argv=None):
    try:
        install(argv)
    except (OSError, tarfile.TarError, UnicodeDecodeError) as e:
        die(f"install failed: {e}")

if __name__ == "__main__":
    main()
