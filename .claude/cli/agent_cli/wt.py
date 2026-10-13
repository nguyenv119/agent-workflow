"""`agent-cli wt`: bead worktrees (new, list, audit, rm) with the project's wt-setup seam.

Naming is the coordinator's: branch feature/bd-<id>-<slug> at <main>/../<main name>-bd-<id>-<slug>.
"""
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import core
from .core import Exit

SAFE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
TOKEN = re.compile(r"[^-.]+(?:\.\d+)*")
PREFIX = re.compile(r"""^issue-prefix:\s*["']?([\w.-]+)""", re.M)
STALE_DAYS = 14
EX_NEW = "agent-cli wt new 42 add-user-login --base origin/main"
EX_RM = "agent-cli wt rm --branch feature/bd-42-add-user-login --delete-branch --yes"


git = functools.partial(core.git, example=EX_NEW)  # pass cwd=; check=False -> None on failure


def main_root(repo):
    """The main checkout (first `git worktree list` entry) of --repo or the cwd's repo; exit 3 when none."""
    out = git("worktree", "list", "--porcelain", cwd=repo or ".", check=False)
    if not out:
        core.die(Exit.BLOCKED, "no git repo here: run from inside the repo or pass --repo <path>",
                 "agent-cli wt new 42 add-user-login --repo ~/src/<repo>")
    return Path(out.splitlines()[0].split(" ", 1)[1])


def worktrees(main):
    """[{path, branch, head}] from `git worktree list --porcelain`, main checkout first."""
    rows = []
    for block in git("worktree", "list", "--porcelain", cwd=main).split("\n\n"):
        kv = dict((ln.split(" ", 1) + [""])[:2] for ln in block.splitlines())
        rows.append({"path": kv["worktree"], "head": kv.get("HEAD", "")[:7],
                     "branch": kv["branch"][len("refs/heads/"):] if "branch" in kv else None})
    return rows


def find(main, path=None, branch=None):
    real = os.path.realpath(path) if path else None
    return next((w for w in worktrees(main)
                 if (real and os.path.realpath(w["path"]) == real) or (branch and w["branch"] == branch)), None)


def bead_prefix(main):
    """The beads issue prefix: .beads/config.yaml `issue-prefix`, else the main checkout's dir name."""
    f = Path(main) / ".beads/config.yaml"
    m = PREFIX.search(f.read_text()) if f.is_file() else None
    return m[1] if m else Path(main).name


def bead_of(branch, prefix):
    """The bead id in feature/bd-<id>-<slug>: [<prefix>-]<token>[.n...]. None when unsure: no bd- branch, or
    no prefix and an all-letter token (it could be another project's prefix)."""
    rest = (branch or "")[len("feature/bd-"):] if (branch or "").startswith("feature/bd-") else ""
    has_prefix = rest.startswith(prefix + "-")
    m = TOKEN.match(rest[len(prefix) + 1:] if has_prefix else rest)
    if not m or not (has_prefix or re.search(r"\d", m[0])):
        return None
    return f"{prefix}-{m[0]}" if has_prefix else m[0]


def dirty(path):
    return bool(git("status", "--porcelain", cwd=path, check=False))


def info(w, main):
    """A list row: path, branch, bead, dirty, ahead/behind origin/main, size_kb, age_days (last commit)."""
    p = w["path"]
    row = {"path": p, "branch": w["branch"], "bead": bead_of(w["branch"], bead_prefix(main)), "main": p == str(main)}
    if not os.path.isdir(p):
        return dict(row, missing=True, dirty=False, ahead=None, behind=None, size_kb=0, age_days=None)
    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD", cwd=p, check=False)
    behind, ahead = map(int, counts.split()) if counts else (None, None)
    ct = git("log", "-1", "--format=%ct", cwd=p, check=False)
    du = subprocess.run(["du", "-sk", p], capture_output=True, text=True).stdout.split()
    return dict(row, dirty=dirty(p), ahead=ahead, behind=behind, size_kb=int(du[0]) if du else 0,
                age_days=round((time.time() - int(ct)) / 86400, 1) if ct else None)


def local_branch(main, ref):
    """ref, or for a ref found only under refs/remotes/ (origin/<b>), the local <b> that tracks it."""
    has = lambda r: git("show-ref", "--verify", "-q", r, cwd=main, check=False) is not None  # noqa: E731
    return ref if has(f"refs/heads/{ref}") or not has(f"refs/remotes/{ref}") else ref.split("/", 1)[-1]


