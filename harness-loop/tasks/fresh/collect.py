"""Offline helpers for build.sh: cost gate, candidate list, manifest from built tasks.

  collect.py plan CANDIDATES --per-task-usd X [--limit N] [--confirm-budget USD]
      Print the candidate count and estimated cost. Exit 0 only if --confirm-budget
      covers the estimate; exit 3 otherwise (nothing is spent).
  collect.py list CANDIDATES [--limit N]
      One "id repo pr" line per candidate, for build.sh's loop.
  collect.py cost LOG --seconds S --usd-per-hour R [--daytona-usd-per-hour D]
      One task's cost as "<usd> measured" (sum of total_cost_usd in the log, plus the
      log's daytona_seconds x D) or, when the log has no session cost, "<usd> elapsed"
      (S / 3600 x R). build.sh adds it to a running total.
  collect.py manifest CANDIDATES BUILD_DIR OUT.json
      Rows for every candidate listed in BUILD_DIR/passed.txt, validated by manifest.py
      (including its hidden-test leak check), written to OUT.json. Each passed task is
      copied from the git-ignored BUILD_DIR to <OUT dir>/<OUT stem>/<id>/ (committed,
      so the exam can run it from a fresh clone), with SWE-gen's network_mode
      "no-network" opened to "public" (see open_network).
"""
import argparse
import json
import re
import shlex
import shutil
import sys
import tomllib
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import manifest  # noqa: E402
from pick_prs import is_test_path  # noqa: E402

PLAN_REFUSED = 3
# A test runner as the first word of a command (path prefix like /opt/venv/bin/ allowed),
# optionally behind uv/poetry/npx/pnpm/yarn/npm/bun or `python -m`.
RUNNER = re.compile(
    r"^(?:\S+/)?(?:(?:uv|poetry|pdm|hatch)\s+run\s+|npx\s+|pnpm\s+(?:exec\s+|run\s+)?|"
    r"yarn\s+(?:run\s+)?|npm\s+(?:run\s+|exec\s+)?|bun\s+(?:run\s+)?|python3?\s+-m\s+)?"
    r"(?:\S+/)?(pytest|jest|vitest|mocha|ava|tap|unittest|test)\b")


def task_id(repo, pr):
    return f"{repo.replace('/', '__').lower()}-{pr}"


def load_candidates(path):
    return json.loads(Path(path).read_text())["candidates"]


# The Claude session's result reports its cost as total_cost_usd (JSON or repr form).
COST = re.compile(r"""total_cost_usd['"]?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)""")


SANDBOX = re.compile(r"daytona_seconds:\s*([0-9]+)")


def cost(log, seconds, usd_per_hour, daytona_usd_per_hour=0.0):
    """(usd, how) for one task. Sums every reported total_cost_usd: overcounting a
    repeated report only stops the loop early, never late."""
    found = [float(x) for x in COST.findall(log)]
    if found:
        sandbox = sum(int(x) for x in SANDBOX.findall(log)) / 3600 * daytona_usd_per_hour
        return round(sum(found) + sandbox, 2), "measured"
    return round(seconds / 3600 * usd_per_hour, 2), "elapsed"


def plan(cands, per_task_usd, budget=None):
    """Return (message, ok). ok only when a budget is given and covers the estimate."""
    n = len(cands)
    est = n * per_task_usd
    lang = Counter(c["language"] for c in cands)
    kind = Counter(c["kind"] for c in cands)
    msg = (f"{n} candidates ({lang['python']} python, {lang['typescript']} typescript; "
           f"{kind['bugfix']} bugfix, {kind['feature']} feature)\n"
           f"estimated cost: ${est:,.2f} ({n} x ${per_task_usd:,.2f} per task, an estimate, "
           f"not a cap; build.sh stops starting tasks once measured spend reaches the budget; "
           f"instruction writer on Gemini credits, not counted)")
    if budget is None:
        return msg + "\nrefusing to spend: pass --confirm-budget <usd> once the estimate is approved", False
    if budget < est:
        return msg + f"\nrefusing to spend: --confirm-budget ${budget:,.2f} is below the estimate", False
    return msg + f"\nbudget ${budget:,.2f} confirmed", True


