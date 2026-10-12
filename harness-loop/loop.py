#!/usr/bin/env python3
"""The harness loop: one improvement round at a time, checked and scored by scripts, never by Claude.

    loop.py round --tasks <manifest> [--improver-cmd CMD] [--no-screen] [--budget-usd X]
                  [--improver-budget-usd Y]
    loop.py monthly [--budget-usd X]
    loop.py sync
    loop.py guard <commit> [--base <ref>]

round: make worktree $HARNESS_LOOP_HOME/wt/<n> on branch harness-loop/<n> off origin/main;
write rounds/<n>/message.txt (paths only); hash, in this process's memory only, the main
screen run it will reuse, history.jsonl, queue.json, spend.jsonl, and this checkout's state
(git status, HEAD, every ref but the round's, every file under harness-loop/, the shared
.git config/hooks/info and the user's git config); run the
improver (default: `claude -p --model opus` on Bedrock, see IMPROVER_ENV); re-check all of it
and drop the round on any change; run the checks (one commit; only .claude/** or AGENTS.md;
no EXAM_MODE; Exam mode section untouched; the gate guard; leak scan; proposal.md with an
Idea: line and >= 2 try folders on disk; load check); then, unless --no-screen, screen the
candidate against main with run_exam.py --screen and compare.py --screen-rule, push, and open
one PR whose score block is written here from compare's output. Screen pass: ready PR, queued.
Screen fail: draft PR titled "[screen fail] ...". The loop never merges.

monthly: full exam (--tries 2, no --screen) on main twice and each queued branch; compare.py
--margin-from; rewrites each PR's score block (screen + full line, verdict). sync: reads PR
states with gh and records merged/closed. guard: the round's gate guard on <commit>^..<commit>
(exit 1 and "rejected: weakens gates" on rejection).

Outcomes in history.jsonl: queued (screen pass, PR open), screen-fail (draft PR), dropped (a
check failed), rejected (the gate guard: the change weakens a gate, reviewer or test),
checked (--no-screen, all checks pass), error, merged, closed.

Budget: WEEKLY_BUDGET_USD over a rolling 7 days of $HARNESS_LOOP_HOME/spend.jsonl, which only
this script writes (improver cost from claude's result line, exam cost from each run's
summary.json). A round that could take the week past the cap (improver budget + exam budget)
is refused before anything starts; so is a monthly.

Leak scan: /Users/ always; tool names (EXAMPLE_LEAK) unless the line has "e.g."; plus private
names, one regex per line, from $HARNESS_LOOP_HOME/leak-patterns.txt, which must exist (an
empty file is a decision; a missing one is a mistake, so the round refuses).

Env: HARNESS_LOOP_HOME (default ~/.harness-loop); LOOP_RUN_EXAM / LOOP_COMPARE replace the
run_exam.py / compare.py command (tests use fakes).
"""
import argparse
import datetime
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
HOME = Path(os.environ.get("HARNESS_LOOP_HOME") or Path.home() / ".harness-loop")
TRIES = Path.home() / ".claude/tries/agent-workflow"
GH_REPO = "nguyenv119/agent-workflow"

WEEKLY_BUDGET_USD = 50.0  # Long, 12 Oct 2026
IMPROVER_MAX_USD = 10.0
IMPROVER_TIMEOUT_S = 3600
DEFAULT_EXAM_BUDGET_USD = 30.0

SCORES_OPEN = "<!-- harness-loop:scores v1 -->"
SCORES_CLOSE = "<!-- /harness-loop:scores -->"
STATE_FILES = ("history.jsonl", "queue.json", "spend.jsonl", "leak-patterns.txt")
ALLOWED_PATH = re.compile(r"^(\.claude/.+|AGENTS\.md)$")
EXAM_WORDS = re.compile(r"EXAM_MODE|exam mode", re.I)
EXAMPLE_LEAK = re.compile(r"Neon|NEON_|pnpm|turbo|vitest|VITEST|rtk |PGlite|Trigger\.dev|Vercel|race-|svc-", re.I)
TRY_PATH = re.compile(r"(?<![\w/.~-])((?:runs/|~/\.claude/tries/agent-workflow/)[^\s`'\"<>()\[\],;]+)")
# The improver gets Bedrock only: never an Anthropic API key, OAuth token or admin AWS key.
SCRUB_ENV = re.compile(r"^(ANTHROPIC_BASE_URL|AWS_ADMIN_.*|AWS_PROFILE"
                       r"|.*(TOKEN(?!S)|SECRET|KEY|PASSWORD|CREDENTIAL).*)$", re.I)