def wt_new(a):
    for name, val in (("bead-id", a.bead_id), ("slug", a.slug)):
        if not SAFE.fullmatch(val):
            core.die(Exit.REFUSED, f"{name} {val!r}: use letters, digits, '.', '_' and '-' only", EX_NEW)
    start = time.time()
    main = main_root(a.repo)
    branch = local_branch(main, a.existing) if a.existing else f"feature/bd-{a.bead_id}-{a.slug}"
    path = main.parent / f"{main.name}-bd-{a.bead_id}-{a.slug}"
    seam = core.find_seam("wt-setup")
    if path.exists():
        w = find(main, path=path)
        if not w or w["branch"] != branch:
            on = f"is on branch {w['branch']}" if w else "exists and is not a worktree"
            core.die(Exit.REFUSED, f"{path} {on}, not {branch}",
                     f"agent-cli wt rm --path {path} --yes  (or pick another slug)")
        created = False
    elif a.dry_run:
        if a.json:
            core.emit({"path": str(path), "branch": branch, "base": None if a.existing else a.base,
                       "created": False, "setup": "would run" if seam else "none", "dry_run": True}, True)
        else:
            print(f"would create: {path} ({branch} from {a.existing or a.base}); setup: {seam or 'none'}")
        return Exit.OK
    else:
        git("fetch", "origin", "-q", cwd=main)
        if a.existing:
            branch = local_branch(main, a.existing)
        track = a.existing and branch != a.existing and git("show-ref", "--verify", "-q", f"refs/heads/{branch}",
                                                            cwd=main, check=False) is None
        add = (["--track", "-b", branch, str(path), a.existing] if track else [str(path), branch] if a.existing
               else ["-b", branch, str(path), a.base])
        git("worktree", "add", "-q", *add, cwd=main)
        created = True
    if seam and not a.dry_run:
        try:  # --json keeps stdout one object: the seam's output streams to stderr instead
            rc = core.run_seam(seam, [str(path), a.bead_id, a.slug], stdout=sys.stderr if a.json else None).returncode
        except OSError as e:
            rc = e
        if rc:
            core.die(Exit.BLOCKED, f"setup hook failed: {seam} ({rc})", f"fix the hook, then rerun: {EX_NEW}")
    out = {"path": str(path), "branch": branch, "base": None if a.existing else a.base,
           "head": git("rev-parse", "--short", "HEAD", cwd=path), "created": created,
           "setup": "ran" if seam and not a.dry_run else "none", "duration_s": round(time.time() - start, 1)}
    if a.json:
        core.emit(out, True)
    else:
        print(f"created: {path} ({branch} from {out['base']})" if created else f"exists: {path}")
    return Exit.OK


def wt_list(a):
    main = main_root(a.repo)
    for w in worktrees(main):
        row = info(w, main)
        print(json.dumps(row) if a.json else
              f"{row['path']}  {row['branch']}  bead={row['bead']}  dirty={row['dirty']}  "
              f"ahead={row['ahead']} behind={row['behind']}  {row['size_kb']}K  {row['age_days']}d")