def _segments(line):
    """Shell commands on one line, split at && || ; | & and cut at redirections."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:  # unbalanced quotes: not a line we can read
        return []
    segs, seg, skip = [], [], False
    for tok in toks:
        if tok in ("&&", "||", ";", "|", "&"):
            segs.append(seg)
            seg, skip = [], False
        elif set(tok) <= set("<>&()"):  # redirection: drop the rest of this command
            if seg and seg[-1].isdigit():  # the 2 of 2>&1
                seg.pop()
            skip = True
        elif not skip:
            seg.append(tok)
    segs.append(seg)
    return [s for s in segs if s]


def _hidden(tok, hidden):
    tok = tok.split("=")[-1].split("::")[0]
    tok = tok[2:] if tok.startswith("./") else tok
    test_file = is_test_path(tok) and "." in Path(tok).name  # a file, not a tests/ folder
    return ("$" in tok or tok == "/tests" or tok.startswith("/tests/") or test_file
            or any(manifest._names(tok, h) for h in hidden))


def test_command(test_sh, hidden):
    """The visible test runner from test.sh, with hidden test paths removed.

    Takes the first command whose first word is a test runner and drops any argument
    naming a hidden test file, the /tests mount, a test file path, or a $VARIABLE (which
    in SWE-gen's test.sh expands to the copied hidden tests). None if no runner is found.
    """
    for raw in test_sh.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for seg in _segments(line):
            words = [w for w in seg if not re.fullmatch(r"[A-Za-z_]\w*=\S*", w)]  # env prefixes
            if not words or not RUNNER.match(" ".join(words)):
                continue
            kept = [w for w in words if not _hidden(w, hidden)]
            return shlex.join(kept)
    return None


def workdir(dockerfile):
    """The last WORKDIR in a Dockerfile, or None."""
    dirs = re.findall(r"^\s*WORKDIR\s+(\S+)", dockerfile, re.M | re.I)
    return dirs[-1] if dirs else None


def find_task(build_dir):
    """The Harbor task folder (the one holding task.toml) under one candidate's output."""
    found = sorted(p.parent for p in Path(build_dir).rglob("task.toml"))
    if len(found) != 1:
        raise ValueError(f"{build_dir}: expected one task.toml, found {len(found)}")
    return found[0]


NO_NETWORK = re.compile(r'^(\s*network_mode\s*=\s*)"no-network"(\s*)$', re.M)
NETWORK_NOTE = ('# SWE-gen wrote network_mode = "no-network". Opened to "public" so the exam\'s agent\n'
                '# can reach its model API, like the other exam sets (harness-loop/tasks/README.md).\n')


def open_network(toml_text):
    """task.toml text with SWE-gen's no-network environment opened to public.

    SWE-gen sets [environment] network_mode = "no-network", which covers the agent
    phase too, and neither Daytona nor Harbor's default docker setup can switch the
    network per phase. The exam's agent needs its model API, so the task runs on
    public like every other exam set; SWE-gen already proved test.sh passes offline.
    Raises ValueError if any phase still ends up without network.
    """
    text = NO_NETWORK.sub(lambda m: f'{m.group(1)}"public"{m.group(2)}', toml_text)
    if text != toml_text and NETWORK_NOTE not in text:
        text = NETWORK_NOTE + text
    cfg = tomllib.loads(text)
    for section in ("environment", "agent", "verifier"):
        if cfg.get(section, {}).get("network_mode") == "no-network":
            raise ValueError(f"[{section}] network_mode is still no-network")
    return text


def publish(tdir, dest):
    """Copy one built task folder to dest (replaced if present), network opened."""
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(tdir, dest, symlinks=True)  # never follow links in untrusted output
    toml = dest / "task.toml"
    toml.write_text(open_network(toml.read_text()))
    return dest


def rows(cands, build_dir, manifest_dir, publish_dir=None):
    """Manifest rows for candidates in BUILD_DIR/passed.txt (SWE-gen nop=0, oracle=1).

    With publish_dir, each task is first copied to publish_dir/<id> and the row points there.
    """
    passed_file = Path(build_dir) / "passed.txt"
    passed = set(passed_file.read_text().split()) if passed_file.is_file() else set()
    out = []
    for c in cands:
        tid = task_id(c["repo"], c["pr"])
        if tid not in passed:
            continue
        tdir = find_task(Path(build_dir) / tid)
        if publish_dir is not None:
            tdir = publish(tdir, Path(publish_dir) / tid)
        dockerfile = next(iter(sorted(tdir.rglob("Dockerfile"))), None)
        sh = tdir / "tests" / "test.sh"
        wd = workdir(dockerfile.read_text()) if dockerfile else None
        if wd is None:
            raise ValueError(f"{tid}: no WORKDIR in the task's Dockerfile")
        hidden = (manifest.hidden_paths(tdir) or set()) | set(c["test_files"])
        out.append({
            "id": tid, "harbor_path": Path(tdir).resolve().relative_to(Path(manifest_dir).resolve()).as_posix(),
            "repo": c["repo"], "pr": c["pr"], "merged_at": c["merged_at"], "language": c["language"],
            "kind": c["kind"], "workdir": wd,
            "test_command": test_command(sh.read_text(), hidden) if sh.is_file() else None,
            "title": c["title"], "screen": False,
        })
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("candidates")
    p.add_argument("--per-task-usd", type=float, required=True)
    p.add_argument("--confirm-budget", type=float)
    p.add_argument("--limit", type=int)
    li = sub.add_parser("list")
    li.add_argument("candidates")
    li.add_argument("--limit", type=int)
    co = sub.add_parser("cost")
    co.add_argument("log")
    co.add_argument("--seconds", type=float, required=True)
    co.add_argument("--usd-per-hour", type=float, required=True)
    co.add_argument("--daytona-usd-per-hour", type=float, default=0.0)
    m = sub.add_parser("manifest")
    m.add_argument("candidates")
    m.add_argument("build_dir")
    m.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "cost":
        log = Path(a.log).read_text(errors="replace") if Path(a.log).is_file() else ""
        print(*cost(log, a.seconds, a.usd_per_hour, a.daytona_usd_per_hour))
        return 0
    cands = load_candidates(a.candidates)
    if getattr(a, "limit", None) is not None:
        if a.limit < 1:
            ap.error("--limit must be at least 1")
        cands = cands[:a.limit]
    if a.cmd == "plan":
        msg, ok = plan(cands, a.per_task_usd, a.confirm_budget)
        print(msg)
        return 0 if ok else PLAN_REFUSED
    if a.cmd == "list":
        for c in cands:
            print(task_id(c["repo"], c["pr"]), c["repo"], c["pr"])
        return 0
    out = Path(a.out)
    month = re.search(r"\d{4}-\d{2}", out.name)
    doc = {"set": out.stem, "source": "swegen" + (f"-{month.group()}" if month else ""),
           "tasks": rows(cands, a.build_dir, out.parent, publish_dir=out.parent / out.stem)}
    tmp = out.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        print(manifest.summary(manifest.load(tmp)))
    except ValueError:
        tmp.unlink()
        raise
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