BEDROCK_ENV = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"}

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
FEWER = re.compile(r"\b(?:only|just|a single)\s+(?:one\s+|a\s+)?(?:reviewers?|reviews?|gates?|tests?)\b(?!-)"
                   r"|\binstead of (?:all |the )?(?:three|3|every)\b"
                   r"|(?<!not )(?<!never )(?<!n't )\b(?:push|merge|commit|open)\w*\s+before\s+(?:the\s+)?"
                   r"(?:\w+\s+)?" + _GATE_NOUN + r"\s+(?:finish|pass|complete|report|return)\w*", re.I)
NO_RUN = re.compile(r"\b(?:do not|don't|never|no need to|without|stop|avoid)\s+(?:run|running|execute|executing"
                    r"|spawn|spawning|write|writing|wait|waiting|call|calling)\b(?:\W+\w+){0,2}?\W+"
                    + GATE_WORDS.pattern, re.I)
PROTECTED = re.compile(r"^\.claude/(skills/(reviewer-[^/]+|test-runner|standards)/|hooks/)")


def die(msg, code=2):
    print(f"loop: {msg}", file=sys.stderr)
    sys.exit(code)


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


def check_budget(est, what):
    spent = week_spent(read_jsonl(HOME / "spend.jsonl"), now())
    if spent + est > WEEKLY_BUDGET_USD + 1e-9:
        die(f"refusing {what}: ${spent:.2f} spent in the last 7 days + up to ${est:.2f} "
            f"> ${WEEKLY_BUDGET_USD:.2f} weekly cap")
    return spent


BOOKED = []  # every ledger row this process wrote; restore_state puts them back


def book(kind, usd, round_n, note=""):
    row = {"ts": now().isoformat(), "kind": kind, "round": round_n, "usd": round(float(usd), 4), "note": note}
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


def guard_commits(base, cand, repo=AW_ROOT):
    diff = git("diff", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--text", base, cand, cwd=repo)

    def at(ref):
        def read(path):
            p = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True, text=True)
            return p.stdout if p.returncode == 0 else None
        return read
    return gate_guard(diff, at(base), at(cand)), diff


# ---- checks ------------------------------------------------------------------------------

def private_patterns():
    f = HOME / "leak-patterns.txt"
    if not f.is_file():
        die(f"{f} is missing: put private names there, one regex per line (an empty file means none)")
    pats = [line.strip() for line in f.read_text().splitlines() if line.strip() and not line.startswith("#")]
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None


