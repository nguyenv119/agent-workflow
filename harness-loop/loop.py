#!/usr/bin/env python3
"""The harness loop: one improvement round at a time, checked and scored by scripts, never by Claude.

    loop.py round --tasks <manifest> [--improver-cmd CMD] [--no-screen] [--proposals FILE] [--budget-usd X]
                  [--improver-budget-usd Y] [--sidecheck <manifest> --sidecheck-addon <dir>
                  [--sidecheck-env docker|daytona] [--sidecheck-budget-usd Z]]
    loop.py monthly [--budget-usd X]
    loop.py exam --pr N --tasks <manifest> [--budget-usd X] [--check-only]
                 [--subscription-only [--rerun-on-fail 1] [--post-status [--status-repo R]]]
    loop.py exam --candidate-installed DIR --baseline-installed DIR --tasks <manifest> --subscription-only
                 [--rerun-on-fail 1] [--post-status --status-repo owner/name --status-sha SHA]
    loop.py sync
    loop.py exam-route [--subscription-only]
    loop.py guard <commit> [--base <ref>]
    loop.py leak [--diff <ref>] [--body <file>] [--redact]
    loop.py budget --est X --what S
    loop.py book --kind K --usd X [--note S]
    loop.py port --from <installed .claude dir or its project root> --source-label S [--base B] [--draft]

round: make worktree $HARNESS_LOOP_HOME/wt/<n> on branch harness-loop/<n> off origin/main;
write rounds/<n>/message.txt (paths only; a `proposals:` line when --proposals names a file that
exists); hash, in this process's memory only, the main screen run it will reuse, history.jsonl, queue.json, spend.jsonl, and this checkout's state
(git status, HEAD, every ref but the round's, every file under harness-loop/, the shared
.git config/hooks/info and the user's git config); run the
improver (default: `claude -p --model opus` on the model route, see Model route); re-check all of it
and drop the round on any change; run the checks (one commit; only .claude/** or AGENTS.md;
no EXAM_MODE; Exam mode section untouched; the gate guard; leak scan; project check; proposal.md
with an Idea: line and >= 2 try folders on disk; load check); then, unless --no-screen, screen the
candidate against main with run_exam.py --screen and compare.py --screen-rule, push, and open
one PR whose score block is written here from compare's output. Screen pass: ready PR, queued.
Screen fail: draft PR titled "[screen fail] ...". The loop never merges.
Side check (--sidecheck, a private task set run with its add-on): only after a screen pass, main
and the candidate sit it; the score block gets a "side check" line with guard=pass iff the
candidate solves at least as many as main and neither side run was cut short. A guard fail is a
guard, not a score: draft PR titled "[side check fail] ...", not queued. Side runs go under
<side manifest dir>/loop-home/runs, never $HARNESS_LOOP_HOME/runs (the improver reads that), and no
side run name (it carries the private set name) reaches the PR, history.jsonl or spend.jsonl.

Early stop: every candidate screen (round and exam) runs with run_exam.py --early-stop-vs <main's run>:
tasks go cheapest first by main's cost, the first runs alone as a cheap dry run, and the run stops as soon as
the screen rule can no longer pass (its score block gets an "early stop: <reason>" line).

monthly: first rebuilds each queued candidate on current main (restack: git merge-tree onto main, one
commit kept by refs/harness-loop/restack/<cand>); a conflict is "stale, re-propose", recorded, dequeued
and written into its PR with no spend. Then a full exam (--tries 2, no --screen) on main twice and each
restacked candidate; compare.py --margin-from; rewrites each PR's score block (screen, side check and
full lines, verdict). sync: reads PR states with gh and records merged/closed. guard: the round's gate
guard on <commit>^..<commit> (exit 1 and "rejected: weakens gates" on rejection).

exam: screens one open PR, hand-made or not. Path gate first (free): a diff (main...head) touching
nothing /work loads (WORK_LOADED) and no skill's or agent's frontmatter gets "exam: not needed (<reason>)"
in its score block and spends nothing. Otherwise the free checks, before the model route or the budget is
asked for: the head is restacked on main (a conflict: "stale, rebase the PR onto main"), the load check runs on
that restacked commit (fail: "dry run fail"), and either fail is written into the block with no spend;
the gate guard is advisory (its reasons go in the block). --check-only runs the same checks, prints the
verdicts and edits nothing. Then, on the model route and inside the weekly cap: main's screen run is reused or
made, and the restacked commit is screened with early stop like a round's; the block is written into the
PR body. Spend is booked as round pr-<N>. The restack ref is deleted once the exam is done (monthly too).
The exam never pushes or merges.

Canary (exam --subscription-only; agent-cli `harness canary` runs it with tasks/canary-2026-10.json): the exam on the
plan token only, never a paid route. In order: CLAUDE_CODE_REMOTE set -> exit 5 "could not run: the canary is
laptop-only" before any gh, git or preflight call; with --pr, a fork PR (gh isCrossRepository) -> exit 5 "could not run:
fork PR, not run with the plan token" (the sandbox holds the token and runs the PR's harness); neither posts a status.
Then the path gate ("not needed" posts success, makes no run), the free checks, the subscription preflight ALONE (no
working token: exit 4 naming the credential, never a value; EXAM_ROUTES is never walked), `docker info` (via
EXAM_DOCKER; down: exit 5), the task fetch (tasks/fetch_cache.py <manifest>, or LOOP_FETCH_CACHE; only now, so a "not
needed" PR downloads nothing; failed: exit 5), the screen and, on a screen fail, --rerun-on-fail more candidate runs
(each a fresh run dir; the verdict is the last compare; a rerun whose tries all errored: exit 5). --post-status writes
commit status `harness-canary` on the PR head (or --status-sha) of GH_REPO (or --status-repo): "not needed (no loaded
file)", "pass solved B/n vs main A/n", "fail (rerun) solved B/n vs main A/n", or state error "could not run: <reason>".
A failed `gh pr edit` of the score block still posts the status, then exits 5 naming the kept body file.
Installed mode (--candidate-installed/--baseline-installed: two installed .claude + AGENTS.md trees, run with
run_exam.py --installed) has no PR and no path gate (agent-cli gates first): a tree holding a .env, .env.*, *.bak or
settings.local.json under .claude, or no file under .claude, exits 2 (install_harness.installed_files; in a git
work tree only files git does not ignore count); identical trees (same installed_sha256) post "not needed (installed trees identical)"; the candidate tree's
free checks (tree_problems) fail as "dry run fail (...)", exit 1. main's run is reused when a booked, sealed baseline
has the same installed_sha256 (a --pr exam never reuses an installed-tree run). A tree with an add-on refuses the
default ~/.harness-loop (exit 2); agent-cli sets HARNESS_LOOP_HOME=~/.harness-loop-private/<repo> for every installing
repo. The token: CLAUDE_CODE_OAUTH_TOKEN, else the file HARNESS_EXAM_ENV_FILE names (exam/envfile.py; only the
preflight and run_exam subprocesses get it).
Canary exit codes (exam --subscription-only and agent-cli harness canary): 0 pass or not needed; 1 fail (after the
rerun), stale, or a free check failed; 2 usage error or refused config; 3 (agent-cli only) gh could not read the PR;
4 paused (usage limit) or no subscription credential; 5 could not run (laptop-only, fork PR, gh or git failed, the PR
moved, Docker down, the fetch failed, both runs or the rerun errored, the exam crashed, the PR body edit failed).

Outcomes in history.jsonl: queued (screen pass, PR open), screen-fail (draft PR), side-fail
(screen pass, side check guard fail, draft PR), dropped (a
check failed), rejected (the gate guard: the change weakens a gate, reviewer or test),
checked (--no-screen, all checks pass), paused (a usage limit: see Model route), error, merged, closed.

Model route (owner decision, 15 Oct 2026): HARNESS_MODEL_ACCESS picks how every paid claude call runs,
the improver, the project check and the exam's /work runs alike. subscription (the default, also when
unset): the Claude Code subscription. Host calls use this machine's `claude` login, or
CLAUDE_CODE_OAUTH_TOKEN when set. bedrock: CLAUDE_CODE_USE_BEDROCK=1 and Bedrock keys, opt-in only.
Nothing switches a host call's route on a failure. Every ledger row records its route; a subscription row books usd 0
and keeps claude's total_cost_usd as notional_usd, so the weekly cap counts only money and `budget`
shows the subscription use. A usage limit (claude's rate_limit error, a 429, "You've hit your session
limit") in the improver, the project check or an exam try ends the round "paused": nothing more starts,
no screen verdict, no PR, exit 4; exam and monthly exit 4 the same way. Run it again once the limit resets.

Exam route (owner decision, 16 Oct 2026): the exam's sandboxes cannot read the keychain login, so on the
subscription route the exam takes the first credential that answers a one-call Haiku preflight (run with a
fresh CLAUDE_CONFIG_DIR and only that credential; --subscription-only: the token alone, exit 4 without it):
CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`, booked
notional), then Bedrock (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, real money), then ANTHROPIC_API_KEY (real
money). Only the exam moves: the improver and the project check stay on the subscription. A paid exam's budget
counts against the weekly cap, and its ledger rows carry its route. A token that hits a usage limit, times out or
fails for any reason but bad auth at the preflight pauses (exit 4), never falls through to a paid route. None working refuses the round, exam or monthly
before anything starts, naming each credential and why (never a value). `exam-route` prints the choice.
Preflight calls are not booked (one Haiku call each, well under a cent).

Budget: WEEKLY_BUDGET_USD over a rolling 7 days of $HARNESS_LOOP_HOME/spend.jsonl, which only
this script writes (improver cost from claude's result line, exam cost from each run's
summary.json). A round that could take the week past the cap (improver + exam + side check budgets)
is refused before anything starts; so is a monthly or an exam. A run outside the loop (the weekly
gardener) is booked here too: `budget --est X --what S` asks first (exit 0 fits the cap, 1 refused with
the reason), and `book --kind K --usd X [--note S]` writes its cost as one row with round "gardener",
so spend.jsonl still has one writer. Both follow the model route: on the subscription, budget always
fits (no money moves) and prints the week's notional use, and book writes usd 0 with notional_usd. Kinds the loop books itself (improver, project-check, exam-*,
sidecheck-*) are refused: an exam-baseline row is the proof a main run is reusable. Run book when no
round is running: a round restores spend.jsonl from its snapshot and drops itself on any change.

leak: the same leak scan, free and on its own, over the lines added in <ref>...HEAD and a PR body
file; prints each hit, exits 1 on any, 3 when leak-patterns.txt is missing, 0 clean. Run it before a push.
--redact prints <file>:<line> (<rule>) per hit instead of the line, so a public CI log never repeats it.

port: opens one PR on agent-workflow from the base files an installed harness edited (`agent-cli harness
drift` base_edited, kept only where HARNESS_FILES.tsv says origin `base`: a merged base+addon settings.json carries
project hooks and is never ported; nor is a file whose install-time sha256 there is not the pinned commit's bytes,
e.g. a settings.json a .skipbase rewrote, named on a `port: skipped` line). Files whose bytes already match
origin/<base> (default main) are dropped; the rest go on branch harness-port/<8 hex of sha256 over the sorted paths>
off origin/<base>, copied when <base> still has the pinned HARNESS_VERSION commit's version, else merged three ways
with the pinned version as base (a conflict skips that file and the PR body lists it). The round's leak scan and load
check gate the push; an open PR for that branch is force-pushed and its title and body rewritten, never a second PR.
HARNESS_PORT_TODAY (YYYY-MM-DD) sets the body's date. --from may also be the project root holding .claude. Exit 0
PR opened or updated or nothing to port, 1 a gate refused (leak, load check, every file conflicting) or gh pr edit
failed, 2 usage or setup error (bad --from, no pinned commit, drift failed, no leak-patterns.txt), 3 no
HARNESS_FILES.tsv.

Leak scan: /Users/ always; tool names (EXAMPLE_LEAK) unless the line has "e.g."; plus private
names, one regex per line, from $HARNESS_LOOP_HOME/leak-patterns.txt, which must exist (an
empty file is a decision; a missing one is a mistake, so the round refuses); and any concrete
.claude/tries/<repo> path but this repo's. It reads the added lines, the commit message and
proposal.md as the PR body shows it (Evidence reduced to masked paths).
Project check: project_check.py, one Sonnet call over the same text, blocks a line that names or
points to one specific project (or carries a rule only one project needs) with none of those
words; it fails closed. It runs last, only when every other check passed; its cost is booked as
project-check (at its $1 cap when unknown).

Tries: the improver reads real /work try folders from every repo under ~/.claude/tries/ (owner
decision, 13 Oct 2026); a cited ~/.claude/tries/<repo>/<bead>/try-N reaches the PR only as tries/<k>/try-N
(the k-th cited try).

Env: HARNESS_LOOP_HOME (default ~/.harness-loop); LOOP_RUN_EXAM / LOOP_COMPARE replace the
run_exam.py / compare.py command; PROJECT_CHECK_CLAUDE replaces project_check.py's claude command
(tests use fakes); EXAM_PREFLIGHT_CLAUDE replaces the `claude` of the exam credential preflight.
"""
import argparse
import datetime
import functools
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