def merged_pr(branch, main):
    """True when gh reports a merged PR for branch; False when gh is missing or errors."""
    if not branch or not shutil.which("gh"):
        return False
    try:
        r = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "merged", "--json", "number",
                            "--limit", "1"], cwd=main, capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and bool(json.loads(r.stdout or "[]"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def bucket(row, main):
    if row["main"]:
        return "main"
    if row.get("missing"):
        return "missing"
    if row["ahead"] == 0 and row["behind"] == 0 and not row["dirty"]:
        return "empty"
    if (row["ahead"] == 0 and row["behind"] and not row["dirty"]) or merged_pr(row["branch"], main):
        return "merged"
    if not row["dirty"] and row["age_days"] is not None and row["age_days"] >= STALE_DAYS:
        return "stale"
    return "active"


def wt_audit(a):
    """Read-only prune audit. Ported from the pstack worktree-cleanup playbook's worktree-audit.sh
    (github.com/cursor/plugins, MIT): biggest first, a suggested bucket each. Never deletes."""
    main = main_root(a.repo)
    rows = sorted((info(w, main) for w in worktrees(main)), key=lambda r: -r["size_kb"])
    for row in rows:
        row["bucket"] = bucket(row, main)
        print(json.dumps(row) if a.json else f"{row['bucket']:7} {row['size_kb']:>9}K  {row['path']}  {row['branch']}")


def deletable(main, b):
    """Would `git branch -d b` succeed: b merged into HEAD or into its upstream."""
    return any(git("merge-base", "--is-ancestor", b, t, cwd=main, check=False) is not None
               for t in ("HEAD", f"{b}@{{upstream}}"))


def wt_rm(a):
    main = main_root(a.repo)
    w = find(main, path=a.path, branch=a.branch)
    if w and os.path.realpath(w["path"]) == os.path.realpath(main):
        core.die(Exit.REFUSED, "that is the main checkout; wt rm only removes bead worktrees", EX_RM)
    branch = a.branch or (w and w["branch"])
    if w and os.path.isdir(w["path"]) and not a.force and dirty(w["path"]):
        core.die(Exit.REFUSED, f"{w['path']} has uncommitted or untracked files; commit them, or pass --force "
                 "to discard them", EX_RM + " --force")
    has_branch = a.delete_branch and branch and git("show-ref", "--verify", "-q", f"refs/heads/{branch}",
                                                     cwd=main, check=False) is not None
    dry = a.dry_run or not a.yes
    out = {"path": w and w["path"], "branch": branch,
           "worktree": ("would remove" if dry else "removed") if w else "already done",
           "branch_deleted": "kept" if not a.delete_branch else "absent" if not has_branch
           else "deleted" if not dry else "would delete" if a.force or deletable(main, branch)
           else "would keep (unmerged; needs --force)"}
    if not dry and w:
        git("worktree", "remove", *(["--force"] if a.force else []), w["path"], cwd=main, example=EX_RM)
    if not dry and has_branch and git("branch", "-d", branch, cwd=main, check=False) is None:
        if not a.force:
            core.die(Exit.PARTIAL, f"worktree {out['worktree']}; branch {branch} is not merged, so it was kept. "
                     "A squash-merged branch needs --force", EX_RM + " --force")
        git("branch", "-D", branch, cwd=main, example=EX_RM)
    core.emit(out, a.json)
    return Exit.OK


def register(sub):
    _, ws = core.group(sub, "wt", "bead worktrees: create (with the project's wt-setup hook), list, audit, remove",
                       [EX_NEW, "agent-cli wt list", "agent-cli wt audit --json", EX_RM])
    p = core.subcommand(ws, "new", "create <main>-bd-<id>-<slug> on feature/bd-<id>-<slug>, then run wt-setup",
                        [EX_NEW, "agent-cli wt new 42 add-user-login --json",
                         "agent-cli wt new 42 review --existing <branch>",
                         "agent-cli wt new 42 add-user-login --repo ~/src/<repo> --dry-run"])
    p.add_argument("bead_id", metavar="bead-id")
    p.add_argument("slug", help="a few kebab-case words from the bead title")
    p.add_argument("--base", default="origin/main", help="start point of the new branch (default origin/main)")
    p.add_argument("--existing", metavar="BRANCH", help="check out this existing branch instead of making one")
    p.add_argument("--dry-run", action="store_true", help="say what would be created, write nothing")
    p.set_defaults(func=wt_new)
    p_list = core.subcommand(ws, "list", "each worktree: branch, bead, dirty, ahead/behind origin/main, size, age",
                             ["agent-cli wt list", "agent-cli wt list --json"])
    p_list.set_defaults(func=wt_list)
    p_audit = core.subcommand(ws, "audit", "read-only prune audit: biggest first, bucket merged|empty|stale|active|missing",
                              ["agent-cli wt audit", "agent-cli wt audit --json"])
    p_audit.set_defaults(func=wt_audit)
    p_rm = core.subcommand(ws, "rm", "remove a worktree (dry run unless --yes); never touches the remote branch",
                           [EX_RM, "agent-cli wt rm --path ../<repo>-bd-42-add-user-login --yes",
                            EX_RM + " --force"])
    g = p_rm.add_mutually_exclusive_group(required=True)
    g.add_argument("--branch", help="the worktree on this branch")
    g.add_argument("--path", help="the worktree at this path")
    p_rm.add_argument("--delete-branch", action="store_true", help="also delete the local branch (even with no worktree)")
    p_rm.add_argument("--force", action="store_true", help="discard local changes; delete an unmerged branch (-D)")
    p_rm.add_argument("--dry-run", action="store_true", help="say what would be removed (the default without --yes)")
    p_rm.add_argument("--yes", action="store_true", help="really remove")
    p_rm.set_defaults(func=wt_rm)
    for q in (p, p_list, p_audit, p_rm):
        q.add_argument("--repo", metavar="DIR", help="the git repo, when the cwd is not inside it")
        q.add_argument("--json", action="store_true", help="one JSON object per line")