def leak_hits(lines, private):
    return [line for line in lines
            if "/Users/" in line or (private and private.search(line))
            or (EXAMPLE_LEAK.search(line) and "e.g." not in line)]


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
    ~/.claude/tries/agent-workflow/..., each an existing dir inside its root (and in `existing`, if given)."""
    valid, invalid = [], []
    for p in dict.fromkeys(m.rstrip(".:") for m in TRY_PATH.findall(text)):
        root, rest = (HOME / "runs", p[len("runs/"):]) if p.startswith("runs/") else (TRIES, p.split("agent-workflow/", 1)[1])
        target = (root / rest).resolve()
        ok = (target.is_dir() and target.is_relative_to(root.resolve()) and target != root.resolve()
              and (existing is None or target in existing))
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


def run_checks(base, cand, proposal, private, existing=None):
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

    def show(ref):
        p = subprocess.run(["git", "-C", str(AW_ROOT), "show", f"{ref}:{coord}"], capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""
    same = section(show(base), "## Exam mode") == section(show(cand), "## Exam mode")
    out.append(("Exam mode section untouched", same, "" if same else "Exam mode section changed"))
    reasons, _ = guard_commits(base, cand)
    out.append(("gate guard", not reasons, "; ".join(reasons)))
    message = git("log", "-1", "--format=%B", cand)
    added = [t for f in files for kind, _, t in f["lines"] if kind == "+"]
    hits = leak_hits(added + message.splitlines() + proposal.splitlines(), private)
    out.append(("leak scan", not hits, "leak: " + "; ".join(h.strip()[:80] for h in hits) if hits else ""))
    idea = re.search(r"^Idea:\s*\S", proposal, re.M)
    valid, invalid = try_folders(proposal, existing)
    ok = bool(proposal) and bool(idea) and len(valid) >= 2
    detail = ("no proposal.md" if not proposal else "no Idea: line" if not idea else
              f"{len(valid)} valid try folders (need 2){'; not found: ' + ', '.join(invalid) if invalid else ''}")
    out.append(("proposal", ok, detail))
    load = load_check(cand)
    out.append(("load check", not load, "; ".join(load)))
    return out, files


# ---- exam --------------------------------------------------------------------------------

def tool(env_var, script):
    cmd = os.environ.get(env_var)
    return shlex.split(cmd) if cmd else [sys.executable, str(LOOP_DIR / "exam" / script)]


def exam_env():
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "HARNESS_LOOP_HOME": str(HOME)}


def run_exam(sha, manifest, kind, budget, round_n, screen=True, tries=1):
    """Run one exam, book its cost, return the run dir."""
    cmd = tool("LOOP_RUN_EXAM", "run_exam.py") + ["--harness", sha, "--tasks", str(manifest),
                                                   "--budget-usd", f"{budget:.2f}", "--kind", kind,
                                                   "--tries", str(tries)] + (["--screen"] if screen else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=exam_env())
    except BaseException:
        book(f"exam-{kind}", budget, round_n, "interrupted: booked at budget")
        raise
    m = re.search(r"^run_exam: (\S+) (complete|incomplete)", p.stdout, re.M)
    if not m:
        raise RuntimeError(f"run_exam left no run (exit {p.returncode}): {p.stderr.strip()[-300:]}")
    run = Path(m[1])
    cost = json.loads((run / "summary.json").read_text()).get("total_cost_usd") or 0
    book(f"exam-{kind}", cost, round_n, run.name)
    return run


def compare(a, b, *extra):
    p = subprocess.run(tool("LOOP_COMPARE", "compare.py") + [str(a), str(b), *extra],
                       capture_output=True, text=True, env=exam_env())
    if p.returncode != 0:
        raise RuntimeError(f"compare.py exit {p.returncode}: {p.stderr.strip()[-300:]}")
    return dict(tok.split("=", 1) for tok in p.stdout.split() if "=" in tok)


def find_base_run(base_sha, manifest):
    """Newest complete, sealed main screen run at base_sha on this manifest that this script booked, or None.

    Booked = named in an exam-baseline row of spend.jsonl, which the improver cannot write (it is
    hashed and restored every round), so a run dir the improver forged is never reused."""
    booked = {r.get("note") for r in read_jsonl(HOME / "spend.jsonl") if r.get("kind") == "exam-baseline"}
    found = []
    for meta_f in (HOME / "runs").glob("*/run.json"):
        try:
            meta = json.loads(meta_f.read_text())
        except ValueError:
            continue
        if (meta.get("harness_commit") == base_sha and meta.get("manifest") == str(Path(manifest).resolve())
                and meta.get("screen") and meta.get("agent") == "workflow" and meta.get("kind") == "baseline"
                and meta.get("status") == "complete" and (meta_f.parent / "summary.sha256").is_file()
                and meta_f.parent.name in booked):
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


def mask_task(path):
    """runs/<run>/<task>/try-N -> runs/<run>/task-<sha8>/try-N: the PR names no exam task (proposal.md keeps it)."""
    parts = path.split("/")
    if parts[0] == "runs" and len(parts) > 3:
        parts[2] = "task-" + hashlib.sha256(parts[2].encode()).hexdigest()[:8]
    return "/".join(parts)


def proposal_for_body(proposal, existing=None):
    """proposal.md with its Evidence list reduced to the cited paths (exam task dirs masked) and no marker lines."""
    out, skip = [], False
    proposal = re.sub(re.escape(SCORES_OPEN) + r".*?" + re.escape(SCORES_CLOSE), "", proposal, flags=re.S)
    for line in proposal.splitlines():
        if re.match(r"^[A-Z][\w ]*:", line):
            skip = line.startswith("Evidence:")
            if skip:
                out.append("Evidence (task dirs masked; notes in the round's proposal.md):")
                out += [f"  {mask_task(t)}" for t in try_folders(proposal, existing)[0]]
                continue
        if not skip and "harness-loop:scores" not in line:
            out.append(line)
    return "\n".join(out).strip()


# ---- the improver ------------------------------------------------------------------------

def improver_settings(round_dir):
    deny = []
    for p in (f"{AW_ROOT}/**", f"{HOME}/runs/**", *(f"{HOME}/{n}" for n in STATE_FILES)):
        deny += [f"{tool_name}(/{p})" for tool_name in ("Edit", "Write", "NotebookEdit")]
    deny += ["Bash(git push:*)", "Bash(gh:*)"]
    f = round_dir / "improver-settings.json"
    f.write_text(json.dumps({"permissions": {"deny": deny}}, indent=2) + "\n")
    return f


def improver_env(round_dir, default):
    env = {k: v for k, v in os.environ.items() if k in BEDROCK_ENV or not SCRUB_ENV.match(k)}
    env.update(HARNESS_LOOP_ROUND_DIR=str(round_dir), HARNESS_LOOP_HOME=str(HOME))
    if default:
        env["CLAUDE_CONFIG_DIR"] = str(round_dir / "claude-config")  # no user settings, memory or plugins
    return env


def improver_argv(args, msg, round_dir):
    if args.improver_cmd:
        argv = shlex.split(args.improver_cmd)
        if not Path(argv[0]).is_absolute() and Path(argv[0]).exists():
            argv[0] = str(Path(argv[0]).resolve())
        return argv + [msg]
    return ["claude", "-p", "--model", "opus", "--permission-mode", "bypassPermissions",
            "--settings", str(improver_settings(round_dir)), "--max-budget-usd", f"{args.improver_budget_usd:.2f}",
            "--output-format", "stream-json", "--verbose", msg]


def run_improver(argv, wt, round_dir, env):
    """Run the improver in its own process group; kill whatever it leaves behind. (exit, cost|None)."""
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
    for line in (round_dir / "improver-transcript.jsonl").read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "result" and rec.get("total_cost_usd") is not None:
            cost = float(rec["total_cost_usd"])
    return code, cost


# ---- commands ----------------------------------------------------------------------------

def next_round():
    nums = [int(p.name) for p in (HOME / "rounds").glob("*") if p.name.isdigit()]
    return max(nums, default=0) + 1


def cleanup(wt, branch, keep_branch):
    git("worktree", "remove", "--force", str(wt), check=False)
    if not keep_branch:
        git("branch", "-D", branch, check=False)


def cmd_round(args):
    default = not args.improver_cmd
    if default:
        if os.environ.get("CLAUDE_CODE_USE_BEDROCK") != "1" or not os.environ.get("AWS_ACCESS_KEY_ID"):
            die("the default improver runs on Bedrock only: set CLAUDE_CODE_USE_BEDROCK=1 and AWS_ACCESS_KEY_ID")
        if args.improver_budget_usd > IMPROVER_MAX_USD:
            die(f"--improver-budget-usd is capped at ${IMPROVER_MAX_USD:.2f}")
    private = private_patterns()
    if git("status", "--porcelain"):
        die(f"loop checkout {AW_ROOT} is not clean; run from a clean, dedicated clone")
    git("fetch", "-q", "origin", "main")
    base_sha = git("rev-parse", "origin/main").strip()
    manifest = Path(args.tasks).resolve()
    base_run = None if args.no_screen else find_base_run(base_sha, manifest)
    exam_budget = 0.0 if args.no_screen else args.budget_usd
    check_budget((args.improver_budget_usd if default else 0.0) + exam_budget, "this round")

    n = next_round()
    round_dir, wt, branch = HOME / "rounds" / str(n), HOME / "wt" / str(n), f"harness-loop/{n}"
    round_dir.mkdir(parents=True)
    git("worktree", "add", "-q", "-b", branch, str(wt), base_sha)
    row = {"round": n, "improver": "default" if default else "stub", "tasks_manifest": args.tasks, "branch": branch,
           "pr": None, "idea": None, "files": [], "base_sha": base_sha, "cand_sha": None, "screen": None,
           "full": None, "outcome": None, "reason": None, "improver_cost_usd": None, "ts": None}

    def finish(outcome, reason="", keep_branch=False):
        cleanup(wt, branch, keep_branch)
        row.update(outcome=outcome, reason=reason, ts=now().isoformat())
        append_jsonl(HOME / "history.jsonl", row)
        print(f"round {n}: {outcome}{': ' + reason if reason else ''}")

    before = None
    try:
        msg = "\n".join([
            "Follow the improver skill. Everything you need is at these paths.",
            f"skill: {wt}/harness-loop/improver/SKILL.md", f"worktree: {wt}",
            f"history: {HOME / 'history.jsonl'}", f"runs: {HOME / 'runs'}", f"tries: {TRIES}",
            f"round_dir: {round_dir}", ""])
        (round_dir / "message.txt").write_text(msg)
        argv = improver_argv(args, msg, round_dir)
        if not default:
            improver_settings(round_dir)
        before, existing = snapshot(branch, base_run), existing_dirs()
        row["improver_cost_usd"] = "running"
        code, cost = run_improver(argv, wt, round_dir, improver_env(round_dir, default))
        changes = tampered(before, snapshot(branch, base_run))
        restore_state(before["state"])
        row["improver_cost_usd"] = None
        if default:
            row["improver_cost_usd"] = cost if cost is not None else args.improver_budget_usd
            book("improver", row["improver_cost_usd"], n, "" if cost is not None else "cost unknown: booked at cap")
        if changes:
            return finish("dropped", "tamper: " + "; ".join(changes))
        if code == "timeout":
            return finish("dropped", f"improver timed out after {IMPROVER_TIMEOUT_S}s")

        cand = git("rev-parse", f"refs/heads/{branch}").strip()
        row["cand_sha"] = cand
        prop_f = round_dir / "proposal.md"
        proposal = prop_f.read_text(errors="replace") if prop_f.is_file() else ""
        idea = re.search(r"^Idea:\s*(.+)$", proposal, re.M)
        row["idea"] = idea[1].strip() if idea else None
        checks, files = run_checks(base_sha, cand, proposal, private, existing)
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

        per_run = exam_budget if base_run else exam_budget / 2
        if base_run is None:
            base_run = run_exam(base_sha, manifest, "baseline", per_run, n)
            before["main screen run"] = hash_tree(base_run)
        cand_run = run_exam(cand, manifest, "candidate", per_run, n)
        changes = tampered(before, snapshot(branch, base_run), ignore_pycache=True, skip_state=("spend.jsonl",))
        if changes:
            restore_state(before["state"])
            return finish("dropped", "tamper during screen: " + "; ".join(changes))
        stats = compare(base_run, cand_run, "--screen-rule")
        verdict = stats["screen"]
        row["screen"] = {"base_run": rel_home(base_run), "cand_run": rel_home(cand_run),
                         "solved_base": int(stats["solvedA"]), "solved_cand": int(stats["solvedB"]),
                         "cost_base": float(stats["costA"]), "cost_cand": float(stats["costB"]),
                         "n": int(stats["n"]), "verdict": verdict}
        (round_dir / "screen.json").write_text(json.dumps({**row["screen"], "compare": stats}, indent=2) + "\n")
        line = score_line("screen", stats, base_sha, cand, tries_of(cand_run), f"screen={verdict}")
        print(f"checks: pass | screen: main {stats['solvedA']}/{stats['n']} ${float(stats['costA']):.2f}, "
              f"candidate {stats['solvedB']}/{stats['n']} ${float(stats['costB']):.2f} -> {verdict}")

        label = "harness-loop" if default else "harness-loop-test"
        body = "\n\n".join([
            proposal_for_body(proposal, existing), scores_block(line),
            f"Runs (under the loop home): `{rel_home(base_run)}` (main), `{rel_home(cand_run)}` (candidate)",
            f"Round {n}, improver: {row['improver']}. Opened by harness-loop/loop.py; scores are written by the "
            "script from compare.py, never by the improver. The loop never merges."])
        body_f = round_dir / "pr-body.md"
        body_f.write_text(body + "\n")
        title = f"harness-loop round {n}: {(row['idea'] or 'no idea line')[:70]}"
        git("-c", "core.hooksPath=/dev/null", "push", "-q", "--no-verify", "origin",
            f"refs/heads/{branch}:refs/heads/{branch}")
        gh = ["gh", "pr", "create", "--repo", GH_REPO, "--head", branch, "--base", "main",
              "--title", ("[screen fail] " if verdict != "pass" else "") + title,
              "--body", body + "\n", "--label", label] + (["--draft"] if verdict != "pass" else [])
        url = subprocess.run(gh, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
        row["pr"] = int(url.rstrip("/").rsplit("/", 1)[1])
        if verdict == "pass":
            write_queue(read_queue() + [{"round": n, "branch": branch, "base_sha": base_sha, "cand_sha": cand,
                                         "manifest": str(manifest), "pr": row["pr"]}])
        print(f"PR {url} (label {label}){', queued' if verdict == 'pass' else ', draft (screen fail)'}")
        return finish("queued" if verdict == "pass" else "screen-fail", keep_branch=True)
    except BaseException as e:  # every started round gets a history row, Ctrl-C included
        if before:  # an error the improver provoked must not keep what it wrote
            restore_state(before["state"])
        if row["improver_cost_usd"] == "running":  # interrupted mid-improver: its cost is unknown
            row["improver_cost_usd"] = args.improver_budget_usd if default else None
            if default:
                book("improver", args.improver_budget_usd, n, "interrupted: booked at cap")
        finish("error", f"{e.__class__.__name__}: {e}", keep_branch=row["cand_sha"] is not None)
        raise


def gh_json(*args):
    out = subprocess.run(["gh", *args, "--repo", GH_REPO], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def cmd_monthly(args):
    queue = [q for q in read_queue() if gh_json("pr", "view", str(q["pr"]), "--json", "state")["state"] == "OPEN"]
    write_queue(queue)  # a merged or closed PR is never re-examined
    if not queue:
        print("monthly: nothing queued")
        return
    groups = {}
    for q in queue:
        groups.setdefault(q["manifest"], []).append(q)
    check_budget(args.budget_usd, "the monthly exam")
    per_run = args.budget_usd / (2 * len(groups) + len(queue))
    git("fetch", "-q", "origin", "main")
    main_sha = git("rev-parse", "origin/main").strip()
    history = {r["round"]: r for r in read_jsonl(HOME / "history.jsonl")}
    for manifest, items in groups.items():
        a1 = run_exam(main_sha, manifest, "baseline", per_run, 0, screen=False, tries=2)
        a2 = run_exam(main_sha, manifest, "baseline", per_run, 0, screen=False, tries=2)
        for q in items:
            b = run_exam(q["cand_sha"], manifest, "candidate", per_run, q["round"], screen=False, tries=2)
            stats = compare(a1, b, "--margin-from", str(a2))
            cut = [rel_home(r) for r in (a1, a2, b) if json.loads((r / "run.json").read_text()).get("status") != "complete"]
            if cut:  # a budget-cut run is a few tasks, not the full exam: no verdict from it
                stats["verdict"] = "incomplete"
            full = {"main_run": rel_home(a1), "main_run_2": rel_home(a2), "cand_run": rel_home(b),
                    "main_sha": main_sha, "solved_base": int(stats["solvedA"]), "solved_cand": int(stats["solvedB"]),
                    "cost_base": float(stats["costA"]), "cost_cand": float(stats["costB"]), "n": int(stats["n"]),
                    "diff": float(stats["diff"]), "margin": float(stats["margin"]), "verdict": stats["verdict"]}
            row = history.get(q["round"], {})
            screen = row.get("screen")
            lines = []
            if screen:
                lines.append(score_line("screen", {"n": screen["n"], "solvedA": screen["solved_base"],
                                                   "solvedB": screen["solved_cand"], "costA": screen["cost_base"],
                                                   "costB": screen["cost_cand"]},
                                        q["base_sha"], q["cand_sha"], 1, f"screen={screen['verdict']}"))
            lines.append(score_line("full", stats, main_sha, q["cand_sha"], tries_of(b),
                                    f"diff={stats['diff']} margin={stats['margin']} verdict={stats['verdict']}"))
            # record and dequeue first: the exam is paid, so a failed PR edit must not make a rerun pay again
            update_history(q["round"], full=full)
            write_queue([x for x in read_queue() if x["round"] != q["round"]])
            body = gh_json("pr", "view", str(q["pr"]), "--json", "body")["body"]
            new = re.sub(re.escape(SCORES_OPEN) + r".*?" + re.escape(SCORES_CLOSE), lambda _: scores_block(*lines),
                         body, count=1, flags=re.S)
            if new == body:
                new = body.rstrip() + "\n\n" + scores_block(*lines)
            body_f = HOME / "rounds" / str(q["round"]) / "pr-body-full.md"  # kept to re-apply by hand on failure
            body_f.parent.mkdir(parents=True, exist_ok=True)
            body_f.write_text(new)
            subprocess.run(["gh", "pr", "edit", str(q["pr"]), "--repo", GH_REPO, "--body-file", str(body_f)],
                           check=True, capture_output=True, text=True)
            print(f"monthly: round {q['round']} PR #{q['pr']}: {lines[-1]}"
                  + (f" (cut short: {', '.join(cut)})" if cut else ""))


def cmd_sync(_args):
    rows = read_jsonl(HOME / "history.jsonl")
    closed = set()
    for r in rows:
        if r.get("pr") and r["outcome"] in ("queued", "screen-fail"):
            state = gh_json("pr", "view", str(r["pr"]), "--json", "state")["state"]
            if state in ("MERGED", "CLOSED"):
                r["outcome"] = state.lower()
                closed.add(r["round"])
                print(f"sync: round {r['round']} PR #{r['pr']} {r['outcome']}")
    write_atomic(HOME / "history.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
    write_queue([q for q in read_queue() if q["round"] not in closed])


def cmd_guard(args):
    reasons, _ = guard_commits(args.base or f"{args.commit}^", args.commit)
    for r in reasons:
        print(f"guard: {r}")
    if reasons:
        print("guard: rejected: weakens gates")
        sys.exit(1)
    print("guard: pass")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("round")
    r.add_argument("--tasks", required=True)
    r.add_argument("--improver-cmd")
    r.add_argument("--no-screen", action="store_true")
    r.add_argument("--budget-usd", type=float, default=DEFAULT_EXAM_BUDGET_USD,
                   help="most the round's exam runs may spend together")
    r.add_argument("--improver-budget-usd", type=float, default=IMPROVER_MAX_USD)
    m = sub.add_parser("monthly")
    m.add_argument("--budget-usd", type=float, default=DEFAULT_EXAM_BUDGET_USD)
    sub.add_parser("sync")
    g = sub.add_parser("guard", help="run the round's gate guard on one commit (exit 1 = rejected)")
    g.add_argument("commit")
    g.add_argument("--base", help="diff base (default: <commit>^)")
    args = ap.parse_args(argv)
    {"round": cmd_round, "monthly": cmd_monthly, "sync": cmd_sync, "guard": cmd_guard}[args.cmd](args)


if __name__ == "__main__":
    main()