LOOP_DIR = Path(__file__).resolve().parent
AW_ROOT = LOOP_DIR.parent
sys.dont_write_bytecode = True  # hash_tree(LOOP_DIR) must not see an exam/__pycache__ this import left
sys.path.insert(0, str(LOOP_DIR / "exam"))
from trial_export import EXAM_ROUTES, OAUTH_TOKEN, json_events, model_route, rate_limit_reason, sandbox_env  # noqa: E402
import envfile  # noqa: E402
import install_harness  # noqa: E402
DEFAULT_HOME = Path.home() / ".harness-loop"
HOME = Path(os.environ.get("HARNESS_LOOP_HOME") or DEFAULT_HOME)
TRIES = Path.home() / ".claude/tries"  # every repo's /work tries: <repo>/<bead>/try-<n>
GH_REPO = "nguyenv119/agent-workflow"

WEEKLY_BUDGET_USD = 75.0  # fits one default round with a side check ($71)
IMPROVER_MAX_USD = 10.0
IMPROVER_TIMEOUT_S = 3600
PREFLIGHT_TIMEOUT_S = 120  # the exam credential preflight: one short Haiku call
# A preflight reply that means the subscription token itself is bad (so the next credential may be tried).
AUTH_FAIL = re.compile(r"\b401\b|invalid|not logged in|unauthori[sz]ed|expired|revoked|authenticat|/login", re.I)
PROJECT_CHECK_MAX_USD = 1.0  # project_check.py's --max-budget-usd; booked when a call's cost is unknown
DEFAULT_EXAM_BUDGET_USD = 30.0
FIRST_ESTIMATE_USD = 15.0  # exam/run_exam.py FIRST_ESTIMATE_USD (a test pins they match)
# Two side runs at FIRST_ESTIMATE_USD each. A default round with --sidecheck books improver 10 +
# project check 1 + exam 30 + side 30 = 71, which fits the weekly 75 only in a week with $4 or less
# already spent; otherwise check_budget refuses it before anything starts.
DEFAULT_SIDECHECK_BUDGET_USD = 30.0
SIDE_HOME = "loop-home"  # side runs live next to the private manifest: <manifest dir>/loop-home/runs

SCORES_OPEN = "<!-- harness-loop:scores v1 -->"
SCORES_CLOSE = "<!-- /harness-loop:scores -->"
STATE_FILES = ("history.jsonl", "queue.json", "spend.jsonl", "leak-patterns.txt")
ALLOWED_PATH = re.compile(r"^(\.claude/.+|AGENTS\.md)$")
EXAM_WORDS = re.compile(r"EXAM_MODE|exam mode", re.I)
EXAMPLE_LEAK = re.compile(r"Neon|NEON_|pnpm|turbo|vitest|VITEST|rtk |PGlite|Trigger\.dev|Vercel|race-|svc-", re.I)
TRY_PATH = re.compile(r"(?<![\w/.~-])((?:runs/|~/\.claude/tries/)[^\s`'\"<>()\[\],;]+)")
# A concrete .claude/tries/<repo> outside the masked Evidence list names a (private) repo; <placeholders> and this
# public repo's own folder pass.
TRIES_NAME = re.compile(r"\.claude/tries/(?:(?!<|agent-workflow/)[\w.-]|\S*\.\.)")  # no ../ detour either
# Host claude calls get the route's credential only: never an Anthropic API key or admin AWS key.
SCRUB_ENV = re.compile(r"^(ANTHROPIC_BASE_URL|AWS_ADMIN_.*|AWS_PROFILE"
                       r"|.*(TOKEN(?!S)|SECRET|KEY|PASSWORD|CREDENTIAL).*)$", re.I)
BEDROCK_ENV = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"}
# Bedrock's switch and model ids would send a subscription call to Bedrock, or name a model it lacks.
NOT_SUBSCRIPTION = re.compile(r"^(CLAUDE_CODE_USE_(BEDROCK|VERTEX|FOUNDRY)|ANTHROPIC_(MODEL|SMALL_FAST_MODEL"
                              r"|DEFAULT_\w+_MODEL))$")
RATE_LIMITED_EXIT = 4  # a usage limit paused the run: not a failure, run again once it resets
COULD_NOT_RUN_EXIT = 5  # the canary could not start or finish (not a fail): leave the PR open, retry later
CANARY_CONTEXT = "harness-canary"
SUBSCRIPTION_ONLY = False  # --subscription-only: the exam route is the plan token or nothing (main sets it)

# Gate guard (Long, 12 Oct): a change may add or sharpen checks, never remove or weaken one.
GATE_WORDS = re.compile(r"\b(tests?|testing|test-runner|test-first|gates?|reviews?|reviewers?|reviewing"
                        r"|acceptance|lint|linting|typecheck|verif\w*|coverage|quality|hooks?)\b", re.I)
WEAKEN = re.compile(r"\b(skip\w*|shorten\w*|sampl\w*|bypass\w*|omit\w*|disabl\w*|ignor\w*|optional\w*"
                    r"|loosen\w*|relax\w*|waive\w*)\b", re.I)
_WEAK = r"(?:skip|shorten|sampl|bypass|omit|disabl|ignor|loosen|relax|waive)\w*"
NEGATED_WEAKEN = re.compile(r"\b(?:never|not|no|cannot|don't|must not)\s+(?:\w+\s+){0,2}?" + _WEAK
                            + r"|\b" + _WEAK + r"(?:\s+\w+){0,3}?\s+(?:is|are)\s+(?:never|not)\s+"
                            r"(?:allowed|permitted|acceptable)\b", re.I)
_GATE_NOUN = r"(?:reviewers?|reviews?|gates?|tests?|test-runner|acceptance)"
# ponytail: phrase lists, not understanding; a reworded weakening can still slip past (the
# improver skill's hard rule 8 and human review of every PR are the backstop).
FEWER = re.compile(r"\b(?:only|just|a single)\s+(?:one\s+|a\s+)?(?:reviewers?|reviews?|gates?|tests?|acceptance)\b(?!-)"
                   r"|\binstead of (?:all |the )?(?:three|3|every)\b"
                   r"|(?<!not )(?<!never )(?<!n't )\b(?:push|merge|commit|open)\w*\s+before\s+(?:the\s+)?"
                   r"(?:\w+\s+)?" + _GATE_NOUN + r"\s+(?:finish|pass|complete|report|return)\w*", re.I)
NO_RUN = re.compile(r"\b(?:do not|don't|never|no need to|without|stop|avoid)\s+(?:run|running|execute|executing"
                    r"|spawn|spawning|write|writing|wait|waiting|call|calling)\b(?:\W+\w+){0,2}?\W+"
                    + GATE_WORDS.pattern, re.I)
PROTECTED = re.compile(r"^\.claude/(skills/(reviewer-[^/]+|test-runner|standards)/|hooks/)")
# What a /work run loads (files, or dirs ending in /): AGENTS.md (the SessionStart hook cats it), the
# settings and their hooks, the /work command and every agent and skill it reaches. A change elsewhere
# needs no exam, except a skill's or agent's frontmatter (name, description), which every session sees.
# test_loop derives this list.
WORK_LOADED = ("AGENTS.md", ".claude/settings.json", ".claude/hooks/", ".claude/commands/work.md",
               ".claude/agents/implementer.md", ".claude/agents/quick-reviewer.md",
               *(f".claude/skills/{s}/" for s in ("coordinator", "implementer", "reviewer-correctness",
                                                  "reviewer-tests", "reviewer-architecture", "test-runner",
                                                  "standards", "quick", "win-condition", "blast-radius",
                                                  "cli-for-agents")))
FRONTMATTER_FILE = re.compile(r"\.claude/(skills/[^/]+/SKILL|agents/[^/]+)\.md")


def die(msg, code=2):
    print(f"loop: {msg}", file=sys.stderr)
    sys.exit(code)


class RateLimited(Exception):
    """A paid claude call hit a usage limit; the round pauses (RATE_LIMITED_EXIT), never falls back."""


def route():
    """The model route (trial_export.model_route over os.environ); main() rejects a bad value first."""
    return model_route(os.environ)


def git(*args, cwd=AW_ROOT, check=True):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True)  # bytes: text=True turns \r into \n
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode("utf-8", "surrogateescape")


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def rel_home(path):
    """runs/... form of a path under $HARNESS_LOOP_HOME (never an absolute path in a PR)."""
    return Path(path).resolve().relative_to(HOME.resolve()).as_posix()


# ---- state: history, queue, spend ledger -------------------------------------------------

def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def append_jsonl(path, row):
    HOME.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def read_queue():
    f = HOME / "queue.json"
    return json.loads(f.read_text()) if f.is_file() else []


def write_queue(queue):
    write_atomic(HOME / "queue.json", json.dumps(queue, indent=2) + "\n")


def update_history(round_n, **fields):
    rows = read_jsonl(HOME / "history.jsonl")
    for r in rows:
        if r["round"] == round_n:
            r.update(fields)
    write_atomic(HOME / "history.jsonl", "".join(json.dumps(r) + "\n" for r in rows))


def week_spent(rows, at):
    """USD in ledger rows from the last 7 days before `at`."""
    week = datetime.timedelta(days=7)
    return sum(max(0.0, float(r["usd"])) for r in rows if at - datetime.datetime.fromisoformat(r["ts"]) < week)


def week_notional(rows, at):
    """Notional USD of subscription rows from the last 7 days before `at` (usage, not money)."""
    week = datetime.timedelta(days=7)
    return sum(float(r.get("notional_usd") or 0) for r in rows if at - datetime.datetime.fromisoformat(r["ts"]) < week)


def check_budget(est, what, code=2, paid=None):
    """Money spent this week; dies when `est` more would pass the cap. A subscription run moves no money:
    paid defaults to the model route's, and a caller whose exam runs on another route passes its own."""
    spent = week_spent(read_jsonl(HOME / "spend.jsonl"), now())
    if (route() != "subscription" if paid is None else paid) and spent + est > WEEKLY_BUDGET_USD + 1e-9:
        die(f"refusing {what}: ${spent:.2f} spent in the last 7 days + up to ${est:.2f} "
            f"> ${WEEKLY_BUDGET_USD:.2f} weekly cap", code=code)
    return spent


BOOKED = []  # every ledger row this process wrote; restore_state puts them back


def book(kind, usd, round_n, note="", via=None):
    """One ledger row on route `via` (default: the model route). On the subscription `usd` is claude's notional
    cost: booked as notional_usd, usd 0."""
    via = via or route()
    cost, paid = round(float(usd), 4), via != "subscription"
    row = {"ts": now().isoformat(), "kind": kind, "round": round_n, "usd": cost if paid else 0.0, "note": note,
           "route": via, **({} if paid else {"notional_usd": cost})}
    BOOKED.append(row)
    append_jsonl(HOME / "spend.jsonl", row)


# ---- tamper snapshot (in memory only) ----------------------------------------------------

def hash_tree(root):
    """{relpath: sha256} of every file under root, following symlinked dirs once."""
    root = Path(root)
    out, seen = {}, set()
    for dirpath, dirs, names in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        for n in names:
            p = Path(dirpath) / n
            rel = p.relative_to(root).as_posix()
            try:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as e:  # dangling link or unreadable: record that, so a change still shows
                out[rel] = f"unreadable: {e.__class__.__name__}"
            if p.is_symlink():
                out[rel] += " -> " + os.readlink(p)
    return out


