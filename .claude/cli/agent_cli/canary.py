"""`agent-cli harness canary`: run the canary exam (harness-loop/loop.py exam --subscription-only) for a PR or two
installed trees, and pass its exit code through. This module only decides where and what; loop.py owns the exam.

agent-workflow source tree (no .claude/HARNESS_VERSION): the local loop.py, with --pr N (`exam --pr N`) or with
--candidate/--baseline (installed mode). Installing repo (.claude/HARNESS_VERSION present):
  --pr N: gh pr view (gh fails: exit 3; fork: exit 5, nothing posted), fetch the PR head and its base, then the
  installing-repo path gate (not needed: success status, nothing cloned); the candidate is the PR merged onto its
  base (git merge-tree, as loop.py's restack; a conflict: failure "stale, rebase the PR onto main", exit 1). Then
  clone agent-workflow (HARNESS_CANARY_SOURCE, default the public URL) at HARNESS_CANARY_REF or the merged tree's
  pinned HARNESS_VERSION commit and run its loop.py with the merged tree's and the base's .claude + AGENTS.md
  (git archive) as --candidate-installed and --baseline-installed, posting status harness-canary on THIS repo's
  PR head.
  --candidate DIR --baseline DIR: the same clone (ref: HARNESS_CANARY_REF or the RUNNING install's commit), no
  gate, no status.
loop.py fetches the tasks itself, after its gate, preflight and Docker check. For an installing repo,
HARNESS_LOOP_HOME defaults to ~/.harness-loop-private/<repo name> (an inherited value wins): its trees are its own,
and ~/.harness-loop feeds agent-workflow's public improver. `exam-env-file:` under `## harness` in project-rules.md
becomes HARNESS_EXAM_ENV_FILE for loop.py (else an inherited one passes through). CLAUDE_CODE_REMOTE set: exit 5
"laptop-only" before any gh, git or clone. A git fetch, diff, merge-tree or archive failure: exit 5, status
error.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import core
from .core import Exit
from .pr import CANARY as CONTEXT, GhError, gh

PUBLIC_SOURCE = "https://github.com/nguyenv119/agent-workflow"
MANIFEST = "tasks/canary-2026-10.json"  # relative to harness-loop/
EXAMPLE = "agent-cli harness canary --pr 91"
EXITS = ("exit: 0 pass or not needed; 1 fail (after one rerun) or stale; 2 usage or refused config; 3 gh could not "
         "read the PR; 4 paused or no plan token; 5 could not run (laptop-only, fork PR, git or clone failed, the PR "
         "moved, Docker down, task fetch failed, both runs errored)")
STALE = "stale, rebase the PR onto main"
PREREQS = ("needs Docker running and the plan token: CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), or a file "
           "holding it named by HARNESS_EXAM_ENV_FILE or `exam-env-file:` under `## harness` in project-rules.md")
# harness-loop/loop.py's WORK_LOADED and FRONTMATTER_FILE (a test pins they match): what a /work run loads.
WORK_LOADED = ("AGENTS.md", ".claude/settings.json", ".claude/hooks/", ".claude/commands/work.md",
               ".claude/agents/implementer.md", ".claude/agents/quick-reviewer.md",
               *(f".claude/skills/{s}/" for s in ("coordinator", "implementer", "reviewer-correctness",
                                                  "reviewer-tests", "reviewer-architecture", "test-runner",
                                                  "standards", "quick", "win-condition", "blast-radius",
                                                  "cli-for-agents")))
FRONTMATTER_FILE = re.compile(r"\.claude/(skills/[^/]+/SKILL|agents/[^/]+)\.md")
# An installing repo's seams every /work runs, and its pin (a bump changes every installed file).
SEAMS = (".claude/project-rules.md", ".claude/CLAUDE.md", ".claude/agent-cli-hooks/", ".claude/HARNESS_VERSION")


def register(hs):
    p = core.subcommand(hs, "canary", "canary exam on the plan token for a harness PR",
                        [EXAMPLE, "agent-cli harness canary --pr 12 --repo owner/app",
                         "agent-cli harness canary --candidate ../app-pr --baseline ../app-main --no-status"])
    p.description = (f"Runs harness-loop's exam for a PR (or two installed trees) and posts commit status {CONTEXT} "
                     f"on the PR head.\n{PREREQS}.\n{EXITS}.")
    p.add_argument("--pr", type=int, help="PR number in this repo")
    p.add_argument("--repo", metavar="OWNER/NAME", help="the PR's repo (default: gh's for the cwd)")
    p.add_argument("--candidate", metavar="DIR", help="an installed tree (.claude + AGENTS.md) to test (no --pr)")
    p.add_argument("--baseline", metavar="DIR", help="main's installed tree (no --pr)")
    p.add_argument("--no-status", action="store_true", help="post no commit status (implied without --pr)")
    p.set_defaults(func=canary_cmd)


def version(root):
    f = Path(root) / ".claude/HARNESS_VERSION"
    try:
        return json.loads(f.read_text()) if f.is_file() else None
    except ValueError:
        core.die(Exit.REFUSED, f"{f} is not valid JSON; reinstall", EXAMPLE)


def frontmatter(text):
    m = re.match(r"---\n.*?\n---\n", text or "", re.S)
    return m[0] if m else None


def show(root, sha, path):
    return core.git("show", f"{sha}:{path}", cwd=root, check=False, strip=False)


def link_targets(root, sha):
    """Repo paths the symlinks under .claude/agent-cli-hooks/ at sha point to (every /work runs them)."""
    out = []
    for line in (core.git("ls-tree", "-r", sha, "--", ".claude/agent-cli-hooks/", cwd=root, check=False)
                 or "").splitlines():
        meta, path = line.split("\t", 1)
        if meta.startswith("120000"):
            target = os.path.normpath(os.path.join(os.path.dirname(path), show(root, sha, path) or ""))
            out += [target, target + "/"]  # a file, or a dir and everything under it
    return out


def needed(root, base, head, target):
    """(needs_exam, reason): loop.py's path gate for an installing repo; could not run (status error on target)
    when git diff or merge-base fails. A path counts as loaded when it, or the
    installed path its add-on source maps to (<addon>/x -> .claude/x), is in WORK_LOADED or SEAMS or a hooks-seam
    link target; a skill's or agent's SKILL.md counts when its frontmatter changed. The add-on's .skipbase and
    settings.add.json change the installed base and settings, so they count too."""
    from .harness import addon_dir  # here, not at the top: harness.py imports this module
    addon = addon_dir(Path(root)).rstrip("/")
    loaded = (*WORK_LOADED, *SEAMS, *link_targets(root, head))
    diff = core.git("diff", "-z", "--name-only", "--no-renames", f"{base}...{head}", cwd=root, check=False,
                    strip=False)
    mb = core.git("merge-base", base, head, cwd=root, check=False)
    if diff is None or not mb:
        could_not_run(target, "git diff failed")
    hits, rest = [], []
    for p in filter(None, diff.split("\0")):
        q = ".claude/" + p[len(addon) + 1:] if p.startswith(addon + "/") else p
        if q in (".claude/.skipbase", ".claude/settings.add.json") or any(
                q == w or (w.endswith("/") and q.startswith(w)) for w in loaded):
            hits.append(p)
        elif FRONTMATTER_FILE.fullmatch(q) and frontmatter(show(root, mb, p)) != frontmatter(show(root, head, p)):
            hits.append(f"{p} (frontmatter)")
        else:
            rest.append(p)
    if hits:
        return True, f"/work loads {', '.join(hits)}"
    return False, f"touches only {', '.join(rest) or 'nothing'}; /work loads none of them"


def post(target, state, desc):
    """Commit status harness-canary on target (repo, sha); None posts nothing."""
    if not target:
        return
    try:
        r = gh(["api", f"repos/{target[0]}/statuses/{target[1]}", "-f", f"state={state}",
                   "-f", f"context={CONTEXT}", "-f", f"description={desc[:140]}"], None)
        err = r.stderr.strip()[-200:] if r.returncode else None
    except GhError as e:
        err = str(e)
    if err is not None:
        core.die(Exit.PARTIAL, f"could not post status {CONTEXT}: {err}", EXAMPLE)
    print(f"canary: status {CONTEXT}={state} on {target[1][:7]}", file=sys.stderr)


def could_not_run(target, reason):
    post(target, "error", f"could not run: {reason}")
    core.die(Exit.PARTIAL, f"could not run: {reason}", EXAMPLE)


def pr_info(a):
    """gh pr view of --pr: exit 5 (nothing posted) on a fork PR, 3 when gh fails (as agent-cli pr)."""
    try:
        r = gh(["pr", "view", str(a.pr), "--json",
                   "isCrossRepository,headRepositoryOwner,headRefOid,baseRefName,url"], a.repo)
        if r.returncode:
            raise ValueError(r.stderr.strip()[-200:])
        view = json.loads(r.stdout)
    except (GhError, ValueError) as e:
        core.die(Exit.BLOCKED, f"gh pr view {a.pr}: {e}", EXAMPLE)
    if view.get("isCrossRepository"):
        core.die(Exit.PARTIAL, "could not run: fork PR, not run with the plan token; nothing posted", EXAMPLE)
    view["repo"] = a.repo or "/".join(view["url"].split("/")[3:5])
    return view


def run(cmd, env=None, cwd=None):
    """Run a child with our stdout/stderr (an exam runs for many minutes); its exit code."""
    print(f"canary: {' '.join(map(str, cmd[:3]))} ...", file=sys.stderr)
    return subprocess.run([str(c) for c in cmd], env=env, cwd=cwd).returncode


def exam_env(root, private):
    """loop.py's env: HARNESS_EXAM_ENV_FILE from project-rules, and (private: an installing repo) HARNESS_LOOP_HOME
    ~/.harness-loop-private/<repo> unless one is inherited."""
    env = core.child_env()
    f = core.rules_line("harness", "exam-env-file")
    if f:
        env["HARNESS_EXAM_ENV_FILE"] = str(Path(f).expanduser())
    if private and not env.get("HARNESS_LOOP_HOME"):
        common = core.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root, check=False)
        name = Path(common).parent.name if common else Path(root).name
        env["HARNESS_LOOP_HOME"] = str(Path.home() / ".harness-loop-private" / name)
    return env


def fetch(root, ref, target):
    """The sha of origin's ref, fetched; could not run when git fails."""
    sha = (core.git("fetch", "-q", "origin", ref, cwd=root, check=False) is not None
           and core.git("rev-parse", "FETCH_HEAD", cwd=root, check=False))
    if not sha:
        could_not_run(target, f"git fetch of {ref} failed")
    return sha


