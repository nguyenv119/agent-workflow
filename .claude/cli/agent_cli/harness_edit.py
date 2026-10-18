"""The harness-edit-on-main rule: harness files change only on a branch, through a PR.

first(paths, cwd) answers for the files a tool writes: "on <branch>" when one is a harness path (or a dir
holding one) in a checkout on the default branch (refs/remotes/origin/HEAD, fallback main), "on a detached
HEAD", or None (allow). A copy whose edits never reach a commit is refused on every branch: "ignored by its
checkout", "skip-worktree in its checkout", or "outside git" for a non-git path under $CLAUDE_PROJECT_DIR; its
message names the `harness-source:` line under `## harness` in project-rules.md, or asks for one. Symlinks are
followed first, so a dir linking into a main checkout is that checkout. The checkout is found by walking up to a
.git entry, so a non-harness path costs no git call; each checkout with a harness path costs one branch read and
one skip-worktree listing, and each glob's fixed part one ignore check, all cached. bash_targets(command)
lists the files a command writes (>, >>, tee, sed -i, cp/mv/ln/rm targets). A git exit non-zero reads as
detached, and as no copy for the copy checks; git missing (OSError) or slow (TimeoutExpired) raises, and the
hook fails open.
"""
import os
import re
import subprocess
import sys

from . import core
from .harness import glob_match

RULE_ID = "harness-edit-on-main"
DEFAULT_PATHS = (".claude/skills/**", ".claude/commands/**", ".claude/agents/**", ".claude/hooks/**",
                 ".claude/bin/**", ".claude/cli/**", ".claude/settings.json", ".claude/CLAUDE.md",
                 ".claude/project-rules.md", ".claude/harness-addon/**", "AGENTS.md", "CLAUDE.md")
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
MESSAGE = ("Harness file {where}: make a worktree (agent-cli wt new <bead-id> <slug> --base origin/main), edit "
           "there, open a PR; it merges with agent-cli pr land --verified-head once CI is green.")
ALL_ARGS = frozenset(("tee", "rm", "mv"))  # every operand is written (mv also removes its sources)
LAST_ARG = frozenset(("cp", "ln"))  # only the destination is written
ANCHORED = ("/", "~", "$")  # targets a cd does not move
COPY_MESSAGE = ("Harness file {where}: an edit here never reaches a commit. Edit its source{src} on a branch and "
                "open a PR{hint}.")
NO_SOURCE = (" (name the source repo with a `harness-source:` line under `## harness` in "
             ".claude/project-rules.md)")
IGNORED, SKIPPED, OUTSIDE = "ignored by its checkout", "skip-worktree in its checkout", "outside git"
MAX_CHECKOUTS = 50  # checkouts looked up (branch + skip-worktree list) per command; past this, deny unchecked


def rule(where="on <branch>"):
    if where in (OUTSIDE, IGNORED, SKIPPED):
        src = core.rules_line("harness", "harness-source")
        message = COPY_MESSAGE.format(where=where, src=f" ({src})" if src else "", hint="" if src else NO_SOURCE)
    else:
        message = MESSAGE.format(where=where)
    return {"id": RULE_ID, "mode": "deny", "source": "builtin", "message": message}


def bash_targets(command):
    """(written, removed): paths command writes (the word after each >, >>, >|, >& (any fd, &>), every operand
    of tee/rm/mv and of sed -i, the last of cp/ln), and those it removes (rm operands, mv sources). Prefixes
    (timeout N, sudo, VAR=x, the command-prefixes seam) are skipped. A cd/pushd moves later relative targets
    into its dir; after one to an unreadable dir ("$X", ~, -, a +N/-N stack entry) they are dropped. Known gap:
    a word with quoted or substituted text never counts; a cd inside ( ) still applies after it. Unbalanced
    quotes: no targets."""
    from . import shape  # shape imports hook, which imports this module
    # >(cmd) is a process substitution, not a file: as <(cmd) its `>` is not read as a redirect
    items = shape._tokens(command.replace(">(", "<(")) or []
    # sudo stays local: regex rules and shapes keep it as the command word (`sudo -u x` would leave -u there)
    cmd = shape.prefix_re(tuple(core.seam_lines("command-prefixes")) + ("sudo",))
    out, gone, seg, op, base = [], [], [], None, ""  # base: dir relative targets sit in ("" the cwd, None unknown)

    def at(ts):
        return [t if t.startswith(ANCHORED) else os.path.join(base, t) for t in ts
                if base is not None or t.startswith(ANCHORED)]
    for kind, text in items + [("sep", ";")]:
        if kind == "sep":
            words = [w for _, w in shape._strip(seg, cmd)]
            args = [w for w in words[1:] if not w.startswith("-")]
            name = words[0].rsplit("/", 1)[-1] if words else ""
            if name in ALL_ARGS or name == "sed" and any(w.startswith(("-i", "--in-place")) for w in words):
                out += at(args)
                gone += at(args if name == "rm" else args[:-1] if name == "mv" else [])
            elif name in LAST_ARG:
                out += at(args[-1:])
            elif name in ("cd", "pushd"):
                d = args[0] if len(args) == 1 and not re.search(r"^[~+]|[$%s]" % shape.Q, args[0]) else ""
                base = os.path.join(base or "", d) if d and (base is not None or d.startswith("/")) else None
            seg, op = [], None
        elif kind == "redir":
            # >&N, >&-, <&N carry their fd in the token: no word follows, the next word stays an operand
            op = None if re.search(r"[<>]&(?:\d+-?|-)$", text) else text
        elif kind == "word" and op:
            out += at([text]) if op.endswith((">", ">|", ">&")) else []  # `>& file` is `&> file`
            op = None
        elif kind == "word":
            seg.append((kind, text))
    return [t for t in out if shape.Q not in t], [t for t in gone if shape.Q not in t]