def git_config_state():
    """Hashes of what can change how git diffs or pushes: the shared .git config/hooks/info, user config."""
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    out = {f".git/{k}": v for k, v in hash_tree(common).items() if k.split("/")[0] in ("config", "hooks", "info")}
    f = Path.home() / ".gitconfig"
    if f.is_file():
        out[str(f)] = hashlib.sha256(f.read_bytes()).hexdigest()
    xdg = {Path.home() / ".config/git", Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git"}
    for d in xdg:  # config, attributes, ignore: attributes can drop files from `git archive`
        out.update({f"{d}/{k}": v for k, v in hash_tree(d).items()})
    return out


def snapshot(own_branch, base_run):
    refs = [line for line in git("for-each-ref", "--format=%(refname) %(objectname)").splitlines()
            if not line.startswith(f"refs/heads/{own_branch} ")]
    return {
        "loop checkout: git status": git("status", "--porcelain", "--ignored=no"),
        "loop checkout: HEAD": git("rev-parse", "HEAD"),
        "loop checkout: refs": refs,
        "loop checkout: harness-loop files": hash_tree(LOOP_DIR),
        "git config, hooks or info": git_config_state(),
        "main screen run": hash_tree(base_run) if base_run else None,
        "state": {name: (HOME / name).read_bytes() if (HOME / name).is_file() else None for name in STATE_FILES},
    }


def tampered(before, after, ignore_pycache=False, skip_state=()):
    """Names of what changed between two snapshots."""
    out = []
    for key in before:
        a, b = before[key], after[key]
        if key == "state":
            out += [f"{name} changed" for name in a if a[name] != b[name] and name not in skip_state]
            continue
        if ignore_pycache and isinstance(a, dict):
            a, b = ({k: v for k, v in d.items() if "__pycache__/" not in k} for d in (a, b))
        if a != b:
            what = sorted(set(a) ^ set(b) | {k for k in a if k in b and a[k] != b[k]})[:3] \
                if isinstance(a, dict) else ""
            out.append(f"{key} changed {what}".rstrip())
    return out


def restore_state(saved):
    """Put the state files back as snapshotted, then re-append the ledger rows this process booked since."""
    for name, data in saved.items():
        f = HOME / name
        if data is None:
            f.unlink(missing_ok=True)
        elif not f.is_file() or f.read_bytes() != data:
            f.write_bytes(data)
    for row in BOOKED:
        append_jsonl(HOME / "spend.jsonl", row)


# ---- diff parsing and the gate guard -----------------------------------------------------

def parse_diff(text):
    """Files of a `git diff -M` unified diff: {old, new, status A|M|D|R, mode, lines: [(+|-, lineno, text)]}."""
    files, cur, in_hunk, o, n = [], None, False, 0, 0
    for line in text.split("\n"):  # not splitlines(): \x0c, \u2028 etc. would cut a line in two
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m[1], "new": m[2], "status": "M", "mode": False, "lines": []}
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif not in_hunk and line.startswith("new file mode"):
            cur["status"] = "A"
        elif not in_hunk and line.startswith("deleted file mode"):
            cur["status"] = "D"
        elif not in_hunk and line.startswith("old mode"):
            cur["mode"] = True
        elif not in_hunk and line.startswith("rename from "):
            cur["status"], cur["old"] = "R", line[len("rename from "):]
        elif not in_hunk and line.startswith("rename to "):
            cur["new"] = line[len("rename to "):]
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            o, n, in_hunk = int(m[1]), int(m[2]), True
        elif in_hunk and line.startswith("+"):
            cur["lines"].append(("+", n, line[1:]))
            n += 1
        elif in_hunk and line.startswith("-"):
            cur["lines"].append(("-", o, line[1:]))
            o += 1
        elif in_hunk and line.startswith(" "):
            o, n = o + 1, n + 1
    return files


def headings_at(text, lineno):
    """The markdown heading chain above line `lineno` (1-based), ignoring code fences."""
    stack, fence = [], False
    for line in text.splitlines()[:lineno]:
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#+)\s+(.*)", line)
        if m:
            stack = [h for h in stack if h[0] < len(m[1])] + [(len(m[1]), m[2])]
    return " / ".join(h for _, h in stack)


