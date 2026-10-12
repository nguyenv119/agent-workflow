#!/usr/bin/env python3
"""Stdlib helpers for WorkflowAgent and the exam runner (no Harbor import).

    trial_export.py finalize <trial_dir>

sandbox_env()   the exact env a WorkflowAgent run passes into the sandbox
stream_result() cost, subtype, session id and the no-PR line from claude's stream-json
write_result()  after the run, exam/result.json + transcript.jsonl (bead .7's input)
finalize()      after Harbor's verifier ran, write <trial>/agent/exam/gates.txt
                so <trial>/agent/exam is a `save_try.py --from-files` dir
"""
import json
import shutil
import sys
from pathlib import Path

BEDROCK_MODELS = {
    "ANTHROPIC_MODEL": "global.anthropic.claude-opus-5-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "global.anthropic.claude-opus-5-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "global.anthropic.claude-sonnet-5-5",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "global.anthropic.claude-haiku-5-5",
    "ANTHROPIC_SMALL_FAST_MODEL": "global.anthropic.claude-haiku-5-5",
}
API_MODEL = "claude-opus-5-5"


def _need(host, key):
    value = host.get(key)
    if not value:
        raise RuntimeError(f"{key} is not set on the host; it is required for this model route")
    return value


def sandbox_env(host, model_access, tries_dir, model=None):
    """Env for `claude` in the sandbox, built from an allowlist of host vars.

    bedrock (default, D41): CLAUDE_CODE_USE_BEDROCK, AWS_REGION and the
    Bedrock-only AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, plus the global.*
    model ids. api: ANTHROPIC_API_KEY only. Nothing else from the host is
    copied (no AWS_ADMIN_*, session token, profile or OAuth token).
    """
    if model_access == "bedrock":
        env = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": host.get("AWS_REGION") or "us-east-1",
            "AWS_ACCESS_KEY_ID": _need(host, "AWS_ACCESS_KEY_ID"),
            "AWS_SECRET_ACCESS_KEY": _need(host, "AWS_SECRET_ACCESS_KEY"),
            **BEDROCK_MODELS,
        }
    elif model_access == "api":
        env = {"ANTHROPIC_API_KEY": _need(host, "ANTHROPIC_API_KEY"), "ANTHROPIC_MODEL": API_MODEL}
    else:
        raise ValueError(f"model_access must be 'bedrock' or 'api', got {model_access!r}")
    if model:
        env["ANTHROPIC_MODEL"] = model
    env.update({
        # Background subagents keep `claude -p` open; without this it stops
        # waiting for them after 10 minutes.
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
        "IS_SANDBOX": "1",
        # The coordinator's own try folders land in the trial logs.
        "HARNESS_TRIES_DIR": str(tries_dir),
    })
    return env


NO_PR_LINE = "EXAM_MODE: no PR"
STATUS_BY_SUBTYPE = {"success": "ok", "error_max_budget_usd": "budget_cut"}


def stream_result(path):
    """What the bead .7 export needs from claude's stream-json (Nones if absent).

    total_cost_usd and subtype come from the last `result` line;
    total_cost_usd includes subagents, the result's `usage` does not.
    session_id is the parent session's, from the first `init` line.
    exam_no_pr_line: the parent's final message has `EXAM_MODE: no PR` on a
    line of its own (only the final message counts: the skill text itself,
    read during the run, also contains that line).
    """
    out = {"total_cost_usd": None, "subtype": None, "session_id": None, "exam_no_pr_line": False}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init" and not out["session_id"]:
            out["session_id"] = event.get("session_id")
        if event.get("type") == "result":
            text = event.get("result") if isinstance(event.get("result"), str) else ""
            out.update(total_cost_usd=event.get("total_cost_usd"), subtype=event.get("subtype"),
                       exam_no_pr_line=NO_PR_LINE in (l.strip() for l in text.splitlines()))
    return out


def _read(path):
    return path.read_text().strip() if path.is_file() else None


def write_result(agent_logs, bead, harness_commit, harness_version=None):
    """Write <agent_logs>/exam/result.json (+ transcript.jsonl) after the run.

    agent_logs is Harbor's downloaded agent dir: claude-code.txt (stream-json),
    sessions/projects/<slug>/<session_id>.jsonl (transcripts) and exam/ (what
    setup_task.sh and finish_task.sh wrote). status: ok on a normal finish,
    budget_cut when --max-budget-usd stopped it, else error. harness_version is
    the .claude/HARNESS_VERSION this trial installed (run_exam checks it).
    """
    agent_logs = Path(agent_logs)
    exam = agent_logs / "exam"
    exam.mkdir(parents=True, exist_ok=True)
    stream = stream_result(agent_logs / "claude-code.txt")
    transcript = None
    if stream["session_id"]:
        hits = sorted((agent_logs / "sessions" / "projects").glob(f"*/{stream['session_id']}.jsonl"))
        if hits:
            shutil.copyfile(hits[0], exam / "transcript.jsonl")
            transcript = "transcript.jsonl"
    result = {
        "bead": bead,
        "branch": _read(exam / "branch.txt") or None,
        "origin_url": _read(exam / "origin_url.txt"),
        "harness_commit": harness_commit,
        "harness_version": harness_version,
        "status": STATUS_BY_SUBTYPE.get(stream["subtype"], "error"),
        "subtype": stream["subtype"],
        "total_cost_usd": stream["total_cost_usd"],
        "exam_no_pr_line": stream["exam_no_pr_line"],
        "session_id": stream["session_id"],
        "transcript": transcript,
    }
    (exam / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def finalize(trial_dir):
    """Write <trial>/agent/exam/gates.txt from the verifier's stdout, stderr and reward."""
    trial = Path(trial_dir)
    verifier = trial / "verifier"
    parts = []
    for name in ("test-stdout.txt", "test-stderr.txt"):
        f = verifier / name
        if f.is_file() and f.stat().st_size:
            parts.append(f"=== {name}\n{f.read_text(errors='replace')}")
    for name in ("reward.txt", "reward.json"):
        f = verifier / name
        if f.is_file():
            parts.append(f"reward: {f.read_text(errors='replace').strip()}\n")
    if not parts:
        parts.append(f"no verifier output in {verifier}\n")
    exam = trial / "agent" / "exam"
    exam.mkdir(parents=True, exist_ok=True)
    (exam / "gates.txt").write_text("".join(parts))
    return exam


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "finalize":
        sys.exit(__doc__)
    print(finalize(sys.argv[2]))
