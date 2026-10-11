"""Shared test helpers for harness-loop tests (stdlib only)."""
import hashlib
import os
import subprocess
from pathlib import Path

GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def run(cmd, cwd, check=True):
    """Run cmd in cwd and return the CompletedProcess (text output captured)."""
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def make_repo(tmpdir, files):
    """Commit `files` ({relpath: content, or None to delete}) to a git repo at tmpdir.

    Inits the repo on first call; calling again on the same dir adds a commit.
    Returns (path, full commit sha).
    """
    path = Path(tmpdir)
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        run(GIT + ["init", "-q"], path)
    for rel, content in files.items():
        f = path / rel
        if content is None:
            f.unlink()
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    run(GIT + ["add", "-A"], path)
    run(GIT + ["commit", "-q", "--allow-empty", "-m", "c"], path)
    return path, run(GIT + ["rev-parse", "HEAD"], path).stdout.strip()


def tree_hashes(root):
    """Map every file under root (relative posix path) to the sha256 of its bytes."""
    root = Path(root)
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out
