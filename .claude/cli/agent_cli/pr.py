"""`agent-cli pr`: is this PR green (status), wait until it is (wait), merge it safely (land).

Green is the coordinator's CI-green gate: every required check has bucket `pass` and mergeStateStatus is
CLEAN, HAS_HOOKS (clean, with merge hooks) or UNSTABLE (only non-required checks are off then).
A required check in bucket `skipping` counts as passing, as in GitHub branch protection, so a
path-filtered required check never hangs a wait until the cap.
Check names come from optional lines under `## coordinator` in .claude/project-rules.md:
`required-checks: a, b`, `advisory-checks: c` and `preview-check: <name>`.
A line keyed by repo, e.g. `required-checks[<owner>/<repo>]: x, y`, replaces the plain line of the same key
for PRs in that repo (--repo, else the repo gh targets from the cwd; slug case ignored); when gh cannot name
the cwd's repo, a stderr warning says so and the plain lines apply.
Harness canary: a PR whose changed files (a rename by both names) match the `harness-paths:` globs under
`## harness` (the harness-edit hook's list) also needs commit status `harness-canary`, which `agent-cli harness
canary --pr N` posts; it is read from its `gh pr checks` row. Success passes, failure or error is FAILED; missing
or pending keeps it WAITING, and once every required check passed blocked_on names the next step (on a fork PR:
that the canary cannot run there). `canary-check: <status>` names that status (`none`/`off`: never needed); without
the line it is needed only when the PR's repo has .claude/HARNESS_VERSION or harness-loop/loop.py at the base ref
(asked of GitHub once per process). Other PRs, and merged or closed ones, never wait for it.
"""
import json
import re
import subprocess
import sys
import time
from urllib.parse import quote

from . import core, wait
from .core import Exit

EX_STATUS = "agent-cli pr status 42 --repo <owner>/<repo>"
EX_WAIT = "agent-cli pr wait 42 --cap 40m"
EX_LAND = "agent-cli pr land 42 --method squash --yes"
MIN_EVERY = 30  # seconds; GitHub's API budget (5,000 req/h) is shared by every session
EXIT = {"READY": Exit.OK, "MERGED": Exit.OK, "FAILED": Exit.FAIL, "CONFLICT": Exit.FAIL, "CLOSED": Exit.FAIL,
        "WAITING": Exit.PARTIAL, "NO_CHECKS": Exit.PARTIAL}
GREEN_STATES = ("CLEAN", "HAS_HOOKS", "UNSTABLE")


class GhError(Exception):
    pass


KEYS = ("required-checks", "advisory-checks", "preview-check", "canary-check")
CANARY = "harness-canary"  # the status `agent-cli harness canary` posts
HARNESS_MARKERS = (".claude/HARNESS_VERSION", "harness-loop/loop.py")  # an installing repo; the harness source
FILES_JQ = ".[] | .filename, (.previous_filename // empty)"
_harness_repos = {}  # (slug, base) -> bool, so `pr wait` asks once


def cwd_repo():
    """owner/name of the repo gh targets from the cwd, None when gh cannot tell."""
    try:
        r = gh(["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"], None, timeout=15)
    except GhError:
        return None
    return None if r.returncode else r.stdout.strip() or None


def config(text=None, repo=None):
    """{required: [names] or None (= every non-skipped check), advisory: [names], preview: name or None,
    canary: status name or None, canary_auto: True when no canary-check line (needed only on harness repos)}.

    For a PR in repo (None: the cwd's, asked of gh only when ## coordinator has a keyed line), `key[repo]:` beats
    `key:`; when gh cannot name the cwd's repo, a stderr warning says so and the plain lines apply."""
    if not repo and any(core.rules_scopes("coordinator", k, text) for k in KEYS):
        repo = cwd_repo()
        if not repo:
            sys.stderr.write("warning: gh repo view could not name the cwd's repo, so keyed check lines are "
                             f"ignored; pass --repo, e.g. {EX_STATUS}\n")
    repo = "/".join((repo or "").strip().strip("/").split("/")[-2:]) or None  # HOST/OWNER/REPO -> OWNER/REPO
    line = lambda key: core.rules_line("coordinator", key, text, scope=repo)  # noqa: E731
    req, adv, can = line("required-checks"), line("advisory-checks"), line("canary-check")
    return {"required": core.names(req) if req is not None else None, "advisory": core.names(adv or ""),
            "preview": line("preview-check") or None,
            "canary": CANARY if can is None else None if can.lower() in ("", "none", "off") else can,
            "canary_auto": can is None}


