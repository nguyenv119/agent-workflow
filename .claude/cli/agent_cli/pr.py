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
Required checks belong to the PR's repo. The plain lines are the cwd repo's: for a PR in another repo (--repo that
is not the cwd's origin) only a keyed line applies, else every reported check must pass (REST: the target's branch
protection or rulesets name the required set first, and an unreadable one falls back to every reported check).
REST: with CLAUDE_CODE_REMOTE set (a cloud session, where `gh pr` GraphQL answers 403) or --rest, the PR, its
check runs and commit statuses are read, and a land is merged, through `gh api repos/<owner>/<repo>/...`; the
repo is --repo, else the cwd's origin. Output and exit codes are the same as on the GraphQL path, with one gap: with
no required-checks line REST reads the target's branch protection and rulesets for the required set, GraphQL reads
none (every reported check), so without a keyed line the two verdicts can differ, REST stricter (a protected check
that has not reported is WAITING) or looser (a reported check protection does not require is not waited on).
`required_source` in the status says where the required set came from: keyed, plain, protection, rules or
all-reported.
Harness canary: a PR whose changed files (a rename by both names) match the `harness-paths:` globs under
`## harness` (the harness-edit hook's list) also needs commit status `harness-canary`, which `agent-cli harness
canary --pr N` posts; it is read from its `gh pr checks` row. Success passes, failure or error is FAILED; missing
or pending keeps it WAITING, and once every required check passed blocked_on names the next step (on a fork PR:
that the canary cannot run there). `canary-check: <status>` names that status (`none`/`off`: never needed); without
the line it is needed only when the PR's repo has .claude/HARNESS_VERSION or harness-loop/loop.py at the base ref
(asked of GitHub once per process). Other PRs, and merged or closed ones, never wait for it.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote

from . import core, wait
from .core import Exit

EX_STATUS = "agent-cli pr status 42 --repo <owner>/<repo>"
EX_WAIT = "agent-cli pr wait 42 --cap 40m"
EX_LAND = "agent-cli pr land 42 --method squash --yes"
EX_WAIT_REPO = "agent-cli pr wait 42 --cap 40m --repo <owner>/<repo>"
EX_LAND_REPO = "agent-cli pr land 42 --method squash --yes --repo <owner>/<repo>"
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


def slug_of(text):
    """OWNER/REPO from an owner/repo, HOST/OWNER/REPO or git remote URL (.git and trailing slashes dropped), else None."""
    m = re.search(r"([^/:@\s]+)/([^/\s]+?)(?:\.git)?/*$", (text or "").strip())
    return f"{m[1]}/{m[2]}" if m else None


def origin_repo():
    """owner/name of the cwd's git `origin` (no network, no gh), None when there is none."""
    return slug_of(core.git("remote", "get-url", "origin", check=False))


def config(text=None, repo=None, here=None):
    """{required: [names] or None (= every non-skipped check), advisory: [names], preview: name or None,
    canary: status name or None, canary_auto: True when no canary-check line (needed only on harness repos)}.

    For a PR in repo (None: the cwd's, asked of gh only when ## coordinator has a keyed line), `key[repo]:` beats
    `key:`; when gh cannot name the cwd's repo, a stderr warning says so and the plain lines apply. With here (the
    cwd repo's slug) different from repo, the plain lines are the cwd repo's, not repo's, so only keyed lines apply."""
    if not repo and any(core.rules_scopes("coordinator", k, text) for k in KEYS):
        repo = cwd_repo()
        if not repo:
            sys.stderr.write("warning: gh repo view could not name the cwd's repo, so keyed check lines are "
                             f"ignored; pass --repo, e.g. {EX_STATUS}\n")
    repo = "/".join((repo or "").strip().strip("/").split("/")[-2:]) or None  # HOST/OWNER/REPO -> OWNER/REPO
    foreign = bool(repo and here and here.lower() != repo.lower())
    line = lambda key: (None if foreign and repo.lower() not in core.rules_scopes("coordinator", key, text)  # noqa: E731
                        else core.rules_line("coordinator", key, text, scope=repo))
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


REST_TRIES, REST_NAP = 5, 3  # mergeable_state is computed lazily: null on the first read, so re-read
MERGE_STATES = {"clean", "dirty", "unstable", "blocked", "behind", "has_hooks", "draft"}
RUN_BUCKETS = {"success": "pass", "skipped": "skipping", "neutral": "skipping", "cancelled": "cancel",
               "failure": "fail", "timed_out": "fail", "action_required": "fail", "startup_failure": "fail"}
STATUS_BUCKETS = {"success": "pass", "failure": "fail", "error": "fail"}  # else pending
RUNS_JQ = ('.check_runs[] | {name, status, conclusion, description: (.output.title // ""), '
           'link: (.html_url // .details_url // "")}')
STATUSES_JQ = ".statuses[] | {context, state, description: (.description // \"\"), link: (.target_url // \"\")}"
PROTECTION_JQ = "[.contexts[]?, (.checks[]?.context)] | unique | .[]"
RULES_JQ = '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context'
_protected = {}  # (slug, base) -> protected_checks' answer, so `pr wait` asks once
_noted = set()  # (slug, base) whose unreadable protection was already said on stderr


def jsonl(text, what):
    try:
        return [json.loads(x) for x in text.splitlines() if x.strip()]
    except ValueError:
        raise GhError(f"gh api {what}: not JSON: {text[:200]!r}")


def fetch_rest(n, slug, timeout=120, sleep=time.sleep):
    """(view, checks) of PR n in slug through REST, shaped as fetch() returns them from gh pr view / gh pr checks:
    the pull (re-read while an open PR's mergeable_state is null or unknown), then its head's check runs and
    commit statuses (the preview deployment, harness-canary) as one list of rows with gh's buckets."""
    path = f"repos/{slug}/pulls/{n}"
    try:
        return _fetch_rest(n, slug, path, timeout, sleep)
    except (KeyError, TypeError, IndexError) as e:  # a REST object that lacks a field the shaping reads
        raise GhError(f"gh api {path}: unexpected response shape ({type(e).__name__}: {e})")


def _fetch_rest(n, slug, path, timeout, sleep):
    for i in range(REST_TRIES):
        pull = jsonl(api([path], timeout), path)[0]
        if pull["state"] != "open" or pull.get("mergeable_state") not in (None, "unknown"):
            break
        if i < REST_TRIES - 1:
            sleep(REST_NAP)
    base, head = pull["base"], pull["head"]
    state = "MERGED" if pull.get("merged") or pull.get("merged_at") else pull["state"].upper()
    ms = pull.get("mergeable_state")
    view = {"baseRefName": base["ref"], "headRefOid": head["sha"], "state": state, "url": pull["html_url"],
            "isCrossRepository": (head.get("repo") or {}).get("full_name", "").lower() != base["repo"]["full_name"].lower(),
            "mergeStateStatus": ms.upper() if ms in MERGE_STATES else "UNKNOWN"}
    sha = head["sha"]
    runs = jsonl(api(["--paginate", f"repos/{slug}/commits/{sha}/check-runs?per_page=100", "--jq", RUNS_JQ], timeout),
                 "check-runs")
    statuses = jsonl(api(["--paginate", f"repos/{slug}/commits/{sha}/status?per_page=100", "--jq", STATUSES_JQ],
                         timeout), "status")
    checks = [{"name": r["name"], "state": (r["conclusion"] or r["status"]).upper(), "description": r["description"],
               "link": r["link"], "bucket": "pending" if r["status"] != "completed"
               else RUN_BUCKETS.get(r["conclusion"], "pending")} for r in runs]
    checks += [{"name": c["context"], "state": c["state"].upper(), "description": c["description"], "link": c["link"],
                "bucket": STATUS_BUCKETS.get(c["state"], "pending")} for c in statuses]
    return view, checks


def protected_checks(slug, base, timeout=120):
    """(names, source): the check names the repo requires on branch base, and where they came from ("protection":
    classic branch protection, else "rules": a ruleset; both are unioned), read through REST. (None, None) when
    neither names any or neither can be read (a token without admin gets 403), so the caller falls back to every
    reported check; both unreadable also prints one stderr note per (slug, base). The answer is kept per (slug,
    base) so `pr wait` asks once, but only when every read answered or was refused with 403/404: a timeout or 5xx
    is asked again on the next poll."""
    key = (slug.lower(), base)
    if key in _protected:
        return _protected[key]
    found, readable, keep = {}, 0, True
    for name, args, jq in (("protection", [f"repos/{slug}/branches/{quote(base)}/protection/required_status_checks"],
                            PROTECTION_JQ),
                           ("rules", ["--paginate", f"repos/{slug}/rules/branches/{quote(base)}?per_page=100"],
                            RULES_JQ)):
        try:
            out = api([*args, "--jq", jq], timeout, missing=True)
        except GhError as e:  # unreadable: the other source, else the fallback
            keep = keep and "HTTP 403" in str(e)
            continue
        readable += 1
        found[name] = [x for x in (out or "").split("\n") if x]
    if not readable and key not in _noted:
        _noted.add(key)
        sys.stderr.write(f"note: cannot read {slug}'s branch protection or rulesets for {base}, so every reported "
                         "check must pass; name the required ones with a required-checks[<owner>/<repo>]: line\n")
    names = list(dict.fromkeys(x for v in found.values() for x in v)) or None
    result = (names, next((k for k, v in found.items() if v), None))
    if keep:
        _protected[key] = result
    return result


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


def verdict(n, view, checks, cfg, canary=None, repo=None, source=None):
    """The status object for one PR: READY, MERGED, CLOSED, FAILED, CONFLICT, NO_CHECKS or WAITING.

    A WAITING PR whose required checks all passed carries blocked_on: what still holds the merge. A required canary
    (canary_of) is judged by its row in checks (state and description copied into it), not as a CI check: failed or
    errored it is FAILED; missing or pending it keeps the PR WAITING, and blocked_on names it before the merge state
    (the command to post it, with --repo when repo is given; a fork PR, where it cannot run; or the status to post
    when canary-check renamed it). source (default: plain, or all-reported without a required list) is
    required_source in the result."""
    gate, hit = "success", None
    source = source or ("all-reported" if cfg["required"] is None else "plain")
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
           "missing_required": missing, "required_source": source, "preview_url": preview or None, "canary": canary}
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


def judge(n, repo, cfg, timeout=120, rest=False, source=None):
    """(view, status) of PR n: one fetch, its canary (open PRs only), the verdict. With rest, repo is the
    OWNER/REPO slug the REST paths need, and with no required-checks line the target's own protection names them."""
    view, checks = fetch_rest(n, repo, timeout) if rest else fetch(n, repo, timeout)
    if rest and cfg["required"] is None:
        names, source = protected_checks(repo, view["baseRefName"], timeout)
        cfg = {**cfg, "required": names}
    canary = canary_of(view, cfg, timeout) if view["state"] == "OPEN" else None
    return view, verdict(n, view, checks, cfg, canary, repo, source)


def target(a, example):
    """(repo, rest, cfg, source) for the command: rest is --rest or CLAUDE_CODE_REMOTE; REST names the repo as
    OWNER/REPO (--repo, else the cwd's origin; none = exit 2); cfg is its rules (plain lines only for the cwd's
    repo); source is "keyed" or "plain" when a required-checks line fixed the required set, else None."""
    rest = bool(a.rest or os.environ.get("CLAUDE_CODE_REMOTE"))
    here = origin_repo() if a.repo or rest else None
    slug = (slug_of(a.repo) or a.repo) if a.repo else here if rest else None
    if rest and not slug:
        core.die(Exit.REFUSED, "REST needs the PR's repo: no --repo and the cwd has no git origin", example)
    cfg = config(repo=slug, here=here)
    keyed = bool(slug) and slug.lower() in core.rules_scopes("coordinator", "required-checks")
    source = None if cfg["required"] is None else "keyed" if keyed else "plain"
    return (slug if rest else a.repo), rest, cfg, source


def status_or_die(a, example):
    """(view, status, repo, rest): the PR judged once; repo and rest as target() resolved them, for a later merge."""
    repo, rest, cfg, source = target(a, example)
    try:
        view, s = judge(a.pr, repo, cfg, rest=rest, source=source)
    except GhError as e:
        core.die(Exit.BLOCKED, str(e), example)
    return view, s, repo, rest


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
    repo, rest, cfg, source = target(a, EX_WAIT_REPO)
    prs = list(dict.fromkeys(a.pr))
    lock = threading.Lock()

    def out(o):
        with lock:
            emit_line(o, a.json)

    def one(n):
        try:
            code = wait_loop(n, lambda left: judge(n, repo, cfg, max(1, left), rest, source)[1], cap, every, grace, out=out)
        except GhError as e:
            if len(prs) == 1:
                core.die(Exit.BLOCKED, str(e), EX_WAIT)
            out({"pr": n, "error": str(e)})
            return Exit.BLOCKED
        if code == Exit.BLOCKED:
            if len(prs) == 1:
                core.die(Exit.BLOCKED, f"no checks appeared within {a.appear_grace}; re-read the PR "
                         "(\"no checks\" is never green)", f"agent-cli pr status {n}")
            out({"pr": n, "error": f"no checks appeared within {a.appear_grace}"})
        return code

    if len(prs) == 1:
        return one(prs[0])
    # Several PRs: each waits in its own thread, so a slow one never holds up the others' verdicts.
    with concurrent.futures.ThreadPoolExecutor(len(prs)) as pool:
        codes = dict(zip(prs, pool.map(one, prs)))
    out({"prs": " ".join(map(str, prs)), "done": " ".join(f"{n}={Exit(c).name}" for n, c in codes.items())})
    return worst(codes.values())


def worst(codes):
    """One exit for several waits: 0 only when every PR is green; else red (1) beats blocked (3) beats cap (4)."""
    for c in (Exit.FAIL, Exit.BLOCKED, Exit.TIMEOUT):
        if c in codes:
            return c
    return max(codes)


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
    view, s, repo, rest = status_or_die(a, EX_LAND_REPO)
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
        if rest:  # sha makes GitHub refuse (409) a head that moved, as --match-head-commit does
            try:
                api(["-X", "PUT", f"repos/{repo}/pulls/{a.pr}/merge", "-f", f"sha={head}", "-f",
                     f"merge_method={a.method}"], 120)
            except GhError as e:
                core.die(Exit.FAIL, f"merge {a.pr}: {e}", EX_LAND)
        else:
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
                        [EX_STATUS, "agent-cli pr status 42 --json",
                         "agent-cli pr status 42 --rest --repo <owner>/<repo>"])
    p.set_defaults(func=pr_status)
    pw = core.subcommand(ps, "wait", "poll status until green (0: READY or MERGED), red (1: FAILED, CONFLICT "
                         "or CLOSED), no checks past the grace (3) or the cap (4); one status per poll (--json: one JSON "
                         "line); a new head restarts the cap. Several PRs are watched in parallel, never one after "
                         "another. Run it with Bash run_in_background",
                         [EX_WAIT, "agent-cli pr wait 41 42 43 --cap 40m",
                          "agent-cli pr wait 42 --cap 40m --every 60 --repo <owner>/<repo>",
                          "agent-cli pr wait 42 --cap 0s --json",
                          "agent-cli pr wait 42 --rest --repo <owner>/<repo>"])
    pw.add_argument("--cap", default="40m", help="give up after this long, exit 4 (default 40m)")
    pw.add_argument("--every", default=str(MIN_EVERY),
                    help=f"seconds between polls, at least {MIN_EVERY} (default {MIN_EVERY})")
    pw.add_argument("--appear-grace", default="5m", help="no checks after this long: exit 3 (default 5m)")
    pw.set_defaults(func=pr_wait)
    for q in (p, pw):
        q.add_argument("--json", action="store_true", help="one JSON object per status (else key: value lines)")
    pl = core.subcommand(ps, "land", "merge a READY PR pinned to its checked head (dry run unless --yes); "
                         "refuses (2) anything else", ["agent-cli pr land 42 --dry-run", EX_LAND,
                                                        "agent-cli pr land 42 --verified-head <sha> --yes",
                                                        "agent-cli pr land 42 --yes --rest --repo <owner>/<repo>"])
    pl.add_argument("--method", choices=("merge", "squash"), default="merge", help="merge method (default merge)")
    pl.add_argument("--verified-head", metavar="SHA",
                    help="the head you verified (7+ hex characters, or a ref that resolves here); a different "
                    "head lands only if its patch-id matches")
    pl.add_argument("--dry-run", action="store_true", help="say what would be merged (the default without --yes)")
    pl.add_argument("--yes", action="store_true", help="really merge")
    pl.set_defaults(func=pr_land)
    pw.add_argument("pr", type=int, nargs="+", metavar="<n>",
                    help="the PR number; name several to wait on them all at once (exit 0 only when all are green)")
    for q in (p, pw, pl):
        if q is not pw:
            q.add_argument("pr", type=int, metavar="<n>", help="the PR number")
        q.add_argument("--rest", action="store_true", help="read (and land) through GitHub REST (gh api) instead of "
                       "gh pr GraphQL; on by itself when CLAUDE_CODE_REMOTE is set, where GraphQL answers 403. "
                       "The repo is --repo, else the cwd's git origin; with no required-checks line the repo's "
                       "branch protection and rulesets name the required checks")
        q.add_argument("--repo", metavar="OWNER/NAME", help="the GitHub repo, when not the cwd's; a "
                       "`required-checks[OWNER/NAME]:` line under ## coordinator in project-rules.md (likewise "
                       "advisory-checks, preview-check, canary-check) replaces the plain line for PRs in that repo "
                       "(without --repo, the cwd's repo per gh; plain lines if gh cannot tell). The plain "
                       "project-rules lines apply only to the cwd's repo (its git origin): a PR in another repo "
                       "needs a keyed line, else every reported check must pass (--rest: its branch protection "
                       "and rulesets name them)")
