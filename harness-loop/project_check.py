#!/usr/bin/env python3
"""Does a change to this public repo name or point to one specific project? One Sonnet call decides.

    python3 harness-loop/project_check.py <base> <head> [body-file]

Reads, from the git repo in the current directory, the lines added in <base>...<head>, the
commit messages of <base>..<head> and the PR body (body-file, optional), and asks Sonnet
(`claude -p --model sonnet`, Bedrock only, no tools) whether any line names or points to a
specific project, company, product, person, private repo, domain, table, internal tool or
infra, or carries a rule that only makes sense for one project. Prints one JSON line
{clean, hits: [{line, why}], cost_usd, error} and exits 0 when clean, 1 when blocked.

Fail closed: a git error, an oversized input, a failed call or a reply that is not the asked
JSON all block (exit 1, with `error` set). loop.py's run_checks runs it on every round, next to
the word-list leak scan, which stays: that one is free and catches exact names; this one
catches a project-only rule that uses none of them.

Env: PROJECT_CHECK_CLAUDE replaces the claude command (tests use a fake that replays recorded
replies); with the default command the call needs CLAUDE_CODE_USE_BEDROCK=1 and Bedrock keys.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True  # loop.py hashes harness-loop/: importing it must not leave a __pycache__
from loop import BEDROCK_ENV, SCRUB_ENV  # noqa: E402  the improver's env rule: Bedrock keys, nothing else

DEFAULT_CLAUDE = ["claude", "-p", "--model", "sonnet", "--tools", "", "--output-format", "json",
                  "--no-session-persistence", "--max-budget-usd", "1.00"]
TIMEOUT_S = 300
MAX_CHARS = 200_000  # a round's diff is a few dozen lines; anything near this is not one to wave through

PROMPT = """\
You review text that is about to be published in a PUBLIC repository. The repository holds a
general-purpose agent workflow harness (skills, hooks and scripts that steer coding agents on
ANY repository). Nothing in it may be tied to one particular project.

Below are numbered lines: lines added by a change (with their file), the commit message and the
pull request body. For each line, decide whether it names or points to a specific project,
company, product, person, private repository, domain or URL, database table, internal tool,
service or piece of infrastructure, or carries a rule, convention or fact that only makes sense
for one particular project or codebase (for example a rule about one app's own data, one team's
deploy, one schema, one customer).

Do NOT flag: general programming terms; widely used public tools, languages and platforms named
as general examples (git, GitHub, Python, a test runner); this harness's own vocabulary
(skills, coordinator, implementer, reviewers, beads/bd, /work, worktrees, try folders, tasks,
exam, harness-loop); generic placeholders such as <repo>, <bead>, example.com or "one task".
A rule that would help a developer on many unfamiliar repositories is generic.

Reply with ONLY a JSON object, no prose and no code fence:
{"clean": true, "hits": []}
or
{"clean": false, "hits": [{"line": "L<number>", "why": "<one short sentence>"}]}

LINES:
"""


def git(*args):
    p = subprocess.run(["git", *args], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p.stdout.decode("utf-8", "replace")


def added_lines(diff):
    """[(path, text)] for every added line of a unified diff."""
    out, path = [], None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+") and path is not None:
            out.append((path, line[1:]))
        elif line.startswith("diff --git "):
            path = None
    return out


def numbered(added, message, body):
    rows = [f"[{p}] {t}" for p, t in added]
    rows += [f"[commit message] {t}" for t in message.splitlines() if t.strip()]
    rows += [f"[PR body] {t}" for t in body.splitlines() if t.strip()]
    return "\n".join(f"L{i} {r}" for i, r in enumerate(rows, 1))


def parse_reply(text):
    """The verdict dict from the model's text, or raise ValueError. A hit always blocks."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("reply has no JSON object")
    v = json.loads(m[0])
    hits = v.get("hits") if isinstance(v, dict) else None
    if not isinstance(v, dict) or not isinstance(v.get("clean"), bool) or not isinstance(hits, list):
        raise ValueError("reply is not {clean: bool, hits: [...]}")
    if not all(isinstance(h, dict) and {"line", "why"} <= h.keys() for h in hits):
        raise ValueError("a hit lacks line or why")
    return {"clean": v["clean"] and not hits, "hits": [{"line": str(h["line"]), "why": str(h["why"])} for h in hits]}


def call_model(prompt):
    """(reply text, cost_usd|None). Raises on any failure of the call itself."""
    fake = os.environ.get("PROJECT_CHECK_CLAUDE")
    argv = shlex.split(fake) if fake else DEFAULT_CLAUDE
    env = {k: v for k, v in os.environ.items() if k in BEDROCK_ENV or not SCRUB_ENV.match(k)}
    if not fake and (env.get("CLAUDE_CODE_USE_BEDROCK") != "1"
                     or not (env.get("AWS_ACCESS_KEY_ID") or env.get("AWS_BEARER_TOKEN_BEDROCK"))):
        raise RuntimeError("the project check runs on Bedrock only: set CLAUDE_CODE_USE_BEDROCK=1 and Bedrock keys")
    with tempfile.TemporaryDirectory(prefix="project-check-") as tmp:
        env["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "config")  # no user settings, memory, hooks or plugins
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=tmp, env=env, timeout=TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
    envelope = json.loads(p.stdout)
    if envelope.get("is_error") or not isinstance(envelope.get("result"), str):
        raise RuntimeError(f"claude returned an error: {str(envelope.get('result'))[:200]}")
    cost = envelope.get("total_cost_usd")
    return envelope["result"], float(cost) if cost is not None else None


def check(base, head, body=""):
    """{clean, hits, cost_usd, error}; clean is False on any error (fail closed)."""
    cost = None
    try:
        diff = git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--text", f"{base}...{head}")
        message = git("log", "--format=%B", f"{base}..{head}")
        lines = numbered(added_lines(diff), message, body)
        if len(lines) > MAX_CHARS:
            raise ValueError(f"input is {len(lines)} chars, over the {MAX_CHARS} cap")
        text, cost = call_model(PROMPT + lines + "\n")
        return {**parse_reply(text), "cost_usd": cost, "error": None}
    except Exception as e:  # fail closed: anything unexpected blocks
        return {"clean": False, "hits": [], "cost_usd": cost, "error": f"{e.__class__.__name__}: {e}"}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    body = ""
    if len(args) == 3:
        with open(args[2], encoding="utf-8", errors="replace") as fh:
            body = fh.read()
    verdict = check(args[0], args[1], body)
    print(json.dumps(verdict))
    return 0 if verdict["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