def gh(args, repo, timeout=120):
    try:
        return subprocess.run(["gh", *args, *(["--repo", repo] if repo else [])],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        raise GhError(f"gh {' '.join(args[:2])}: {e}")


def fetch(n, repo, timeout=120):
    """(view, checks) from one `gh pr view` and one `gh pr checks`, each killed after timeout seconds;
    gh's "no checks reported" -> []."""
    r = gh(["pr", "view", str(n), "--json", "baseRefName,headRefOid,isCrossRepository,mergeStateStatus,state,url"],
           repo, timeout)
    try:
        if r.returncode:
            raise ValueError(r.stderr.strip())
        view = json.loads(r.stdout)
    except ValueError as e:
        raise GhError(f"gh pr view {n}: {e}")
    r = gh(["pr", "checks", str(n), "--json", "name,bucket,state,description,link"], repo, timeout)
    if "no checks reported" in r.stderr:
        return view, []
    try:
        return view, json.loads(r.stdout)
    except ValueError:
        raise GhError(f"gh pr checks {n}: {r.stderr.strip() or 'no JSON on stdout'}")


def api(args, timeout, missing=False):
    """stdout of `gh api args` (REST; the repo is in the path, so no --repo); with missing, None on HTTP 404.
    Any other failure raises GhError."""
    r = gh(["api", *args], None, timeout)
    if r.returncode == 0:
        return r.stdout
    if missing and "HTTP 404" in r.stderr:
        return None
    raise GhError(f"gh api {next(x for x in args if x.startswith('repos/'))}: {r.stderr.strip()}")


def harness_repo(slug, base, timeout):
    """True when slug has a HARNESS_MARKERS file at ref base; asked of GitHub once per (slug, base)."""
    key = (slug.lower(), base)
    if key not in _harness_repos:
        _harness_repos[key] = any(api([f"repos/{slug}/contents/{f}?ref={quote(base)}", "--jq", ".sha"], timeout,
                                      missing=True) is not None for f in HARNESS_MARKERS)
    return _harness_repos[key]


def canary_of(view, cfg, timeout=120):
    """None when cfg names no canary, else {context, required, paths (first 5 changed harness paths), state and
    description (None here; verdict fills them from the check row named context), reason (why not required, else
    None)}."""
    if not cfg["canary"]:
        return None
    from .harness import glob_match  # harness -> canary -> pr: a top-level import would cycle
    from .harness_edit import harness_globs
    slug, n = re.search(r"([^/]+/[^/]+)/pull/(\d+)", view["url"]).groups()
    files = api(["--paginate", f"repos/{slug}/pulls/{n}/files?per_page=100", "--jq", FILES_JQ], timeout).splitlines()
    globs = harness_globs()
    paths = [f for f in dict.fromkeys(files) if any(glob_match(g, f) for g in globs)]
    out = {"context": cfg["canary"], "required": False, "paths": paths[:5], "state": None, "description": None,
           "reason": "no harness path changed"}
    if paths and cfg["canary_auto"] and not harness_repo(slug, view["baseRefName"], timeout):
        out["reason"] = "not a harness repo"
    elif paths:
        out.update(required=True, reason=None)
    return out


def verdict(n, view, checks, cfg, canary=None, repo=None):
    """The status object for one PR: READY, MERGED, CLOSED, FAILED, CONFLICT, NO_CHECKS or WAITING.

    A WAITING PR whose required checks all passed carries blocked_on: what still holds the merge. A required canary
    (canary_of) is judged by its row in checks (state and description copied into it), not as a CI check: failed or
    errored it is FAILED; missing or pending it keeps the PR WAITING, and blocked_on names it before the merge state
    (the command to post it, with --repo when repo is given; a fork PR, where it cannot run; or the status to post
    when canary-check renamed it)."""
    gate, hit = "success", None
    if canary and canary["required"]:
        hit = next((c for c in checks if c["name"] == canary["context"]), None)
        checks = [c for c in checks if c is not hit]  # judged as the canary below, never as a CI check
        if cfg["required"] is not None:  # a required-checks line naming it too must not report it missing
            cfg = {**cfg, "required": [x for x in cfg["required"] if x != canary["context"]]}
        gate = hit and {"pass": "success", "fail": "failure", "cancel": "failure"}.get(hit["bucket"], "pending")
        canary = {**canary, "state": hit and hit["state"].lower(), "description": hit and hit["description"]}
    row = lambda c: {"name": c["name"], "bucket": c["bucket"], "link": c.get("link", "")}  # noqa: E731
    adv = set(cfg["advisory"])
    if cfg["required"] is None:
        required, missing = [c for c in checks if c["bucket"] != "skipping" and c["name"] not in adv], []
    else:
        required = [c for c in checks if c["name"] in cfg["required"]]
        missing = [x for x in cfg["required"] if x not in {c["name"] for c in required}]
    ms = view["mergeStateStatus"]
    passed = not missing and {c["bucket"] for c in required} <= {"pass", "skipping"}
    if view["state"] in ("MERGED", "CLOSED"):
        v = view["state"]
    elif not checks and not hit:
        v = "NO_CHECKS"
    elif any(c["bucket"] in ("fail", "cancel") for c in required) or gate == "failure":
        v = "FAILED"
    elif ms == "DIRTY":
        v = "CONFLICT"
    elif passed:
        v = "READY" if ms in GREEN_STATES and gate == "success" else "WAITING"
    else:
        v = "WAITING"
    preview = next((c.get("link") for c in checks if cfg["preview"] and c["name"] == cfg["preview"]), None)
    out = {"pr": n, "url": view["url"], "head": view["headRefOid"], "merge_state": ms, "verdict": v,
           "required": [row(c) for c in required], "advisory": [row(c) for c in checks if c["name"] in adv],
           "missing_required": missing, "preview_url": preview or None, "canary": canary}
    if v == "WAITING" and passed:
        ctx = canary and canary["context"]
        if gate == "pending":
            out["blocked_on"] = f"{ctx} pending" + (f": {canary['description']}" if canary["description"] else "")
        elif gate is None and view["isCrossRepository"]:  # agent-cli harness canary refuses forks
            slug = re.search(r"([^/]+/[^/]+)/pull/", view["url"])[1]
            out["blocked_on"] = (f"{ctx} cannot run on a fork PR: push the branch to this repo and open the PR from "
                                 f"it, or set canary-check[{slug}]: none")
        elif gate is None and ctx == CANARY:
            out["blocked_on"] = f"{ctx} not posted: run agent-cli harness canary --pr {n}" + (
                f" --repo {repo}" if repo else "")
        elif gate is None:
            out["blocked_on"] = f"{ctx} not posted on {view['headRefOid'][:12]}: post that commit status"
        else:
            out["blocked_on"] = ms
    return out


def emit_line(obj, json_mode=True):
    """house-rules "--json": obj as one JSON line, else `key: value` lines whose lists and nulls are JSON;
    flushed, so a background log shows each poll as it happens."""
    if not json_mode:
        obj = {k: json.dumps(v) if v is None or isinstance(v, (list, dict)) else v for k, v in obj.items()}
    core.emit(obj, json_mode)
    sys.stdout.flush()


def wait_loop(n, poll, cap, every, grace, clock=time.monotonic, sleep=time.sleep, out=emit_line):
    """Poll (poll(seconds_left) -> status) through wait.poll until a final verdict; exit code.

    A new head restarts the cap (wait.RESTART) and the appear-grace; NO_CHECKS for grace seconds is
    BLOCKED. A gh error after the first poll is printed and polling goes on (a first-poll error raises)."""
    seen = {"head": None, "since": clock()}

    def check(left):
        try:
            s = poll(left)
        except GhError as e:
            if seen["head"] is None:
                raise
            out({"pr": n, "error": str(e)})
            return None
        moved = seen["head"] is not None and s["head"] != seen["head"]
        if moved:
            out({"pr": n, "event": "head_changed", "from": seen["head"], "to": s["head"]})
            seen["since"] = clock()
        seen["head"] = s["head"]
        out(s)
        if EXIT[s["verdict"]] != Exit.PARTIAL:
            return EXIT[s["verdict"]]
        if s["verdict"] == "NO_CHECKS" and clock() - seen["since"] >= grace:
            return Exit.BLOCKED
        return wait.RESTART if moved else None

    got = wait.poll(check, every, cap, clock, sleep)
    return Exit.TIMEOUT if got is None else got


def judge(n, repo, cfg, timeout=120):
    """(view, status) of PR n: one fetch, its canary (open PRs only), the verdict."""
    view, checks = fetch(n, repo, timeout)
    canary = canary_of(view, cfg, timeout) if view["state"] == "OPEN" else None
    return view, verdict(n, view, checks, cfg, canary, repo)


def status_or_die(a, example):
    cfg = config(repo=a.repo)
    try:
        return judge(a.pr, a.repo, cfg)
    except GhError as e:
        core.die(Exit.BLOCKED, str(e), example)


def pr_status(a):
    s = status_or_die(a, EX_STATUS)[1]
    emit_line(s, a.json)
    return EXIT[s["verdict"]]


def duration(text, flag):
    try:
        return core.parse_duration(text)
    except ValueError as e:
        core.die(Exit.REFUSED, f"{flag}: {e}", EX_WAIT)


def pr_wait(a):
    cap, every, grace = duration(a.cap, "--cap"), duration(a.every, "--every"), duration(a.appear_grace,
                                                                                         "--appear-grace")
    if every < MIN_EVERY:
        core.die(Exit.REFUSED, f"--every {a.every}: poll at most every {MIN_EVERY}s (GitHub API budget)",
                 EX_WAIT + " --every 60")
    cfg = config(repo=a.repo)
    try:
        code = wait_loop(a.pr, lambda left: judge(a.pr, a.repo, cfg, max(1, left))[1],
                         cap, every, grace, out=lambda o: emit_line(o, a.json))
    except GhError as e:
        core.die(Exit.BLOCKED, str(e), EX_WAIT)
    if code == Exit.BLOCKED:
        core.die(Exit.BLOCKED, f"no checks appeared within {a.appear_grace}; re-read the PR "
                 "(\"no checks\" is never green)", f"agent-cli pr status {a.pr}")
    return code


def patch_id(base, sha):
    """`git patch-id --stable` of the PR diff base...sha ('' for an empty diff), None when git cannot tell."""
    diff = core.git("diff", f"{base}...{sha}", check=False, strip=False)
    out = core.git("patch-id", "--stable", input=diff, check=False) if diff is not None else None
    return None if out is None else (out.split() or [""])[0]


def same_patch(n, base_ref, verified, head):
    """Port of the patch-id re-check in step 3 of the pstack shipping playbook (github.com/cursor/plugins,
    pstack/skills/poteto-mode/playbooks/shipping.md, MIT):
    after a rebase, the verified head and the new head must carry the same change against the base."""
    base = f"origin/{base_ref}"
    if any(core.git("cat-file", "-e", f"{x}^{{commit}}", check=False) is None for x in (verified, head, base)):
        core.git("fetch", "-q", "origin", base_ref, f"pull/{n}/head", check=False)
    ids = [patch_id(base, verified), patch_id(base, head)]
    if None in ids:
        core.die(Exit.REFUSED, f"cannot compare patch-ids of {verified[:12]} and {head[:12]} against {base} "
                 "here; run land from a clone of the PR's repo, or re-verify the new head yourself "
                 f"(then land with --verified-head {head[:12]})", f"git fetch origin pull/{n}/head")
    return ids[0] == ids[1]


def verified_sha(text):
    """--verified-head as given when it is 7-40 hex characters, else the full sha git resolves it to;
    anything shorter or unresolvable is refused, since a 1-6 character prefix matches far too many heads."""
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
        return text.lower()
    sha = core.git("rev-parse", "--verify", "-q", f"{text}^{{commit}}", check=False)
    if not sha:
        core.die(Exit.REFUSED, f"--verified-head {text!r}: give at least 7 hex characters of the sha you "
                 "verified (or a ref that resolves to it here)", "agent-cli pr land 42 --verified-head 1a2b3c4d --yes")
    return sha


def pr_land(a):
    if a.verified_head:
        a.verified_head = verified_sha(a.verified_head)
    view, s = status_or_die(a, EX_LAND)
    if s["verdict"] != "READY":
        why = "it is already merged" if s["verdict"] == "MERGED" else f"its verdict is {s['verdict']}, not READY"
        why += f" (blocked on {s['blocked_on']})" if s.get("blocked_on") else ""
        core.die(Exit.REFUSED, f"PR {a.pr} not landed: {why}", f"agent-cli pr status {a.pr}" if s["verdict"] == "MERGED"
                 else f"agent-cli pr wait {a.pr} --cap 40m, then agent-cli pr land {a.pr} --yes")
    head = s["head"]
    if a.verified_head and not head.startswith(a.verified_head) and not same_patch(
            a.pr, view["baseRefName"], a.verified_head, head):
        core.die(Exit.REFUSED, f"rebased content changed since {a.verified_head[:12]} (head is now {head[:12]}); "
                 f"re-verify: review what changed and rerun your checks on {head[:12]} before landing it",
                 f"git diff {a.verified_head[:12]} {head[:12]}")
    dry = a.dry_run or not a.yes
    if not dry:  # pinned to the head judged above; never --auto
        r = gh(["pr", "merge", str(a.pr), f"--{a.method}", "--match-head-commit", head], a.repo)
        if r.returncode:
            core.die(Exit.FAIL, f"gh pr merge {a.pr}: {r.stderr.strip()}", EX_LAND)
    emit_line({"pr": a.pr, "url": s["url"], "head": head, "method": a.method,
               "landed": "would merge" if dry else "merged"})
    return Exit.OK


def register(sub):
    _, ps = core.group(sub, "pr", "PR checks: green verdict (status), wait for it (wait), merge safely (land)",
                       [EX_STATUS, EX_WAIT, "agent-cli pr land 42 --dry-run"])
    p = core.subcommand(ps, "status", "one verdict: READY, FAILED, WAITING, CONFLICT, NO_CHECKS, MERGED or CLOSED "
                        "(exit 0 READY/MERGED, 1 FAILED/CONFLICT/CLOSED, 5 WAITING/NO_CHECKS). READY = every "
                        "required check passes (a skipped one counts) and mergeStateStatus is CLEAN, HAS_HOOKS or "
                        "UNSTABLE; a WAITING PR with all required checks passed names what holds it in blocked_on (the "
                        "canary step, else the merge state). An open PR changing harness-paths (## harness) also needs "
                        "commit status harness-canary in a repo "
                        "with .claude/HARNESS_VERSION or harness-loop/loop.py (canary-check under ## coordinator "
                        "names the status and needs it in any repo; none turns it off)",
                        [EX_STATUS, "agent-cli pr status 42 --json"])
    p.set_defaults(func=pr_status)
    pw = core.subcommand(ps, "wait", "poll status until green (0: READY or MERGED), red (1: FAILED, CONFLICT "
                         "or CLOSED), no checks past the grace (3) or the cap (4); one status per poll (--json: one JSON "
                         "line); a new head restarts the cap. Run it with Bash run_in_background",
                         [EX_WAIT, "agent-cli pr wait 42 --cap 40m --every 60 --repo <owner>/<repo>",
                          "agent-cli pr wait 42 --cap 0s --json"])
    pw.add_argument("--cap", default="40m", help="give up after this long, exit 4 (default 40m)")
    pw.add_argument("--every", default="60", help=f"seconds between polls, at least {MIN_EVERY} (default 60)")
    pw.add_argument("--appear-grace", default="5m", help="no checks after this long: exit 3 (default 5m)")
    pw.set_defaults(func=pr_wait)
    for q in (p, pw):
        q.add_argument("--json", action="store_true", help="one JSON object per status (else key: value lines)")
    pl = core.subcommand(ps, "land", "merge a READY PR pinned to its checked head (dry run unless --yes); "
                         "refuses (2) anything else", ["agent-cli pr land 42 --dry-run", EX_LAND,
                                                        "agent-cli pr land 42 --verified-head <sha> --yes"])
    pl.add_argument("--method", choices=("merge", "squash"), default="merge", help="merge method (default merge)")
    pl.add_argument("--verified-head", metavar="SHA",
                    help="the head you verified (7+ hex characters, or a ref that resolves here); a different "
                    "head lands only if its patch-id matches")
    pl.add_argument("--dry-run", action="store_true", help="say what would be merged (the default without --yes)")
    pl.add_argument("--yes", action="store_true", help="really merge")
    pl.set_defaults(func=pr_land)
    for q in (p, pw, pl):
        q.add_argument("pr", type=int, metavar="<n>", help="the PR number")
        q.add_argument("--repo", metavar="OWNER/NAME", help="the GitHub repo, when not the cwd's; a "
                       "`required-checks[OWNER/NAME]:` line under ## coordinator in project-rules.md (likewise "
                       "advisory-checks, preview-check, canary-check) replaces the plain line for PRs in that repo "
                       "(without --repo, the cwd's repo per gh; plain lines if gh cannot tell)")
