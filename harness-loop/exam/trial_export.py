#!/usr/bin/env python3
"""Stdlib helpers for WorkflowAgent and the exam runner (no Harbor import).

    trial_export.py finalize <trial_dir>

sandbox_env()   the exact env a WorkflowAgent run passes into the sandbox
stream_result() total_cost_usd + subtype from claude's stream-json result line
finalize()      after Harbor's verifier ran, write <trial>/agent/exam/gates.txt
                so <trial>/agent/exam is a `save_try.py --from-files` dir
"""
import json
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


def stream_result(path):
    """{'total_cost_usd', 'subtype'} from the last `result` line, Nones if absent.

    total_cost_usd includes subagents; the result's `usage` does not.
    """
    out = {"total_cost_usd": None, "subtype": None}
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            out = {"total_cost_usd": event.get("total_cost_usd"), "subtype": event.get("subtype")}
    return out


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
