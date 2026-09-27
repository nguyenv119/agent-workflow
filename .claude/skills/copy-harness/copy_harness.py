#!/usr/bin/env python3
"""Copy the up-to-date agent harness into another repo. Rules and follow-ups: SKILL.md beside this file.

  python3 copy_harness.py <target-repo> [--dry-run]
  python3 copy_harness.py --self-test
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()

# Every live copy of the harness, as (repo root, whether its AGENTS.md is the harness one).
# conspectus/conspectus's AGENTS.md is that repo's own one-line pointer, so it is never copied.
SOURCES = [
    (HOME / "long-harness/agent-workflow", True),
    (HOME / "conspectus", True),
    (HOME / "conspectus/conspectus", False),
]
TREES = [".claude/skills", ".claude/commands", ".claude/hooks", ".claude/agents"]
FILES = [".claude/smoke-test.sh"]  # /smoke-test runs it
OWN = [".claude/settings.json", "AGENTS.md"]  # often the target's own: copied only when it has none
BACKUPS = HOME / ".claude/harness-backups"


def junk(name):
    return name in (".DS_Store", "__pycache__") or name.endswith((".pyc", ".log")) or ".bak" in name


def harness_files(root, agents_md):
    """Repo-relative paths of every harness file under root. Symlinks are followed, so the
    conspectus copies (which link into each other) come out as real files."""
    for tree in TREES:
        for dirpath, dirnames, filenames in os.walk(root / tree, followlinks=True):
            dirnames[:] = [d for d in dirnames if not junk(d)]
            yield from (Path(dirpath, f).relative_to(root) for f in filenames if not junk(f))
    for rel in FILES + OWN:
        if (root / rel).is_file() and (agents_md or rel != "AGENTS.md"):
            yield Path(rel)


def stamp(mtime):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))


def label(root):
    return str(root.relative_to(HOME)) if root.is_relative_to(HOME) else str(root)


def git(target, *args):
    """Stdout of a git command run in target, or None when it fails (not a repo, no commits yet)."""
    run = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)
    return run.stdout if run.returncode == 0 else None


def copy_harness(target, dry_run=False, sources=SOURCES, backups=BACKUPS):
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        sys.exit(f"copy-harness: {target} is not a folder")
    if any(target == root.resolve() for root, _ in sources):
        sys.exit(f"copy-harness: {target} is one of the harness sources; pick another repo")

    copies = {}  # rel path -> [(mtime, source label, path)] across every source that has it
    for root, agents_md in sources:
        if not root.is_dir():
            print(f"  source missing, skipped: {root}")
            continue
        for rel in harness_files(root, agents_md):
            path = root / rel
            copies.setdefault(rel, []).append((path.stat().st_mtime, label(root), path))
    if not copies:
        sys.exit("copy-harness: no harness files found in any source")

    # A fresh clone stamps every file with the clone time, so a tracked file's date says nothing.
    # Ask git instead: tracked and unchanged since the last commit means the repo's committed copy.
    exclude_path = git(target, "rev-parse", "--git-path", "info/exclude")  # None: not a git repo
    changed = git(target, "diff", "--name-only", "--relative", "-z", "HEAD") if exclude_path else None  # None: no commits yet
    committed = set() if changed is None else set((git(target, "ls-files", "-z") or "").split("\0")) - set(changed.split("\0"))

    backup_dir = backups / f"{target.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    new, replaced, kept, outside, disagree, unchanged = [], [], [], [], [], 0
    for rel in sorted(copies):
        mtime, source, src = max(copies[rel], key=lambda c: c[0])  # newest copy wins
        body = src.read_bytes()
        older = [f"{name} {stamp(m)}" for m, name, p in copies[rel] if p != src and p.read_bytes() != body]
        if older:
            disagree.append(f"{rel}: {source} {stamp(mtime)} (older: {', '.join(older)})")

        dest = target / rel
        if not dest.resolve().is_relative_to(target):
            outside.append(str(rel))  # a symlink in the target leads elsewhere; writing through it edits that place
            continue
        if dest.exists():
            if dest.read_bytes() == body:
                unchanged += 1
                continue
            if str(rel) in OWN:  # may be the project's own or carry local edits, so a person decides
                kept.append(f"{rel}: the target already has one, left as is (compare it with {source}'s copy)")
                continue
            if dest.stat().st_mtime > mtime and str(rel) not in committed:
                kept.append(f"{rel}: the target's copy is newer than every source")
                continue
            replaced.append(str(rel))
            if not dry_run:
                (backup_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup_dir / rel)
        else:
            new.append(str(rel))
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)  # keeps the exec bit (hooks) and the source mtime (so a rerun can tell our copies from local edits)

    # Keep the harness out of the target's commits, the way ~/conspectus/conspectus does.
    excluded, tracked, exclude = [], [], None
    if exclude_path:
        exclude = target / exclude_path.strip()
        text = exclude.read_text() if exclude.exists() else ""
        wanted = [".claude/"] + (["AGENTS.md"] if "AGENTS.md" in new else [])
        excluded = [p for p in wanted if p not in text.splitlines()]
        if excluded and not dry_run:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            gap = "\n" if text and not text.endswith("\n") else ""
            exclude.write_text(text + gap + "# local agent harness (copy-harness)\n" + "".join(p + "\n" for p in excluded))
        if not dry_run:
            tracked = (git(target, "status", "--short", "--", ".claude", "AGENTS.md") or "").splitlines()

    def names(tree, suffix=""):
        return sorted({r.parts[2].removesuffix(suffix) for r in copies if r.parts[:2] == (".claude", tree) and len(r.parts) > 2})

    def section(title, lines):
        if lines:
            print(f"  {title}\n" + "".join(f"    {line}\n" for line in lines), end="")

    print(f"copy-harness -> {target}" + ("   [dry run: nothing written]" if dry_run else ""))
    print(f"  new {len(new)} · replaced {len(replaced)} · unchanged {unchanged} · kept {len(kept)}")
    skills, commands = names("skills"), names("commands", ".md")
    print(f"  skills ({len(skills)}): {', '.join(skills)}")
    print(f"  commands ({len(commands)}): {' '.join('/' + c for c in commands)}")
    section("replaced" + ("" if dry_run else f" (old versions saved in {backup_dir})") + ":", replaced)
    section("kept:", kept)
    section("skipped, a symlink in the target leads outside it:", outside)
    section(f"copies disagree, newest used ({len(disagree)}):", disagree)
    section(f"added to {exclude}:", excluded)
    section("git sees tracked files changed (git diff to review, git checkout -- <file> to undo):", tracked)
    return new, replaced


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp).resolve()
        a, b, target, outside = tmp / "a", tmp / "b", tmp / "target", tmp / "outside"

        def put(path, text, mtime):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            os.utime(path, (mtime, mtime))

        put(a / ".claude/skills/s/SKILL.md", "old", 1000)
        put(b / ".claude/skills/s/SKILL.md", "new", 2000)  # newest copy wins
        put(a / ".claude/skills/s/tool.py.bak-1", "junk", 3000)  # backups and logs are skipped
        put(a / ".claude/skills/s/run.sh", "#!/bin/sh", 1000)
        os.chmod(a / ".claude/skills/s/run.sh", 0o755)  # scripts and hooks stay executable
        put(a / ".claude/skills/t/SKILL.md", "linked", 1000)
        (b / ".claude/skills/t2").symlink_to(a / ".claude/skills/t")  # a linked skill lands as real files
        put(a / ".claude/skills/mine/SKILL.md", "source", 1000)
        put(b / ".claude/commands/c.md", "fresh", 2000)
        put(a / ".claude/hooks/h.sh", "hook", 1000)
        put(a / "AGENTS.md", "harness", 1000)
        put(a / ".claude/settings.json", "settings v1", 1000)  # the target has none, so it gets ours
        put(target / "AGENTS.md", "the target's own", 500)  # never replaced
        put(target / ".claude/commands/c.md", "stale", 500)  # replaced, old version backed up
        put(target / ".claude/skills/mine/SKILL.md", "local edit", 5000)  # newer than every source: kept
        put(target / ".claude/skills/extra/SKILL.md", "target only", 500)  # never deleted
        outside.mkdir()
        (target / ".claude/hooks").symlink_to(outside)  # never written through
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        put(a / ".claude/skills/team/SKILL.md", "harness", 1000)
        put(a / ".claude/skills/team2/SKILL.md", "harness", 1000)
        put(target / ".claude/skills/team/SKILL.md", "older, committed", 9000)  # a fresh clone's date: replaced anyway
        put(target / ".claude/skills/team2/SKILL.md", "committed", 9000)
        git(target, "add", ".claude/skills/team", ".claude/skills/team2")
        assert git(target, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                   "commit", "-q", "--no-verify", "-m", "team") is not None, "self-test setup: commit failed"
        put(target / ".claude/skills/team2/SKILL.md", "uncommitted edit", 9000)  # a local edit to a tracked file: kept

        sources, out = [(a, True), (b, True)], io.StringIO()
        with contextlib.redirect_stdout(out):
            copy_harness(target, dry_run=True, sources=sources, backups=tmp / "backups")
            dry_wrote = ((target / ".claude/skills/s").exists() or (tmp / "backups").exists()
                         or ".claude/" in (target / ".git/info/exclude").read_text().splitlines())
            copy_harness(target, sources=sources, backups=tmp / "backups")
            first = (target / ".claude/skills/s/SKILL.md").read_text()
            put(b / ".claude/skills/s/SKILL.md", "newer", 3000)  # sources move on after the copy
            put(a / ".claude/settings.json", "settings v2", 3000)
            refresh = copy_harness(target, sources=sources, backups=tmp / "backups")
            again = copy_harness(target, sources=sources, backups=tmp / "backups")

        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            copy_harness(b, sources=sources, backups=tmp / "backups")
            raise AssertionError("copied into one of its own sources")

        def read(rel):
            return (target / rel).read_text()

        assert not dry_wrote, "a dry run wrote to the target"
        assert first == "new"
        assert ".claude/skills/s/SKILL.md: " in out.getvalue()  # listed as a disagreement
        assert read(".claude/skills/s/SKILL.md") == "newer"
        assert read(".claude/settings.json") == "settings v1"  # copied while missing, never replaced after
        assert "settings.json: the target already has one" in out.getvalue()
        assert refresh == ([], [".claude/skills/s/SKILL.md"]), f"refresh: {refresh}"
        assert not (target / ".claude/skills/s/tool.py.bak-1").exists()
        assert os.access(target / ".claude/skills/s/run.sh", os.X_OK)
        assert read(".claude/skills/t2/SKILL.md") == "linked" and not (target / ".claude/skills/t2").is_symlink()
        assert read("AGENTS.md") == "the target's own"
        assert read(".claude/commands/c.md") == "fresh"
        assert [p.read_text() for p in (tmp / "backups").glob("*/.claude/commands/c.md")] == ["stale"]
        assert read(".claude/skills/mine/SKILL.md") == "local edit"
        assert read(".claude/skills/team/SKILL.md") == "harness"
        assert read(".claude/skills/team2/SKILL.md") == "uncommitted edit"
        assert " M .claude/skills/team/SKILL.md" in out.getvalue()  # reported as a tracked change
        assert read(".claude/skills/extra/SKILL.md") == "target only"
        assert not (outside / "h.sh").exists()
        assert ".claude/" in (target / ".git/info/exclude").read_text().splitlines()
        assert again == ([], []), f"second run changed files: {again}"
    print("self-test OK")


if __name__ == "__main__":
    args = sys.argv[1:]
    rest = [a for a in args if a != "--dry-run"]
    if rest == ["--self-test"]:
        self_test()
    elif len(rest) == 1 and not rest[0].startswith("-"):
        copy_harness(rest[0], dry_run="--dry-run" in args)
    else:
        sys.exit(__doc__)