def landed(root, base, head, target):
    """The tree head's change makes on base (git merge-tree, as loop.py's restack): the PR as it would land.
    A conflict posts failure "stale, rebase the PR onto main" and exits 1, as loop.py's free check does."""
    p = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", base, head],
                       capture_output=True, text=True)
    if p.returncode == 1:
        post(target, "failure", STALE)
        core.die(Exit.FAIL, f"PR {head[:7]}: {STALE} ({base[:7]})", EXAMPLE)
    if p.returncode:
        could_not_run(target, "git merge-tree failed")
    return p.stdout.split()[0]


def archive(root, treeish, dest, target):
    """dest = treeish's .claude + AGENTS.md (an installed tree as committed); could not run when git fails."""
    paths = (core.git("ls-tree", "--name-only", treeish, "--", ".claude", "AGENTS.md", cwd=root, check=False)
             or "").split()
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", treeish, "--", *paths],
                         capture_output=True)
    if not paths or tar.returncode or subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout).returncode:
        could_not_run(target, "git archive failed")
    return dest


def canary_cmd(a):
    if os.environ.get("CLAUDE_CODE_REMOTE"):
        core.die(Exit.PARTIAL, "could not run: the canary is laptop-only (CLAUDE_CODE_REMOTE is set); nothing posted",
                 EXAMPLE)
    if (a.pr is None) == (a.candidate is None) or (a.candidate is None) != (a.baseline is None):
        core.die(Exit.REFUSED, "give --pr N, or --candidate DIR and --baseline DIR", EXAMPLE)
    root = core.CLAUDE_DIR.parent
    pr = pr_info(a) if a.pr is not None else None
    target = (pr["repo"], pr["headRefOid"]) if pr and not a.no_status else None
    trees = [Path(d).resolve() for d in (a.candidate, a.baseline)] if pr is None else None
    pin = version(root)
    flags = ["--tasks", MANIFEST, "--subscription-only", "--rerun-on-fail", "1"]
    if pin is None:  # the agent-workflow source tree: its own loop.py
        loop_dir = root / "harness-loop"
        mode = (["--pr", str(a.pr), *(["--post-status"] if target else []),
                 *(["--status-repo", target[0]] if target else [])] if pr
                else ["--candidate-installed", trees[0], "--baseline-installed", trees[1]])
        return run([sys.executable, loop_dir / "loop.py", "exam", *mode, *flags], env=exam_env(root, False),
                   cwd=loop_dir)
    with tempfile.TemporaryDirectory(prefix="harness-canary-") as tmp:
        ref = os.environ.get("HARNESS_CANARY_REF") or pin.get("commit")
        if pr:
            base = fetch(root, pr["baseRefName"], target)
            head = fetch(root, f"pull/{a.pr}/head", target)
            if head != pr["headRefOid"]:
                could_not_run(target, f"PR #{a.pr} moved while fetching; run again")
            needs, reason = needed(root, base, head, target)
            print(f"canary: PR #{a.pr} {head[:7]}: {'exam needed' if needs else 'not needed'}: {reason}")
            if not needs:
                post(target, "success", "not needed (no loaded file)")
                return Exit.OK
            cand = landed(root, base, head, target)
            ref = os.environ.get("HARNESS_CANARY_REF") or (json.loads(show(root, cand, ".claude/HARNESS_VERSION")
                                                                      or "{}").get("commit"))
            trees = [archive(root, cand, Path(tmp) / "candidate", target),
                     archive(root, base, Path(tmp) / "baseline", target)]
        source = os.environ.get("HARNESS_CANARY_SOURCE") or PUBLIC_SOURCE
        print(f"canary: agent-workflow source {source} ref {ref}")
        aw = Path(tmp) / "agent-workflow"
        if not ref or run(["git", "clone", "-q", "--no-checkout", source, aw]) \
                or run(["git", "-C", aw, "checkout", "-q", "--detach", ref]):
            could_not_run(target, "could not check out agent-workflow at the pinned commit")
        loop_dir = aw / "harness-loop"
        status = (["--post-status", "--status-repo", target[0], "--status-sha", target[1]] if target else [])
        return run([sys.executable, loop_dir / "loop.py", "exam", "--candidate-installed", trees[0],
                    "--baseline-installed", trees[1], *flags, *status],
                   env=exam_env(root, True), cwd=loop_dir)