def hook_commands(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return {(event, e.get("matcher", ""), h.get("command"))
            for event, entries in (data.get("hooks") or {}).items() for e in entries for h in e.get("hooks", [])}


def weakening(line):
    """True if an added line tells an agent to skip, shorten, sample or bypass a gate."""
    rest = NEGATED_WEAKEN.sub(" ", line)
    return bool("disableallhooks" in line.lower() or (WEAKEN.search(rest) and GATE_WORDS.search(rest)) or NO_RUN.search(line) or FEWER.search(line))


def gate_guard(diff_text, old_text, new_text):
    """Reasons the diff removes or weakens a quality gate, reviewer or test ([] = passes).

    (a) deletes, renames or changes the mode of a reviewer/test-runner/standards skill file or a
        hook, or drops a hook's settings.json entry;
    (b) removes or rewrites a line in those files, or in any markdown file a line that mentions a
        gate/review/test word or sits under a heading that does (e.g. the coordinator's reviewer,
        gate and real-acceptance steps, the implementer's test phases, a Quality Gates table);
    (c) adds a line telling an agent to skip, shorten, sample, bypass or not run one.
    old_text(path) / new_text(path) give the file at base / candidate, or None.
    """
    reasons = []
    for f in parse_diff(diff_text):
        old = f["old"]
        if PROTECTED.match(old) and (f["status"] in "DR" or f["mode"]):
            reasons.append(f"(a) {'deletes' if f['status'] == 'D' else 'renames' if f['status'] == 'R' else 'changes the mode of'} {old}")
        removed = [(ln, t) for kind, ln, t in f["lines"] if kind == "-"]
        if PROTECTED.match(old) and removed:
            reasons.append(f"(b) removes or rewrites {len(removed)} line(s) of {old}")
        elif old.endswith(".md") and removed:
            base = old_text(old) or ""
            for ln, t in removed:
                head = headings_at(base, ln)
                if GATE_WORDS.search(t) or GATE_WORDS.search(head):
                    reasons.append(f"(b) removes or rewrites {old}:{ln} ({head or 'top'}): {t.strip()[:80]}")
        if old == ".claude/settings.json":
            before = hook_commands(old_text(old))
            after = set() if f["status"] == "D" else hook_commands(new_text(f["new"]))
            if before and after is not None:
                reasons += [f"(a) drops hook entry {ev} {cmd}" for ev, _, cmd in sorted(before - after)]
        reasons += [f"(c) {f['new']}:{ln}: {t.strip()[:100]}" for kind, ln, t in f["lines"]
                    if kind == "+" and weakening(t)]
    return reasons


def show(ref, path, repo=AW_ROOT):
    """The file at ref, or None."""
    p = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def guard_commits(base, cand, repo=AW_ROOT):
    diff = git("diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--text", base, cand, cwd=repo)
    return gate_guard(diff, lambda p: show(base, p, repo), lambda p: show(cand, p, repo)), diff


def work_loaded(path):
    return any(path == w or (w.endswith("/") and path.startswith(w)) for w in WORK_LOADED)


def frontmatter(text):
    m = re.match(r"---\n.*?\n---\n", text or "", re.S)
    return m[0] if m else None


def path_gate(base, head, repo=AW_ROOT):
    """(needs_exam, reason) for the merge-base diff base...head: True when it touches a path /work loads
    (WORK_LOADED) or the frontmatter of any skill's SKILL.md or .claude/agents/*.md (adding or deleting one
    changes it too)."""
    diff = git("diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", cwd=repo)
    paths = [p for p in diff.split("\0") if p]
    mb = git("merge-base", base, head, cwd=repo).strip()
    hits, rest = [], []
    for p in paths:
        if work_loaded(p):
            hits.append(p)
        elif FRONTMATTER_FILE.fullmatch(p):
            front = frontmatter(show(mb, p, repo)) != frontmatter(show(head, p, repo))
            (hits if front else rest).append(f"{p} ({'frontmatter' if front else 'body'})")
        else:
            rest.append(p)
    if hits:
        return True, f"/work loads {', '.join(hits)}"
    return False, f"touches only {', '.join(rest) or 'nothing'}; /work loads none of them"


def restack(cand, onto, repo=AW_ROOT):
    """cand's change since its merge base with onto, as one commit on onto (the same as a cherry-pick for a
    one-commit candidate, without a worktree); cand itself if it already sits on onto; None on a conflict.
    A ref under refs/harness-loop/restack/ keeps the commit from gc until run_exam has read it."""
    if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", onto, cand]).returncode == 0:
        return cand
    p = subprocess.run(["git", "-C", str(repo), "merge-tree", "--write-tree", onto, cand],
                       capture_output=True, text=True)
    if p.returncode == 1:
        return None
    if p.returncode:
        raise RuntimeError(f"git merge-tree: {p.stderr.strip()[:300]}")
    sha = git("-c", "user.name=harness-loop", "-c", "user.email=harness-loop@example.invalid", "commit-tree",
              p.stdout.split()[0], "-p", onto, "-m", f"harness-loop: {cand[:7]} on {onto[:7]}", cwd=repo).strip()
    git("update-ref", f"refs/harness-loop/restack/{cand}", sha, cwd=repo)
    return sha


def unstack(cand, repo=AW_ROOT):
    """Delete restack()'s ref for cand once run_exam has read the commit (a no-op if it made none)."""
    git("update-ref", "-d", f"refs/harness-loop/restack/{cand}", cwd=repo, check=False)


# ---- checks ------------------------------------------------------------------------------

def private_patterns():
    f = HOME / "leak-patterns.txt"
    if not f.is_file():
        die(f"{f} is missing: put private names there, one regex per line (an empty file means none)")
    pats = [line.strip() for line in f.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None


def leak_rule(line, private):
    """The first leak rule the line breaks (users-path, private, tries, example-word), or None."""
    if "/" "Users/" in line:  # split so this line does not trip the scan it implements
        return "users-path"
    if private and private.search(line):
        return "private"
    if TRIES_NAME.search(line):
        return "tries"
    if EXAMPLE_LEAK.search(line) and "e.g." not in line:
        return "example-word"
    return None


def leak_hits(lines, private):
    return [line for line in lines if leak_rule(line, private)]


def change_leaks(base, cand, body, private):
    """Leak hits in the lines base..cand adds, cand's commit message and the PR body (the round's and port's scan)."""
    files = parse_diff(git("diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--text", base, cand))
    added = [t for f in files for kind, _, t in f["lines"] if kind == "+"]
    return leak_hits(added + git("log", "-1", "--format=%B", cand).splitlines() + body.splitlines(), private)


def section(text, title):
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if line.startswith("## "):
            inside = line.strip() == title
        if inside:
            out.append(line)
    return "\n".join(out)


def existing_dirs():
    """Every dir under the two try roots, taken before the improver runs: a folder it creates is no evidence."""
    return {Path(d).resolve() for root in (HOME / "runs", TRIES) for d, _, _ in os.walk(root)}


def try_folders(text, existing=None):
    """(valid, invalid) try-folder citations: runs/... under $HARNESS_LOOP_HOME/runs or
    ~/.claude/tries/<repo>/..., each an existing dir inside its root (and in `existing`, if given)."""
    valid, invalid, seen = [], [], set()
    for p in dict.fromkeys(m.rstrip(".:") for m in TRY_PATH.findall(text)):
        root, rest = (HOME / "runs", p[len("runs/"):]) if p.startswith("runs/") else (TRIES, p[len("~/.claude/tries/"):])
        target = (root / rest).resolve()
        # a tries citation names at least <repo>/<bead>: a whole repo's folder is not one try
        deep = root is not TRIES or target.parent != TRIES.resolve()
        # one try counts once: not try-1 and try-1/, nor a bead folder and its own try
        nested = any(target.is_relative_to(s) or s.is_relative_to(target) for s in seen)
        ok = (target.is_dir() and target.is_relative_to(root.resolve()) and target != root.resolve() and deep
              and (existing is None or target in existing) and not nested)
        if ok:
            seen.add(target)
        (valid if ok else invalid).append(p)
    return valid, invalid


def frontmatter_ok(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    keys = {line.split(":", 1)[0].strip() for line in m[1].splitlines() if ":" in line} if m else set()
    return {"name", "description"} - keys


def load_check(cand):
    """[] or the reasons the harness at cand does not install and load."""
    with tempfile.TemporaryDirectory(prefix="loop-load-") as tmp:
        target = Path(tmp) / "x"
        p = subprocess.run([sys.executable, str(LOOP_DIR / "install_harness.py"), "--repo", str(AW_ROOT),
                            "--commit", cand, "--target", str(target)], capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if p.returncode != 0:
            return [f"install_harness failed: {p.stderr.strip()[:200]}"]
        return tree_problems(target)


def tree_problems(target):
    """[] or why the installed .claude + AGENTS.md tree at target would not load: a hook that is not valid
    bash, a settings.json that does not parse, a SKILL.md frontmatter without name or description."""
    target = Path(target)
    bad = [f"bash -n {h.name}" for h in sorted((target / ".claude/hooks").glob("*.sh"))
           if subprocess.run(["bash", "-n", str(h)], capture_output=True).returncode != 0]
    settings = target / ".claude/settings.json"
    if settings.is_file():
        try:
            json.loads(settings.read_text())
        except ValueError as e:
            bad.append(f"settings.json does not parse: {e}")
    for skill in sorted((target / ".claude/skills").glob("*/SKILL.md")):
        missing = frontmatter_ok(skill.read_text())
        if missing:
            bad.append(f"{skill.parent.name}/SKILL.md frontmatter lacks {', '.join(sorted(missing))}")
    return bad


def project_check(base, cand, body, round_n):
    """(ok, detail) from project_check.py over base..cand and the PR body; books its cost. Fails closed."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="loop-body-") as fh:
        fh.write(body)
        fh.flush()
        p = subprocess.run([sys.executable, str(LOOP_DIR / "project_check.py"), base, cand, fh.name], cwd=AW_ROOT,
                           capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        v = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"no verdict (exit {p.returncode}): {p.stderr.strip()[:200]}"
    if v.get("cost_usd") is not None:
        book("project-check", v["cost_usd"], round_n)
    elif v.get("called"):
        book("project-check", PROJECT_CHECK_MAX_USD, round_n, "cost unknown: booked at cap")
    if v.get("rate_limited"):
        raise RateLimited(f"project check: {v['rate_limited']}")
    if p.returncode == 0 and v.get("clean") is True:
        return True, f"${float(v.get('cost_usd') or 0):.4f}"
    if v.get("error"):
        return False, "error: " + str(v["error"])[:200]
    return False, "; ".join(f"{h.get('line')}: {h.get('why')}"[:160] for h in v.get("hits") or []) or "blocked"


def run_checks(base, cand, proposal, private, existing=None, round_n=0):
    """[(name, ok, detail)] for every check. The gate guard's rows are named 'gate guard'."""
    out = []
    count = int(git("rev-list", "--count", f"{base}..{cand}"))
    out.append(("one commit", count == 1, f"{count} commits"))
    files = parse_diff(git("diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--text", base, cand))
    paths = sorted({p for f in files for p in (f["old"], f["new"])})
    bad = [p for p in paths if not ALLOWED_PATH.match(p) or "EXAM_MODE" in p or p.endswith(".gitattributes")]
    out.append(("allowed paths", not bad, ", ".join(bad) or f"{len(paths)} path(s)"))
    changed = [t for f in files for _, _, t in f["lines"]]
    exam = [t.strip()[:80] for t in changed if EXAM_WORDS.search(t)]
    out.append(("no EXAM_MODE", not exam, "; ".join(exam)))
    coord = ".claude/skills/coordinator/SKILL.md"
    same = section(show(base, coord) or "", "## Exam mode") == section(show(cand, coord) or "", "## Exam mode")
    out.append(("Exam mode section untouched", same, "" if same else "Exam mode section changed"))
    reasons, _ = guard_commits(base, cand)
    out.append(("gate guard", not reasons, "; ".join(reasons)))
    body = proposal_for_body(proposal, existing)  # what the PR shows: Evidence as masked paths, notes stay local
    hits = change_leaks(base, cand, body, private)
    out.append(("leak scan", not hits, "leak: " + "; ".join(h.strip()[:80] for h in hits) if hits else ""))
    idea = re.search(r"^Idea:\s*\S", proposal, re.M)
    valid, invalid = try_folders(proposal, existing)
    ok = bool(proposal) and bool(idea) and len(valid) >= 2
    detail = ("no proposal.md" if not proposal else "no Idea: line" if not idea else
              f"{len(valid)} valid try folders (need 2){'; not found: ' + ', '.join(invalid) if invalid else ''}")
    out.append(("proposal", ok, detail))
    load = load_check(cand)
    out.append(("load check", not load, "; ".join(load)))
    if all(ok for _, ok, _ in out):  # the one paid check runs last, and only on a round that can still pass
        out.append(("project check", *project_check(base, cand, body, round_n)))
    return out, files


# ---- exam --------------------------------------------------------------------------------

def tool(env_var, script):
    cmd = os.environ.get(env_var)
    return shlex.split(cmd) if cmd else [sys.executable, str(LOOP_DIR / "exam" / script)]


def exam_env():
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HARNESS_LOOP_HOME": str(HOME)}


def run_exam(sha, manifest, kind, budget, round_n, screen=True, tries=1, extra=(), side_home=None):
    """Run one exam, book its cost, return the run dir. sha: a commit, or a Path of an installed tree (--installed).

    side_home: a private side check. Its run goes under side_home/runs, never HOME/runs (which the
    improver reads), and its ledger row names neither the run nor its set."""
    harness = ["--installed", str(sha)] if isinstance(sha, Path) else ["--harness", sha]
    cmd = tool("LOOP_RUN_EXAM", "run_exam.py") + [*harness, "--tasks", str(manifest),
                                                   "--budget-usd", f"{budget:.2f}", "--kind", kind,
                                                   "--tries", str(tries)] + (["--screen"] if screen else []) + list(extra)
    label = f"sidecheck-{kind}" if side_home else f"exam-{kind}"
    via = exam_route()[0]
    env = envfile.with_token({**exam_env(), "HARNESS_MODEL_ACCESS": via,
                              **({"HARNESS_LOOP_HOME": str(side_home)} if side_home else {})})
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    except BaseException:
        book(label, budget, round_n, "interrupted: booked at budget", via)
        raise
    m = re.search(r"^run_exam: (\S+) (complete|incomplete)", p.stdout, re.M)
    if not m:
        raise RuntimeError(f"run_exam left no run (exit {p.returncode}): {p.stderr.strip()[-300:]}")
    run = Path(m[1])
    cost = json.loads((run / "summary.json").read_text()).get("total_cost_usd") or 0
    book(label, cost, round_n, "side check" if side_home else run.name, via)
    limit = json.loads((run / "run.json").read_text()).get("rate_limited")
    if limit:  # no name: a side run's carries the private set
        raise RateLimited(f"{label} run: {limit}")
    return run


def compare(a, b, *extra):
    p = subprocess.run(tool("LOOP_COMPARE", "compare.py") + [str(a), str(b), *extra],
                       capture_output=True, text=True, env=exam_env())
    if p.returncode != 0:
        raise RuntimeError(f"compare.py exit {p.returncode}: {p.stderr.strip()[-300:]}")
    return dict(tok.split("=", 1) for tok in p.stdout.split() if "=" in tok)


def harness_tree(sha):
    """What the exam installs at sha: the .claude tree and AGENTS.md blob ids ('' if sha is unknown)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return ""
    return git("ls-tree", sha, "--", ".claude", "AGENTS.md", check=False)


def find_base_run(base_sha, manifest):
    """Newest complete, sealed main screen run of this manifest's screen tasks that this script booked,
    made at any commit whose installed harness (.claude/ + AGENTS.md) matches base_sha's, or None.

    Booked = named in an exam-baseline row of spend.jsonl, which the improver cannot write (it is
    hashed and restored every round), so a run dir the improver forged is never reused. Matching on
    the installed tree, not the commit, lets a run survive main commits that only touch harness-loop/.
    The exam's own settings (model, per-try cap) are checked against the candidate run once it
    exists (screen_against_main); the sandbox (docker or daytona) is deliberately not part of the key.
    base_sha a Path (an installed tree): the key is its installed_sha256 instead."""
    rows = read_jsonl(HOME / "spend.jsonl")
    booked = ({r.get("note") for r in rows if r.get("kind") == "exam-baseline"}
              - {r.get("note") for r in rows if r.get("kind") == "exam-baseline-stale"})
    data = json.loads(Path(manifest).read_text())
    want = sorted(t["id"] for t in data.get("tasks", []) if t.get("screen"))
    installed = install_harness.installed_sha256(base_sha) if isinstance(base_sha, Path) else None
    tree = None if installed else harness_tree(base_sha)
    found = []
    for meta_f in (HOME / "runs").glob("*/run.json"):
        try:
            meta = json.loads(meta_f.read_text())
        except (ValueError, OSError):
            continue
        if not isinstance(meta, dict):
            continue
        if (meta.get("set") == data.get("set") and meta.get("tasks") == want and meta.get("screen")
                and meta.get("agent") == "workflow" and meta.get("kind") == "baseline"
                and meta.get("status") == "complete" and meta.get("model_access", "bedrock") == exam_route()[0] and (meta_f.parent / "summary.sha256").is_file()
                and meta_f.parent.name in booked
                and (meta.get("installed_sha256") == installed if installed
                     else tree and not meta.get("installed_sha256")
                     and harness_tree(str(meta.get("harness_commit"))) == tree)):
            found.append((meta_f.stat().st_mtime, meta_f.parent))
    return max(found)[1] if found else None


def tries_of(run):
    return json.loads((Path(run) / "run.json").read_text()).get("tries", 1)


def score_line(kind, stats, base_sha, cand_sha, tries, tail):
    n = int(stats["n"])
    return (f"{kind} ({n} tasks x {tries}): baseline main {base_sha[:7]} solved {stats['solvedA']}/{n} ${float(stats['costA']):.2f}"
            f" | candidate {cand_sha[:7]} solved {stats['solvedB']}/{n} ${float(stats['costB']):.2f} | {tail}")


def scores_block(*lines):
    return "\n".join([SCORES_OPEN, *lines, SCORES_CLOSE])


def set_scores_block(body, lines):
    """body with its score block replaced by lines (appended if it has none)."""
    new, n = re.subn(re.escape(SCORES_OPEN) + r".*?" + re.escape(SCORES_CLOSE), lambda _: scores_block(*lines),
                     body, count=1, flags=re.S)
    return new if n else body.rstrip() + "\n\n" + scores_block(*lines)


def edit_pr_body(pr, lines, body_f):
    """Write lines into PR #pr's score block; the body is kept at body_f to re-apply by hand. True if gh took it."""
    body_f.parent.mkdir(parents=True, exist_ok=True)
    body_f.write_text(set_scores_block(gh_json("pr", "view", str(pr), "--json", "body")["body"], lines))
    return subprocess.run(["gh", "pr", "edit", str(pr), "--repo", GH_REPO, "--body-file", str(body_f)],
                          capture_output=True, text=True).returncode == 0


def settings_differ(base_run, cand_run, round_n):
    """Exam settings a main run differs from the candidate's in; books such a main run stale, so
    find_base_run skips it from now on (its verdict would be wrong)."""
    b, c = (json.loads((Path(r) / "run.json").read_text()) for r in (base_run, cand_run))
    differ = [k for k in ("model", "max_budget_usd", "tries") if b.get(k) != c.get(k)]
    if differ:
        book("exam-baseline-stale", 0, round_n, Path(base_run).name)
    return differ


def early_stop_line(run):
    why = json.loads((Path(run) / "run.json").read_text()).get("early_stop")
    return [f"early stop: {why}"] if why else []


def check_screen_budget(budget, base_run, flag="--budget-usd"):
    """Die unless `budget`, split over main's run (skipped when base_run is reused) and the candidate's, gives
    each run run_exam's first-try estimate (else nothing starts and compare.py crashes on the empty run)."""
    need = FIRST_ESTIMATE_USD * (1 if base_run else 2)
    if budget < need:
        die(f"{flag} {budget:g} is too small: pass at least {flag} {need:g} (each run, "
            f"{'the candidate' if base_run else 'main and the candidate'}, needs ${FIRST_ESTIMATE_USD:g} to start)")


def screen_against_main(base_sha, cand, manifest, budget, round_n, base_run=None, after_baseline=None,
                        after_candidate=None):
    """Screen cand against main's screen run: base_run (find_base_run's) is reused, or main is run first
    (then after_baseline(run)); cand runs with early stop against it. after_candidate(base_run) may return a
    reason to stop before scoring (cmd_round's tamper check). Returns (base_run, cand_run, stop, stats):
    stop is that reason, or the exam settings main's run differs from cand's in, with stats None."""
    per_run = budget if base_run else budget / 2
    if base_run is None:
        base_run = run_exam(base_sha, manifest, "baseline", per_run, round_n)
        if after_baseline:
            after_baseline(base_run)
    cand_run = run_exam(cand, manifest, "candidate", per_run, round_n, extra=("--early-stop-vs", str(base_run)))
    stop = after_candidate(base_run) if after_candidate else ""
    if stop:
        return base_run, cand_run, stop, None
    differ = settings_differ(base_run, cand_run, round_n)
    if differ:
        return base_run, cand_run, (f"main run {rel_home(base_run)} differs from the candidate in "
                                    f"{', '.join(differ)}; the next screen re-runs main"), None
    return base_run, cand_run, "", compare(base_run, cand_run, "--screen-rule")


def mask_task(path, k=1):
    """runs/<run>/<task>/try-N -> runs/<run>/task-<sha8>/try-N, and the k-th ~/.claude/tries/<repo>/<bead>/try-N ->
    tries/<k>/try-N: the PR names no exam task, repo or bead (proposal.md keeps them). A plain count, not a
    hash: a hash of a guessable repo and bead name could be confirmed offline."""
    parts = path.split("/")
    if parts[0] == "runs" and len(parts) > 3:
        parts[2] = "task-" + hashlib.sha256(parts[2].encode()).hexdigest()[:8]
    elif parts[:3] == ["~", ".claude", "tries"]:  # from the resolved path: x/../<repo> must not shift the cut
        rel = (TRIES / "/".join(parts[3:])).resolve().relative_to(TRIES.resolve()).parts
        parts = ["tries", str(k), *rel[2:]]
    return "/".join(parts)


def proposal_for_body(proposal, existing=None):
    """proposal.md with its Evidence list reduced to the cited paths (exam task, repo and bead dirs masked) and no
    marker lines."""
    out, skip = [], False
    proposal = re.sub(re.escape(SCORES_OPEN) + r".*?" + re.escape(SCORES_CLOSE), "", proposal, flags=re.S)
    for line in proposal.splitlines():
        if re.match(r"^[A-Z][\w ]*:", line):
            skip = line.startswith("Evidence:")
            if skip:
                out.append("Evidence (task, repo and bead dirs masked; notes in the round's proposal.md):")
                out += [f"  {mask_task(t, k)}" for k, t in enumerate(try_folders(proposal, existing)[0], 1)]
                continue
        if not skip and "harness-loop:scores" not in line:
            out.append(line)
    return "\n".join(out).strip()


# ---- the improver ------------------------------------------------------------------------

def improver_settings(round_dir, proposals=None):
    deny = []
    for p in (f"{AW_ROOT}/**", f"{HOME}/runs/**", f"{TRIES}/**", *(f"{HOME}/{n}" for n in STATE_FILES),
              *([str(proposals)] if proposals else [])):
        deny += [f"{tool_name}(/{p})" for tool_name in ("Edit", "Write", "NotebookEdit")]
    deny += ["Bash(git push:*)", "Bash(gh:*)"]
    f = round_dir / "improver-settings.json"
    f.write_text(json.dumps({"permissions": {"deny": deny}, "autoMemoryEnabled": False}, indent=2) + "\n")
    return f


def model_env():
    """os.environ minus every credential but the route's (the improver's and the project check's env):
    the Bedrock keys on bedrock; on the subscription CLAUDE_CODE_OAUTH_TOKEN (when set) and no Bedrock
    switch or model ids."""
    if route() == "bedrock":
        return {k: v for k, v in os.environ.items() if k in BEDROCK_ENV or not SCRUB_ENV.match(k)}
    return {k: v for k, v in os.environ.items()
            if (k == OAUTH_TOKEN or not SCRUB_ENV.match(k)) and not NOT_SUBSCRIPTION.match(k)}


def on_bedrock(env):
    return env.get("CLAUDE_CODE_USE_BEDROCK") == "1" and bool(env.get("AWS_ACCESS_KEY_ID"))


def bedrock_problem(env):
    """Why a call on the bedrock route cannot run, or '' (always '' on the subscription)."""
    if route() == "bedrock" and not on_bedrock(env):
        return "Bedrock (HARNESS_MODEL_ACCESS=bedrock) needs CLAUDE_CODE_USE_BEDROCK=1 and AWS_ACCESS_KEY_ID"
    return ""


def preflight(via):
    """'' when one cheap claude call answers on the credential the exam's sandboxes would get on route `via`,
    else why not (never a credential value). On the subscription only a missing token or an auth failure
    (AUTH_FAIL) falls through to the next, paid, credential; a usage limit, a timeout or any other failure
    raises RateLimited, so a busy subscription pauses and never moves the exam to a paid route."""
    try:
        cred = sandbox_env(envfile.with_token(os.environ), via, tries_dir="/")
    except RuntimeError as e:  # "<VAR> is not set on the host; ..." or an unreadable HARNESS_EXAM_ENV_FILE
        return str(e).split(";")[0]
    secrets = [v for k, v in cred.items() if SCRUB_ENV.match(k) and v]
    cmd = shlex.split(os.environ.get("EXAM_PREFLIGHT_CLAUDE") or "claude") + [
        "-p", "reply ok", "--model", "haiku", "--output-format", "json", "--no-session-persistence"]
    with tempfile.TemporaryDirectory(prefix="exam-preflight-") as tmp:  # no user settings, hooks or login
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "CLAUDE_CONFIG_DIR": tmp, **cred}
        try:
            p = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=PREFLIGHT_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            if via == "subscription":
                raise RateLimited(f"exam preflight on the subscription did not answer ({e.__class__.__name__})") from None
            return f"preflight call failed ({e.__class__.__name__})"
    events = json_events(p.stdout)
    if p.returncode == 0 and any(isinstance(e, dict) and e.get("type") == "result" and e.get("is_error") is False
                                 for e in events):
        return ""
    said = next((e["result"] for e in events if isinstance(e, dict) and isinstance(e.get("result"), str)), "")
    said = said or (p.stderr.strip().splitlines() or [""])[-1]
    for v in secrets:
        said = said.replace(v, "***")
    if via == "subscription" and (rate_limit_reason(events) or not AUTH_FAIL.search(said)):
        raise RateLimited(f"exam preflight on the subscription: {said[:200]}")
    return f"preflight call failed (exit {p.returncode}: {said[:200]})"


@functools.cache
def exam_route():
    """(route, reasons) for the exam's sandboxed /work runs, decided once per process. On HARNESS_MODEL_ACCESS=bedrock:
    bedrock (run_exam.py checks the keys). On the subscription (owner decision, 16 Oct 2026) the first of
    EXAM_ROUTES whose one-call preflight answers: the CLAUDE_CODE_OAUTH_TOKEN, then Bedrock (paid), then
    ANTHROPIC_API_KEY (paid); route None when none does. reasons: why each earlier one was passed over.
    SUBSCRIPTION_ONLY: the subscription's preflight alone, whatever the model route."""
    if SUBSCRIPTION_ONLY:
        why = preflight("subscription")
        return ("subscription", ()) if not why else (None, (f"subscription: {why}",))
    if route() == "bedrock":
        return "bedrock", ()
    reasons = []
    for via in EXAM_ROUTES:
        why = preflight(via)
        if not why:
            return via, tuple(reasons)
        reasons.append(f"{via}: {why}")
    return None, tuple(reasons)


def exam_problem():
    """Why no exam can run, or ''. Prints the route the exam takes and what was passed over."""
    via, reasons = exam_route()
    if via is None:
        return ("no exam credential works (" + "; ".join(reasons) + "); for the subscription run "
                f"`claude setup-token` once and export {OAUTH_TOKEN} (or name a file holding it in {envfile.ENV_FILE})")
    print(f"exam route: {via}" + (f" (passed over: {'; '.join(reasons)})" if reasons else ""))
    return ""


def keychain_login(env):
    """True when claude must use this machine's stored login: the subscription with no CLAUDE_CODE_OAUTH_TOKEN.
    That login lives under the default config dir, so such a call cannot get a fresh CLAUDE_CONFIG_DIR."""
    return route() == "subscription" and not env.get(OAUTH_TOKEN)


# What fences a keychain-login call from the user's settings, CLAUDE.md, hooks, plugins and MCP servers
# (with auto-memory off in its --settings) where a fresh CLAUDE_CONFIG_DIR cannot be used.
KEYCHAIN_FENCE = ["--setting-sources", "project,local", "--strict-mcp-config"]


def improver_env(round_dir, default):
    env = model_env()
    env.update(HARNESS_LOOP_ROUND_DIR=str(round_dir), HARNESS_LOOP_HOME=str(HOME))
    if default and not keychain_login(env):
        env["CLAUDE_CONFIG_DIR"] = str(round_dir / "claude-config")  # no user settings, memory or plugins
    return env


def improver_argv(args, msg, round_dir, proposals=None):
    if args.improver_cmd:
        argv = shlex.split(args.improver_cmd)
        if not Path(argv[0]).is_absolute() and Path(argv[0]).exists():
            argv[0] = str(Path(argv[0]).resolve())
        return argv + [msg]
    return ["claude", "-p", "--model", "opus", "--permission-mode", "bypassPermissions",
            "--settings", str(improver_settings(round_dir, proposals)), "--max-budget-usd", f"{args.improver_budget_usd:.2f}",
            *(KEYCHAIN_FENCE if keychain_login(model_env()) else []),
            "--output-format", "stream-json", "--verbose", msg]


def run_improver(argv, wt, round_dir, env):
    """Run the improver in its own process group; kill whatever it leaves behind.
    (exit, cost|None, usage-limit message|None)."""
    with (round_dir / "improver-transcript.jsonl").open("w") as out, \
            (round_dir / "improver-stderr.txt").open("w") as err:
        proc = subprocess.Popen(argv, cwd=wt, env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            code = proc.wait(timeout=IMPROVER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            code = "timeout"
        finally:  # also on Ctrl-C: never leave a paid improver running
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
    cost = None
    events = json_events((round_dir / "improver-transcript.jsonl").read_text(errors="replace"))
    for rec in events:
        if isinstance(rec, dict) and rec.get("type") == "result" and rec.get("total_cost_usd") is not None:
            cost = float(rec["total_cost_usd"])
    return code, cost, rate_limit_reason(events)


# ---- commands ----------------------------------------------------------------------------

def next_round():
    nums = [int(p.name) for p in (HOME / "rounds").glob("*") if p.name.isdigit()]
    return max(nums, default=0) + 1


def cleanup(wt, branch, keep_branch):
    git("worktree", "remove", "--force", str(wt), check=False)
    if not keep_branch:
        git("branch", "-D", branch, check=False)


def side_home_ok(side_home, addon):
    """Side runs inside the add-on would be installed into the next sandbox (and change its hash
    mid-run) unless the add-on's .installignore covers them; refuse that layout."""
    if side_home.resolve() == HOME.resolve() or HOME.resolve() in side_home.resolve().parents:
        die(f"side runs ({side_home}) must not live under {HOME}: the improver reads it")
    if addon not in side_home.parents:
        return
    # install_harness.addon_files' rule (prefix lines in .installignore), copied: importing it writes
    # harness-loop/__pycache__, which hash_tree(LOOP_DIR) hashes and which the tests' throwaway loop
    # checkouts (no .gitignore) report as untracked. ponytail: re-sync if the installer's rule changes.
    ignore = addon / ".installignore"
    lines = [s.strip().rstrip("/") for s in (ignore.read_text().splitlines() if ignore.is_file() else [])]
    rel = side_home.relative_to(addon)
    if not any(ln and not ln.startswith("#") and (rel == Path(ln) or Path(ln) in rel.parents) for ln in lines):
        die(f"side runs would land in the add-on at {rel}; list the side manifest's folder in {ignore}")


def push_branch(branch, force=False):
    git("-c", "core.hooksPath=/dev/null", "push", "-q", "--no-verify", *(["--force"] if force else []), "origin",
        f"refs/heads/{branch}:refs/heads/{branch}")


def open_pr(branch, base, title, body, label=None, draft=False, force=False):
    """Push branch and open its PR against base (the round's and port's); returns (url, PR number)."""
    push_branch(branch, force)
    gh = ["gh", "pr", "create", "--repo", GH_REPO, "--head", branch, "--base", base, "--title", title, "--body", body,
          *(["--label", label] if label else []), *(["--draft"] if draft else [])]
    url = subprocess.run(gh, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
    return url, int(url.rstrip("/").rsplit("/", 1)[1])


def cmd_round(args):
    default = not args.improver_cmd
    if default:
        if bedrock_problem(os.environ):
            die(f"the default improver: {bedrock_problem(os.environ)}")
        if args.improver_budget_usd > IMPROVER_MAX_USD:
            die(f"--improver-budget-usd is capped at ${IMPROVER_MAX_USD:.2f}")
    if bool(args.sidecheck) != bool(args.sidecheck_addon):
        die("--sidecheck and --sidecheck-addon go together")
    if not args.no_screen and (why := exam_problem()):
        die(f"refusing this round: {why}")
    if args.sidecheck:
        side_home_ok(Path(args.sidecheck).resolve().parent / SIDE_HOME, Path(args.sidecheck_addon).resolve())
    private = private_patterns()
    if git("status", "--porcelain"):
        die(f"loop checkout {AW_ROOT} is not clean; run from a clean, dedicated clone")
    git("fetch", "-q", "origin", "main")
    base_sha = git("rev-parse", "origin/main").strip()
    manifest = Path(args.tasks).resolve()
    base_run = None if args.no_screen else find_base_run(base_sha, manifest)
    if not args.no_screen:
        check_screen_budget(args.budget_usd, base_run)
        if args.sidecheck:  # the side check always runs main and the candidate
            check_screen_budget(args.sidecheck_budget_usd, None, "--sidecheck-budget-usd")
    exam_budget = 0.0 if args.no_screen else args.budget_usd
    if args.sidecheck and not args.no_screen:
        exam_budget += args.sidecheck_budget_usd
    host = args.improver_budget_usd + PROJECT_CHECK_MAX_USD if default and route() != "subscription" else 0.0
    exam_paid = bool(exam_budget) and exam_route()[0] != "subscription"
    check_budget(host + (exam_budget if exam_paid else 0.0), "this round", paid=route() != "subscription" or exam_paid)

    n = next_round()
    round_dir, wt, branch = HOME / "rounds" / str(n), HOME / "wt" / str(n), f"harness-loop/{n}"
    round_dir.mkdir(parents=True)
    git("worktree", "add", "-q", "-b", branch, str(wt), base_sha)
    row = {"round": n, "improver": "default" if default else "stub", "tasks_manifest": args.tasks, "branch": branch,
           "pr": None, "idea": None, "files": [], "base_sha": base_sha, "cand_sha": None, "screen": None, "sidecheck": None,
           "full": None, "outcome": None, "reason": None, "improver_cost_usd": None, "ts": None}

    def finish(outcome, reason="", keep_branch=False):
        cleanup(wt, branch, keep_branch)
        row.update(outcome=outcome, reason=reason, ts=now().isoformat())
        append_jsonl(HOME / "history.jsonl", row)
        print(f"round {n}: {outcome}{': ' + reason if reason else ''}")

    before = None
    prop = Path(args.proposals).resolve() if args.proposals and Path(args.proposals).is_file() else None
    try:
        msg = "\n".join([
            "Follow the improver skill. Everything you need is at these paths.",
            f"skill: {wt}/harness-loop/improver/SKILL.md", f"worktree: {wt}",
            f"history: {HOME / 'history.jsonl'}", f"runs: {HOME / 'runs'}", f"tries: {TRIES}",
            f"round_dir: {round_dir}"]
            + ([f"proposals: {prop}"] if prop else []) + [""])
        (round_dir / "message.txt").write_text(msg)
        argv = improver_argv(args, msg, round_dir, prop)
        if not default:
            improver_settings(round_dir, prop)
        before, existing = snapshot(branch, base_run), existing_dirs()
        row["improver_cost_usd"] = "running"
        code, cost, limit = run_improver(argv, wt, round_dir, improver_env(round_dir, default))
        changes = tampered(before, snapshot(branch, base_run))
        restore_state(before["state"])
        row["improver_cost_usd"] = None
        if default:
            row["improver_cost_usd"] = cost if cost is not None else args.improver_budget_usd
            book("improver", row["improver_cost_usd"], n, "" if cost is not None else "cost unknown: booked at cap")
        if changes:
            return finish("dropped", "tamper: " + "; ".join(changes))
        if limit:
            raise RateLimited(f"improver: {limit}")
        if code == "timeout":
            return finish("dropped", f"improver timed out after {IMPROVER_TIMEOUT_S}s")

        cand = git("rev-parse", f"refs/heads/{branch}").strip()
        row["cand_sha"] = cand
        prop_f = round_dir / "proposal.md"
        proposal = prop_f.read_text(errors="replace") if prop_f.is_file() else ""
        idea = re.search(r"^Idea:\s*(.+)$", proposal, re.M)
        row["idea"] = idea[1].strip() if idea else None
        checks, files = run_checks(base_sha, cand, proposal, private, existing, round_n=n)
        row["files"] = sorted({f["new"] if f["status"] != "D" else f["old"] for f in files})
        (round_dir / "checks.json").write_text(json.dumps([{"check": c, "ok": ok, "detail": d}
                                                           for c, ok, d in checks], indent=2) + "\n")
        plus = sum(k == "+" for f in files for k, _, _ in f["lines"])
        minus = sum(k == "-" for f in files for k, _, _ in f["lines"])
        print(f"round {n}: improver changed {', '.join(row['files']) or 'nothing'} "
              f"({len(row['files'])} file(s), +{plus} -{minus})")
        guard = [d for c, ok, d in checks if c == "gate guard" and not ok]
        if guard:
            return finish("rejected", "weakens gates: " + guard[0])
        failed = [f"{c}: {d}" for c, ok, d in checks if not ok]
        if failed:
            return finish("dropped", "; ".join(failed))
        if args.no_screen:
            return finish("checked", "checks pass (--no-screen: no exam, no push, no PR)", keep_branch=True)

        def tamper(main_run):
            changes = tampered(before, snapshot(branch, main_run), ignore_pycache=True, skip_state=("spend.jsonl",))
            if changes:
                restore_state(before["state"])
            return "; ".join(changes)

        def fresh_main(run):
            before["main screen run"] = hash_tree(run)

        def screen_tamper(main_run):
            bad = tamper(main_run)
            return bad and "tamper during screen: " + bad

        base_run, cand_run, stop, stats = screen_against_main(base_sha, cand, manifest, args.budget_usd, n, base_run,
                                                              after_baseline=fresh_main, after_candidate=screen_tamper)
        if stop:
            return finish("dropped", stop)
        verdict = stats["screen"]
        row["screen"] = {"base_run": rel_home(base_run), "cand_run": rel_home(cand_run),
                         "solved_base": int(stats["solvedA"]), "solved_cand": int(stats["solvedB"]),
                         "cost_base": float(stats["costA"]), "cost_cand": float(stats["costB"]),
                         "n": int(stats["n"]), "verdict": verdict}
        (round_dir / "screen.json").write_text(json.dumps({**row["screen"], "compare": stats}, indent=2) + "\n")
        line = score_line("screen", stats, base_sha, cand, tries_of(cand_run), f"screen={verdict}")
        print(f"checks: pass | screen: main {stats['solvedA']}/{stats['n']} ${float(stats['costA']):.2f}, "
              f"candidate {stats['solvedB']}/{stats['n']} ${float(stats['costB']):.2f} -> {verdict}")
        lines, ready, prefix = [line, *early_stop_line(cand_run)], verdict == "pass", "[screen fail] "
        if ready and args.sidecheck:
            side = Path(args.sidecheck).resolve()
            kw = {"screen": False, "side_home": side.parent / SIDE_HOME,
                  "extra": ("--addon", args.sidecheck_addon, "--env", args.sidecheck_env)}
            sa = run_exam(base_sha, side, "baseline", args.sidecheck_budget_usd / 2, n, **kw)
            sc = run_exam(cand, side, "candidate", args.sidecheck_budget_usd / 2, n, **kw)
            bad = tamper(base_run)
            if bad:
                return finish("dropped", "tamper during side check: " + bad)
            s = compare(sa, sc)
            # A budget-cut side run is a few tasks, not the set: no pass from it.
            complete = all(json.loads((r / "run.json").read_text()).get("status") == "complete" for r in (sa, sc))
            guard = "pass" if complete and int(s["solvedB"]) >= int(s["solvedA"]) else "fail"
            lines.append(score_line("side check", s, base_sha, cand, 1,
                                    f"env={args.sidecheck_env} guard={guard}" + ("" if complete else " (cut short)")))
            # No run paths: side run names carry the private set name, and HOME is the improver's to read.
            row["sidecheck"] = {"solved_base": int(s["solvedA"]), "solved_cand": int(s["solvedB"]),
                                "cost_base": float(s["costA"]), "cost_cand": float(s["costB"]), "n": int(s["n"]),
                                "complete": complete, "guard": guard,
                                "line": lines[-1]}  # monthly keeps the line in the rewritten score block
            print(lines[-1])
            ready, prefix = guard == "pass", "[side check fail] "

        label = "harness-loop" if default else "harness-loop-test"
        body = "\n\n".join([
            proposal_for_body(proposal, existing), scores_block(*lines),
            f"Runs (under the loop home): `{rel_home(base_run)}` (main), `{rel_home(cand_run)}` (candidate)",
            f"Round {n}, improver: {row['improver']}. Opened by harness-loop/loop.py; scores are written by the "
            "script from compare.py, never by the improver. The loop never merges."])
        body_f = round_dir / "pr-body.md"
        body_f.write_text(body + "\n")
        title = f"harness-loop round {n}: {(row['idea'] or 'no idea line')[:70]}"
        url, row["pr"] = open_pr(branch, "main", ("" if ready else prefix) + title, body + "\n", label, draft=not ready)
        if ready:
            write_queue(read_queue() + [{"round": n, "branch": branch, "base_sha": base_sha, "cand_sha": cand,
                                         "manifest": str(manifest), "pr": row["pr"]}])
        print(f"PR {url} (label {label}){', queued' if ready else ', draft ' + prefix.strip()}")
        outcome = "queued" if ready else "screen-fail" if verdict != "pass" else "side-fail"
        return finish(outcome, keep_branch=True)
    except BaseException as e:  # every started round gets a history row, Ctrl-C included
        if before and row["pr"] is None:  # an error the improver provoked must not keep what it wrote
            restore_state(before["state"])
        if row["improver_cost_usd"] == "running":  # interrupted mid-improver: its cost is unknown
            row["improver_cost_usd"] = args.improver_budget_usd if default else None
            if default:
                book("improver", args.improver_budget_usd, n, "interrupted: booked at cap")
        if isinstance(e, RateLimited):  # main() exits RATE_LIMITED_EXIT
            finish("paused", f"usage limit, run again once it resets: {e}", keep_branch=row["cand_sha"] is not None)
        else:
            finish("error", f"{e.__class__.__name__}: {e}", keep_branch=row["cand_sha"] is not None)
        raise


def gh_json(*args):
    out = subprocess.run(["gh", *args, "--repo", GH_REPO], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def prior_lines(q, row):
    """The screen and side check lines a queued round's PR block keeps when monthly rewrites it."""
    screen, lines = row.get("screen"), []
    if screen:
        lines.append(score_line("screen", {"n": screen["n"], "solvedA": screen["solved_base"],
                                           "solvedB": screen["solved_cand"], "costA": screen["cost_base"],
                                           "costB": screen["cost_cand"]},
                                q["base_sha"], q["cand_sha"], 1, f"screen={screen['verdict']}"))
    if row.get("sidecheck"):
        lines.append(row["sidecheck"]["line"])
    return lines


def cmd_monthly(args):
    queue = [q for q in read_queue() if gh_json("pr", "view", str(q["pr"]), "--json", "state")["state"] == "OPEN"]
    write_queue(queue)  # a merged or closed PR is never re-examined
    if not queue:
        print("monthly: nothing queued")
        return
    if why := exam_problem():
        die(f"the monthly exam: {why}")
    check_budget(args.budget_usd, "the monthly exam", paid=exam_route()[0] != "subscription")
    git("fetch", "-q", "origin", "main")
    main_sha = git("rev-parse", "origin/main").strip()
    try:
        monthly_exams(args, queue, main_sha)
    finally:  # the restacked commits are only needed until their run_exam has read them
        for q in queue:
            unstack(q["cand_sha"])


def monthly_exams(args, queue, main_sha):
    """cmd_monthly's restack and full exams of the open queued rounds, on main_sha."""
    history = {r["round"]: r for r in read_jsonl(HOME / "history.jsonl")}
    failed, groups, live = [], {}, 0

    def finish(q, lines, full):
        """Record and dequeue first: the exam is paid, so a failed PR edit must not make a rerun pay again."""
        update_history(q["round"], full=full)
        write_queue([x for x in read_queue() if x["round"] != q["round"]])
        body_f = HOME / "rounds" / str(q["round"]) / "pr-body-full.md"  # kept to re-apply by hand on failure
        if not edit_pr_body(q["pr"], lines, body_f):  # carry on: the other rounds share this group's paid main runs
            failed.append(q["round"])
            print(f"monthly: round {q['round']} PR #{q['pr']}: gh pr edit failed; body kept at {body_f}",
                  file=sys.stderr)
            return False
        return True

    for q in queue:  # score each candidate's change on today's main, not the main it was branched from
        scored = restack(q["cand_sha"], main_sha)
        if scored:
            groups.setdefault(q["manifest"], []).append({**q, "scored_sha": scored})
            live += 1
            continue
        line = f"full: stale, re-propose (conflicts with main {main_sha[:7]})"
        lines = prior_lines(q, history.get(q["round"], {})) + [line]
        if finish(q, lines, {"verdict": "stale", "main_sha": main_sha}):
            print(f"monthly: round {q['round']} PR #{q['pr']}: {line}")
    per_run = args.budget_usd / (2 * len(groups) + live) if live else 0
    for manifest, items in groups.items():
        a1 = run_exam(main_sha, manifest, "baseline", per_run, 0, screen=False, tries=2)
        a2 = run_exam(main_sha, manifest, "baseline", per_run, 0, screen=False, tries=2)
        for q in items:
            b = run_exam(q["scored_sha"], manifest, "candidate", per_run, q["round"], screen=False, tries=2)
            stats = compare(a1, b, "--margin-from", str(a2))
            cut = [rel_home(r) for r in (a1, a2, b) if json.loads((r / "run.json").read_text()).get("status") != "complete"]
            if cut:  # a budget-cut run is a few tasks, not the full exam: no verdict from it
                stats["verdict"] = "incomplete"
            full = {"main_run": rel_home(a1), "main_run_2": rel_home(a2), "cand_run": rel_home(b),
                    "main_sha": main_sha, "cand_sha": q["cand_sha"], "scored_sha": q["scored_sha"],
                    "solved_base": int(stats["solvedA"]), "solved_cand": int(stats["solvedB"]),
                    "cost_base": float(stats["costA"]), "cost_cand": float(stats["costB"]), "n": int(stats["n"]),
                    "diff": float(stats["diff"]), "margin": float(stats["margin"]), "verdict": stats["verdict"]}
            lines = prior_lines(q, history.get(q["round"], {})) + [
                score_line("full", stats, main_sha, q["scored_sha"], tries_of(b),
                           f"diff={stats['diff']} margin={stats['margin']} verdict={stats['verdict']}")]
            if finish(q, lines, full):
                print(f"monthly: round {q['round']} PR #{q['pr']}: {lines[-1]}"
                      + (f" (cut short: {', '.join(cut)})" if cut else ""))
    if failed:
        die(f"gh pr edit failed for round(s) {', '.join(map(str, failed))}; results are in history.jsonl", code=1)


def cmd_sync(_args):
    rows = read_jsonl(HOME / "history.jsonl")
    closed = set()
    for r in rows:
        if r.get("pr") and r["outcome"] in ("queued", "screen-fail", "side-fail"):
            state = gh_json("pr", "view", str(r["pr"]), "--json", "state")["state"]
            if state in ("MERGED", "CLOSED"):
                r["outcome"] = state.lower()
                closed.add(r["round"])
                print(f"sync: round {r['round']} PR #{r['pr']} {r['outcome']}")
    write_atomic(HOME / "history.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    write_queue([q for q in read_queue() if q["round"] not in closed])


def post_status(target, state, desc):
    """Commit status harness-canary on target (repo, sha); None (no --post-status) posts nothing."""
    if not target:
        return
    repo, sha = target
    p = subprocess.run(["gh", "api", f"repos/{repo}/statuses/{sha}", "-f", f"state={state}",
                        "-f", f"context={CANARY_CONTEXT}", "-f", f"description={desc[:140]}"],
                       capture_output=True, text=True)
    if p.returncode:
        die(f"could not post status {CANARY_CONTEXT} on {repo}@{sha[:7]}: {p.stderr.strip()[-200:]}",
            code=COULD_NOT_RUN_EXIT)
    print(f"canary: status {CANARY_CONTEXT}={state} on {sha[:7]}")


def could_not_run(target, reason):
    """Not a fail: status error with the reason (public: no paths, no names), exit 5."""
    post_status(target, "error", f"could not run: {reason}")
    die(f"could not run: {reason}", code=COULD_NOT_RUN_EXIT)


def docker_problem():
    """Why `docker info` (EXAM_DOCKER) fails, or ''."""
    try:
        p = subprocess.run([*shlex.split(os.environ.get("EXAM_DOCKER", "docker")), "info"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"Docker is not reachable ({e.__class__.__name__})"
    return "" if p.returncode == 0 else "Docker is not running"


def canary_screen(base, cand, manifest, budget, round_n, reruns, target):
    """The canary's screen of cand against base (commits or installed-tree Paths): the subscription preflight
    alone (exit 4), Docker (exit 5), the task fetch (exit 5), main's run reused or made, the candidate, and on a
    screen fail up to `reruns` fresh candidate runs. Returns (stats of the last compare, last candidate run, reran).
    A RuntimeError (a run_exam or compare crash) is main()'s: could not run."""
    via, reasons = exam_route()
    if via is None:
        die(f"subscription-only: no plan credential works ({'; '.join(reasons)}); run `claude setup-token` once and "
            f"export {OAUTH_TOKEN}, or name a file holding it in {envfile.ENV_FILE}", code=RATE_LIMITED_EXIT)
    print(f"exam route: {via}")
    if why := docker_problem():
        could_not_run(target, why)
    fetch = shlex.split(os.environ.get("LOOP_FETCH_CACHE") or f"{sys.executable} {LOOP_DIR / 'tasks/fetch_cache.py'}")
    if subprocess.run([*fetch, str(manifest)], stdout=sys.stderr).returncode:  # exam setup: only once it is needed
        could_not_run(target, "task fetch failed")
    base_run = find_base_run(base, manifest)
    check_screen_budget(budget, base_run)
    per_run, reran = (budget if base_run else budget / 2), False
    base_run, cand_run, stop, stats = screen_against_main(base, cand, manifest, budget, round_n, base_run)
    if not stop and run_broken(base_run) and run_broken(cand_run):
        could_not_run(target, "main's and the candidate's runs both errored or stopped")
    for _ in range(0 if stop else reruns):
        if stats["screen"] == "pass":
            break
        print("canary: screen fail, rerunning the candidate once more")
        cand_run = run_exam(cand, manifest, "candidate", per_run, round_n, extra=("--early-stop-vs", str(base_run)))
        if run_broken(cand_run):
            could_not_run(target, "the candidate's rerun errored")
        stats, reran = compare(base_run, cand_run, "--screen-rule"), True
    if stop:
        print(f"loop: {stop}", file=sys.stderr)
        could_not_run(target, "main's run settings differ from the candidate's; run again")
    return stats, cand_run, reran


def run_broken(run):
    """True when a run says nothing about the harness: every try errored (a Docker or image build failure, not a
    score), or it stopped incomplete without an early stop (an early stop is a verdict: it can no longer pass)."""
    tries = [json.loads(p.read_text()).get("status") for p in Path(run).glob("*/try-*/result.json")]
    meta = json.loads((Path(run) / "run.json").read_text())
    return (meta.get("status") != "complete" and not meta.get("early_stop")) or all(t == "error" for t in tries)


def canary_verdict(stats, reran, target):
    """Post and print the canary verdict; exit 1 on a fail."""
    n, ok = int(stats["n"]), stats["screen"] == "pass"
    desc = (f"{'pass' if ok else 'fail'}{' (rerun)' if reran else ''} solved {stats['solvedB']}/{n} "
            f"vs main {stats['solvedA']}/{n}")
    post_status(target, "success" if ok else "failure", desc)
    print(f"canary: {desc}")
    if not ok:
        sys.exit(1)


def exam_installed(args):
    """exam --candidate-installed C --baseline-installed B: the canary on two installed trees (no PR, no gate)."""
    trees = [Path(d).resolve() for d in (args.candidate_installed, args.baseline_installed)]
    for d in trees:
        if not (d / ".claude").is_dir():
            die(f"{d} has no .claude dir (pass an installed .claude + AGENTS.md tree)")
        if ((install_harness.installed_version(d) or {}).get("addon")
                and HOME.resolve() == DEFAULT_HOME.resolve()):
            die("an installed tree with an add-on is private: set HARNESS_LOOP_HOME to a private dir, "
                "never ~/.harness-loop (the improver reads its runs/)")
    if args.post_status and not (args.status_repo and args.status_sha):
        die("--post-status with installed trees needs --status-repo owner/name and --status-sha SHA")
    target = args.target = (args.status_repo, args.status_sha) if args.post_status else None
    try:
        same = install_harness.installed_sha256(trees[0]) == install_harness.installed_sha256(trees[1])
    except ValueError as e:  # a .env or other local file: never packed into a sandbox
        die(str(e))
    if same:  # the gate may count paths (hook-seam link targets) the installed trees cannot hold
        post_status(target, "success", "not needed (installed trees identical)")
        print("canary: not needed (installed trees identical)")
        return
    if bad := tree_problems(trees[0]):  # the free dry run, as exam_restacked's load_check
        free = f"dry run fail ({'; '.join(bad)})"
        post_status(target, "failure", free)
        die(f"candidate {trees[0]}: {free}", code=1)
    stats, _, reran = canary_screen(trees[1], trees[0], Path(args.tasks).resolve(), args.budget_usd, "canary",
                                    args.rerun_on_fail, target)
    canary_verdict(stats, reran, target)


def cmd_exam(args):
    installed = args.candidate_installed or args.baseline_installed
    if (args.pr is None) == (not installed) or (installed and not (args.candidate_installed and args.baseline_installed)):
        die("exam: give --pr N, or --candidate-installed DIR and --baseline-installed DIR")
    if (installed or args.rerun_on_fail or args.post_status) and not args.subscription_only:
        die("exam: --candidate-installed, --rerun-on-fail and --post-status need --subscription-only (the canary)")
    if args.subscription_only and os.environ.get("CLAUDE_CODE_REMOTE"):
        die("could not run: the canary is laptop-only (CLAUDE_CODE_REMOTE is set); nothing posted",
            code=COULD_NOT_RUN_EXIT)
    if installed:
        return exam_installed(args)
    pr = gh_json("pr", "view", str(args.pr), "--json", "state,headRefOid,headRefName,body"
                 + (",isCrossRepository,headRepositoryOwner" if args.subscription_only else ""))
    if args.subscription_only and pr.get("isCrossRepository"):
        die("could not run: fork PR, not run with the plan token; nothing posted", code=COULD_NOT_RUN_EXIT)
    if pr["state"] != "OPEN":
        die(f"PR #{args.pr} is {pr['state']}, not OPEN")
    # the PR head gh named: where the status goes (a git failure from here on posts error there, see main)
    args.target = (args.status_repo or GH_REPO, pr["headRefOid"]) if args.post_status and not args.check_only else None
    git("fetch", "-q", "origin", "main")
    git("fetch", "-q", "origin", f"pull/{args.pr}/head")
    head = git("rev-parse", "FETCH_HEAD").strip()
    if head != pr["headRefOid"]:
        moved = f"PR #{args.pr} moved while fetching ({pr['headRefOid'][:7]} -> {head[:7]}); run again"
        if args.subscription_only:
            could_not_run(args.target, moved)
        die(moved)
    main_sha = git("rev-parse", "origin/main").strip()
    needs, reason = path_gate(main_sha, head)
    print(f"exam: PR #{args.pr} ({pr['headRefName']} {head[:7]}): "
          f"{'exam needed' if needs else 'no exam needed'}: {reason}")
    body_f = HOME / "exams" / f"pr-{args.pr}" / "pr-body.md"

    edit_failed = []

    def post(*lines):
        if not edit_pr_body(args.pr, lines, body_f):
            if not args.subscription_only:
                die(f"gh pr edit failed; body kept at {body_f}", code=1)
            edit_failed.append(body_f)  # the canary posts its status first (the merge gate reads it), then exits 5
        print("\n".join(lines))

    try:
        if not needs:
            if not args.check_only:
                post(f"exam: not needed ({reason})")
                post_status(args.target, "success", "not needed (no loaded file)")
            return
        cand = restack(head, main_sha)  # score the change as it would land on today's main
        try:
            exam_restacked(args, main_sha, head, cand, post)
        finally:
            unstack(head)
    finally:
        if edit_failed:  # replaces the verdict's exit (0 or 1): the score block never reached the PR
            die(f"gh pr edit failed; body kept at {edit_failed[0]}", code=COULD_NOT_RUN_EXIT)


def exam_restacked(args, main_sha, head, cand, post):
    """cmd_exam after the path gate: the free checks on cand (head restacked on main, None on a conflict),
    written to the PR before the model route or the budget is asked for, then the screen."""
    free = None
    if cand is None:
        free = "exam: stale, rebase the PR onto main"
    else:
        load = load_check(cand)  # the free dry run; the paid one is the candidate's first (cheapest) task
        guard, _ = guard_commits(git("merge-base", main_sha, head).strip(), head)
        print(f"exam: load check: {'; '.join(load) or 'pass'}")
        print(f"exam: gate guard (advisory): {'; '.join(guard) or 'pass'}")
        if load:
            free = f"exam: dry run fail ({'; '.join(load)})"
    if free:
        if args.check_only:
            print(free)
        else:
            post(free)
            post_status(args.target, "failure", free.removeprefix("exam: "))
        die(f"PR #{args.pr}: {free.removeprefix('exam: ')} (main {main_sha[:7]})", code=1)
    if args.check_only:
        return
    if args.subscription_only:
        stats, cand_run, reran = canary_screen(main_sha, cand, Path(args.tasks).resolve(), args.budget_usd,
                                               f"pr-{args.pr}", args.rerun_on_fail, args.target)
        post(score_line("screen", stats, main_sha, cand, tries_of(cand_run),
                        f"screen={stats['screen']}" + (" (rerun)" if reran else "")),
             *early_stop_line(cand_run), *([f"gate guard (advisory): {'; '.join(guard)}"] if guard else []))
        return canary_verdict(stats, reran, args.target)
    if why := bedrock_problem(os.environ) or exam_problem():
        die(f"the exam: {why}")
    n, manifest = f"pr-{args.pr}", Path(args.tasks).resolve()
    base_run = find_base_run(main_sha, manifest)
    check_screen_budget(args.budget_usd, base_run)
    check_budget(args.budget_usd, "the exam", paid=exam_route()[0] != "subscription")
    base_run, cand_run, stop, stats = screen_against_main(main_sha, cand, manifest, args.budget_usd, n, base_run)
    if stop:
        die(stop, code=1)
    post(score_line("screen", stats, main_sha, cand, tries_of(cand_run), f"screen={stats['screen']}"),
         *early_stop_line(cand_run), *([f"gate guard (advisory): {'; '.join(guard)}"] if guard else []))


def cmd_exam_route(args):
    """Print the exam's route and what was passed over (one preflight call per credential tried); exit 1 if none
    (--subscription-only: the subscription's preflight alone, exit 4 if it fails)."""
    if why := exam_problem():
        die(why, code=RATE_LIMITED_EXIT if args.subscription_only else 1)


def cmd_guard(args):
    reasons, _ = guard_commits(args.base or f"{args.commit}^", args.commit)
    for r in reasons:
        print(f"guard: {r}")
    if reasons:
        print("guard: rejected: weakens gates")
        sys.exit(1)
    print("guard: pass")


def cmd_leak(args):
    if not args.diff and not args.body:
        die("leak: give --diff <ref> and/or --body <file>, e.g. leak --diff origin/main --body pr.md")
    if not (HOME / "leak-patterns.txt").is_file():
        die(f"{HOME / 'leak-patterns.txt'} is missing: put private names there, one regex per line "
            "(an empty file means none)", code=3)
    lines = []  # (file, line number, text)
    if args.diff:
        diff = git("diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--text", f"{args.diff}...HEAD",
                   cwd=Path.cwd())
        lines += [(f["new"], n, t) for f in parse_diff(diff) for kind, n, t in f["lines"] if kind == "+"]
    if args.body:
        body = Path(args.body)
        lines += [(body.name, n, t) for n, t in enumerate(body.read_text().splitlines(), 1)]
    private = private_patterns()
    hits = [(f, n, t, rule) for f, n, t in lines if (rule := leak_rule(t, private))]
    for f, n, t, rule in hits:
        print(f"leak: {f}:{n} ({rule})" if args.redact else f"leak: {t.strip()[:200]}")
    if hits:
        sys.exit(1)
    print(f"leak: clean ({len(lines)} lines)")


def blob(ref, path):
    """The raw bytes of path at ref, or None."""
    p = subprocess.run(["git", "-C", str(AW_ROOT), "cat-file", "blob", f"{ref}:{path}"], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def port_file(path, data, base_ref, pinned, wt, tmp):
    """Put the installed bytes `data` of path into worktree wt (at base_ref): as they are when base_ref still has the
    pinned version, else merged three ways with the pinned version as base. 'ported', 'same' (base_ref already has
    the result) or 'conflict' (a conflict, a binary refusal, or a side missing the path)."""
    ours, old = blob(base_ref, path), blob(pinned, path)
    if ours is None or old is None:
        return "conflict"
    if ours != old:
        sides = []
        for name, b in (("ours", ours), ("pinned", old), ("installed", data)):
            (Path(tmp) / name).write_bytes(b)
            sides.append(str(Path(tmp) / name))
        merged = subprocess.run(["git", "merge-file", "-p", *sides], capture_output=True)
        if merged.returncode != 0:
            return "conflict"
        data = merged.stdout
    if data == ours:
        return "same"
    (wt / path).write_bytes(data)  # into the existing file: keeps its mode
    return "ported"


def cmd_port(args):
    src = Path(args.src)
    if (src / ".claude/HARNESS_FILES.tsv").is_file():  # the project root given: read its .claude dir
        src /= ".claude"
    if not src.is_dir():
        die(f"port: --from {src} is not a directory (give the installed .claude dir)")
    if not (src / "HARNESS_FILES.tsv").is_file():
        die(f"port: no HARNESS_FILES.tsv in {src}: the install predates it; reinstall with install_harness.py --force",
            code=3)
    try:
        pinned = json.loads((src / "HARNESS_VERSION").read_text())["commit"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        die(f"port: {src / 'HARNESS_VERSION'} names no commit: {e}")
    private = private_patterns()
    base_ref = f"origin/{args.base}"
    git("fetch", "-q", "origin", args.base)
    try:
        git("cat-file", "-e", f"{pinned}^{{commit}}")
    except RuntimeError:
        die(f"port: pinned commit {pinned} is not in {AW_ROOT}")
    root = src.resolve().parent
    p = subprocess.run([sys.executable, str(AW_ROOT / ".claude/bin/agent-cli"), "harness", "drift", "--target",
                        str(root), "--json"], capture_output=True, text=True)
    try:  # exit 1 = other drift (addon_edited, unowned...): base_edited is still good to port
        edited = json.loads(p.stdout)["base_edited"] if p.returncode in (0, 1) else None
    except (ValueError, KeyError, TypeError):
        edited = None
    if not isinstance(edited, list):
        die(f"port: harness drift failed (exit {p.returncode}): {p.stderr.strip()[:300]}")
    tsv = (src / "HARNESS_FILES.tsv").read_text().splitlines()[1:]
    rows = {r[0]: r[1:3] for r in (ln.split("\t") for ln in tsv)}  # path -> [origin, install-time sha256]
    # Deliberate defense in depth beside drift: port only `base` rows (never base+addon: it carries project hooks)
    # whose install-time sha256 is the pinned commit's bytes. Any installer transform of a base file (a .skipbase
    # rewriting settings.json) would otherwise send the project's opt-outs upstream as deletions.
    paths = []
    for f in sorted(f for f in edited if rows.get(f, [])[:1] == ["base"]):
        pin = blob(pinned, f)
        if pin is None or [hashlib.sha256(pin).hexdigest()] != rows[f][1:]:
            print(f"port: skipped {f}: the installer changed it (not a byte copy of {pinned[:7]}), edit the add-on "
                  "instead")
            continue
        paths.append(f)
    if not paths:
        print("port: nothing to port")
        return
    installed = {f: (root / f).read_bytes() for f in paths}
    paths = [f for f in paths if installed[f] != blob(base_ref, f)]
    if not paths:
        print("port: nothing to port (already upstream)")
        return
    branch = "harness-port/" + hashlib.sha256("\n".join(paths).encode()).hexdigest()[:8]
    with tempfile.TemporaryDirectory(prefix="loop-port-") as tmp:
        wt = Path(tmp) / "wt"
        git("worktree", "add", "-q", "-B", branch, str(wt), base_ref)
        try:  # the local branch and worktree always go; the pushed branch is the record
            port_commit(args, branch, base_ref, pinned, private,
                        {f: port_file(f, installed[f], base_ref, pinned, wt, tmp) for f in paths}, wt)
        finally:
            cleanup(wt, branch, keep_branch=False)


def port_commit(args, branch, base_ref, pinned, private, done, wt):
    """Commit port_file's results in wt, gate them (leak scan, load check), then push and open or update the PR."""
    ported = [f for f, s in done.items() if s == "ported"]
    skipped = [f for f, s in done.items() if s == "conflict"]
    if not ported:
        if not skipped:
            print("port: nothing to port (already upstream)")
            return
        for f in skipped:
            print(f"port: conflict, not ported: {f}")
        sys.exit(1)
    title = f"harness-port: {len(ported)} file(s) from {args.source_label}"
    git("-c", "core.hooksPath=/dev/null", "commit", "-q", "--no-verify", "-a", "-m", title, cwd=wt)
    cand = git("rev-parse", "HEAD", cwd=wt).strip()
    body = "\n".join([
        *(f"- {f}: installed from {pinned[:7]}" for f in ported), "",
        f"Source: {args.source_label}", f"Date: {os.environ.get('HARNESS_PORT_TODAY') or now().date().isoformat()}",
        *(["", "Skipped (conflict):", *(f"- {f}" for f in skipped)] if skipped else []), "",
        "Opened by harness-loop/loop.py port from edited base files of an installed harness. The loop never merges."])
    hits = change_leaks(base_ref, cand, body, private)
    for h in hits:
        print(f"port: leak: {h.strip()[:200]}")
    reasons = [] if hits else load_check(cand)
    for r in reasons:
        print(f"port: load check: {r}")
    if hits or reasons:
        sys.exit(1)
    what = f"{len(ported)} file{'s' if len(ported) != 1 else ''}: {', '.join(ported)}"
    open_prs = gh_json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
    if open_prs:
        n = open_prs[0]["number"]
        push_branch(branch, force=True)
        e = subprocess.run(["gh", "pr", "edit", str(n), "--repo", GH_REPO, "--title", title, "--body", body],
                           capture_output=True, text=True)
        if e.returncode != 0:
            die(f"port: gh pr edit #{n} failed: {e.stderr.strip()[:300]}", code=1)
        print(f"port: updated PR #{n} {branch} ({what})")
    else:
        _, n = open_pr(branch, args.base, title, body, draft=args.draft, force=True)
        print(f"port: PR #{n} {branch} ({what})")


def usd(text):
    """argparse type: a finite dollar amount >= 0 (a negative row would hand the week money back)."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a dollar amount: {text!r}") from None
    if not 0 <= v < float("inf"):
        raise argparse.ArgumentTypeError(f"must be a finite amount >= 0: {text!r}")
    return v


def cmd_budget(args):
    spent = check_budget(args.est, args.what, code=1)
    if route() == "subscription":
        use = week_notional(read_jsonl(HOME / "spend.jsonl"), now())
        print(f"budget: ok, subscription run (no money; the ${WEEKLY_BUDGET_USD:.2f} cap counts only paid runs, "
              f"${spent:.2f} of it spent); subscription use in the last 7 days: ${use:.2f} notional")
        return
    print(f"budget: ok, ${spent:.2f} spent in the last 7 days + ${args.est:.2f} <= ${WEEKLY_BUDGET_USD:.2f} weekly cap")


LOOP_KINDS = re.compile(r"^(improver|project-check|exam-.*|sidecheck-.*)$")


def cmd_book(args):
    if LOOP_KINDS.match(args.kind):
        die(f"book: kind {args.kind!r} is one the loop books itself; use another, e.g. --kind gardener")
    book(args.kind, args.usd, "gardener", args.note)
    print(f"book: {args.kind} ${args.usd:.4f}" + (" notional (subscription: $0 money)" if route() == "subscription" else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("round")
    r.add_argument("--tasks", required=True)
    r.add_argument("--improver-cmd")
    r.add_argument("--no-screen", action="store_true")
    r.add_argument("--proposals", help="a file of harness-wide ideas the improver reads first (skipped if missing)")
    r.add_argument("--budget-usd", type=float, default=DEFAULT_EXAM_BUDGET_USD,
                   help="most the round's exam runs may spend together")
    r.add_argument("--improver-budget-usd", type=float, default=IMPROVER_MAX_USD)
    r.add_argument("--sidecheck", help="private manifest sat by main and candidate after a screen pass")
    r.add_argument("--sidecheck-addon", help="the add-on dir for --sidecheck (passed to run_exam.py --addon)")
    r.add_argument("--sidecheck-env", default="docker", choices=["docker", "daytona"])
    r.add_argument("--sidecheck-budget-usd", type=float, default=DEFAULT_SIDECHECK_BUDGET_USD)
    m = sub.add_parser("monthly")
    m.add_argument("--budget-usd", type=float, default=DEFAULT_EXAM_BUDGET_USD)
    sub.add_parser("sync")
    e = sub.add_parser("exam", help="screen one open PR (or two installed trees) against main; canary exit codes: "
                       "0 pass/not needed, 1 fail, 2 usage, 4 paused/no plan token, 5 could not run")
    e.add_argument("--pr", type=int)
    e.add_argument("--tasks", required=True)
    e.add_argument("--budget-usd", type=float, default=DEFAULT_EXAM_BUDGET_USD)
    e.add_argument("--check-only", action="store_true", help="path gate and free checks only: no spend, no PR edit")
    e.add_argument("--subscription-only", action="store_true",
                   help="the canary: plan token only (CLAUDE_CODE_OAUTH_TOKEN or HARNESS_EXAM_ENV_FILE), Docker "
                   "running, laptop only, no fork PRs")
    e.add_argument("--rerun-on-fail", type=int, default=0, metavar="N", help="rerun the candidate up to N times "
                   "on a screen fail; the verdict is the last compare")
    e.add_argument("--post-status", action="store_true", help=f"post commit status {CANARY_CONTEXT}")
    e.add_argument("--status-repo", metavar="OWNER/NAME", help=f"repo of the status (default {GH_REPO})")
    e.add_argument("--status-sha", metavar="SHA", help="commit of the status (installed mode)")
    e.add_argument("--candidate-installed", metavar="DIR", help="an installed .claude + AGENTS.md tree (no --pr)")
    e.add_argument("--baseline-installed", metavar="DIR", help="main's installed tree (no --pr)")
    er = sub.add_parser("exam-route", help="which credential the exam would run on (one cheap preflight call each); "
                        "exit 1 if none works")
    er.add_argument("--subscription-only", action="store_true",
                    help="preflight the subscription token alone; exit 4 if it does not answer")
    g = sub.add_parser("guard", help="run the round's gate guard on one commit (exit 1 = rejected)")
    g.add_argument("commit")
    g.add_argument("--base", help="diff base (default: <commit>^)")
    lk = sub.add_parser("leak", help="free leak scan of a branch's added lines and a PR body (exit 1 = hits)")
    lk.add_argument("--diff", metavar="REF", help="scan lines added in REF...HEAD")
    lk.add_argument("--body", help="PR body file to scan")
    lk.add_argument("--redact", action="store_true",
                    help="print each hit as <file>:<line> (<rule>) without the line text (for public CI logs)")
    bu = sub.add_parser("budget", help="exit 0 if a run of --est USD fits the weekly cap, 1 (with the reason) if not")
    bu.add_argument("--est", type=usd, required=True, help="most the run may spend, in USD")
    bu.add_argument("--what", required=True, help="what is asking, named in the refusal (e.g. gardener)")
    bk = sub.add_parser("book", help="append one spend row for a run outside the loop (round \"gardener\")")
    bk.add_argument("--kind", required=True, help="the row's kind, e.g. gardener")
    bk.add_argument("--usd", type=usd, required=True, help="what the run cost, in USD")
    bk.add_argument("--note", default="", help="free text kept on the row (e.g. the report dir)")
    pt = sub.add_parser("port", help="open one upstream PR from the base files an installed harness edited "
                        "(exit 1 = a gate refused, 3 = no HARNESS_FILES.tsv)")
    pt.add_argument("--from", dest="src", required=True, help="the installed .claude dir")
    pt.add_argument("--source-label", required=True, help="free text naming the source in the PR, never a company name")
    pt.add_argument("--base", default="main", help="branch to port onto and open the PR against (default: main)")
    pt.add_argument("--draft", action="store_true", help="open the PR as a draft")
    args = ap.parse_args(argv)
    try:
        route()
    except ValueError as e:
        die(str(e))
    global SUBSCRIPTION_ONLY
    SUBSCRIPTION_ONLY = getattr(args, "subscription_only", False)
    if SUBSCRIPTION_ONLY:  # every call this process makes stays on the plan, never Bedrock or the API
        os.environ["HARNESS_MODEL_ACCESS"] = "subscription"
    try:
        {"round": cmd_round, "monthly": cmd_monthly, "sync": cmd_sync, "exam": cmd_exam, "exam-route": cmd_exam_route, "guard": cmd_guard,
         "leak": cmd_leak, "budget": cmd_budget, "book": cmd_book, "port": cmd_port}[args.cmd](args)
    except RateLimited as e:
        die(f"paused: {e}; nothing fell back to a paid route, run again once the limit resets", code=RATE_LIMITED_EXIT)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        if not SUBSCRIPTION_ONLY:
            raise
        print(f"loop: {e}", file=sys.stderr)  # local only: the status gets no paths or names
        could_not_run(getattr(args, "target", None), "the exam stopped on an error (see the local log)")


if __name__ == "__main__":
    main()