def _git(d, *args):
    """git -C d args: stripped stdout, or None when git exits non-zero (OSError/timeout propagate). Bytes that are
    not UTF-8 (a raw path in `ls-files -z`) decode as os.fsdecode does, so they never raise and fail the hook open."""
    r = subprocess.run(["git", "-C", d, *args], capture_output=True, text=True, encoding="utf-8",
                       errors="surrogateescape", timeout=2)
    return r.stdout.strip() if r.returncode == 0 else None


def _top(p):
    """The nearest dir at or above p holding a .git entry (the checkout's toplevel), or None."""
    while p != os.path.dirname(p):
        if os.path.exists(os.path.join(p, ".git")):
            return p
        p = os.path.dirname(p)
    return None


def _branch(top):
    """Why top's checkout breaks the rule ("on main", "on a detached HEAD"), or None for another branch."""
    branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD")
    if branch is None:
        return "on a detached HEAD"
    default = _git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main"
    return f"on {branch}" if branch == default.removeprefix("origin/") else None


def harness_globs():
    """The harness-paths globs: the `harness-paths:` line under `## harness` in project-rules.md, else
    DEFAULT_PATHS. The one list for this hook and the canary rule of `agent-cli pr`."""
    value = core.rules_line("harness", "harness-paths")
    return core.names(value) if value else list(DEFAULT_PATHS)


def first(paths, cwd, removed=()):
    """Why writing one of paths breaks the rule, for the first that does, or None to allow.

    A path is a harness write when a harness-paths glob matches it or it is a dir above one (`.claude`,
    the checkout root, or, for one of removed, a dir above the checkout of cwd or $CLAUDE_PROJECT_DIR, judged by
    that checkout): removing or replacing that dir rewrites the harness files under it, while a write into a
    dir above the checkout (`cp x ..`) does not. On macOS names compare case-folded.

    Before the branch, a harness path below the checkout root is a copy, refused on any branch, when git ignores
    the matched glob's fixed part (`.claude/skills` for `.claude/skills/**`; the dir itself for a dir above harness
    files), so an ignored __pycache__ under a tracked harness dir is no copy, or when the file is skip-worktree.
    Known gap: an ignored harness dir holding a force-added tracked file is judged not ignored."""
    fold = str.lower if sys.platform == "darwin" else str  # its default file system ignores case
    globs = [fold(g) for g in harness_globs()]
    fixed = [re.split(r"[*?[]", g)[0] for g in globs]  # each glob's text before its first wildcard
    base, verdicts, ignored = os.environ.get("CLAUDE_PROJECT_DIR"), {}, {}
    tops = {_top(os.path.realpath(d)) for d in (cwd, base) if d} - {None}
    def real(t):
        return os.path.realpath(os.path.join(cwd, os.path.expandvars(os.path.expanduser(t))))
    todo, gone = list(dict.fromkeys(map(real, paths))), set(map(real, removed))
    for p in todo:  # removing a dir above a known checkout's root also removes that root
        if p in gone:
            todo += [k for k in tops if fold(k).startswith(fold(os.path.join(p, ""))) and k not in todo]
        top = _top(p)
        root = top or (os.path.realpath(base) if base else None)
        rel = fold(os.path.relpath(p, root)) if root else None
        if not rel or not (rel == "." or any(glob_match(g, rel) for g in globs)
                           or any(f.startswith(rel + "/") for f in fixed)):
            continue
        if top is None:
            return OUTSIDE
        if top not in verdicts:
            if len(verdicts) == MAX_CHECKOUTS:
                return f"in more than {MAX_CHECKOUTS} checkouts"
            listed = (_git(top, "ls-files", "-vz") or "").split("\0")
            verdicts[top] = _branch(top), {fold(e[2:]) for e in listed if e[:2] in ("S ", "s ")}
        branch, skipped = verdicts[top]
        if rel != ".":
            raw = os.path.relpath(p, top)  # real case: git compares names itself
            f = next((f for g, f in zip(globs, fixed) if glob_match(g, rel)), raw)  # raw: a dir above harness files
            # x: the glob's fixed part or that dir, so few distinct values (no cap); a leading wildcard has none
            x = raw[:len(f.rstrip("/"))]
            if x and (top, x) not in ignored:
                ignored[top, x] = _git(top, "check-ignore", "-q", "--", x) is not None
            if x and ignored[top, x]:
                return IGNORED
            if not os.path.isdir(p) and fold(raw) in skipped:
                return SKIPPED
        if branch:
            return branch
    return None
