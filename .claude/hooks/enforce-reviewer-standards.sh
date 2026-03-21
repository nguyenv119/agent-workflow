#!/usr/bin/env bash
# PreToolUse hook: enforce that reviewer subagents are spawned with SKILL.md
# and REQUIRED STANDARDS references.
#
# Claude Code fires this hook before every Agent tool call. The hook:
#   1. Exits immediately for non-Agent calls (matcher handles this, but guard anyway).
#   2. Checks if the prompt looks like a reviewer spawn (mentions "Reviewer" role).
#   3. If it is a reviewer spawn, verifies the prompt includes:
#      - A SKILL.md reference (so the reviewer reads its canonical process)
#      - A REQUIRED STANDARDS reference (so the reviewer reads shared standards)
#   4. Exits non-zero with a message if either is missing — blocking the call.
#   5. Exits 0 for non-reviewer Agent calls (no interference).

set -euo pipefail

INPUT="$(cat)"

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // ""')"

# Guard: only inspect Agent tool calls.
if [[ "$TOOL_NAME" != "Agent" ]]; then
  exit 0
fi

PROMPT="$(printf '%s' "$INPUT" | jq -r '.tool_input.prompt // ""')"

# Detect reviewer spawns. The coordinator templates use "ROLE: ... Reviewer".
# Also catch variations like "you are the correctness reviewer" or similar.
IS_REVIEWER=false
if printf '%s' "$PROMPT" | grep -qiE '(ROLE:.*Reviewer|Correctness Reviewer|Test Quality Reviewer|Architecture Reviewer|reviewer-correctness|reviewer-tests|reviewer-architecture)'; then
  IS_REVIEWER=true
fi

# Non-reviewer Agent calls pass through.
if [[ "$IS_REVIEWER" != "true" ]]; then
  exit 0
fi

# --- Reviewer spawn detected. Enforce standards loading. ---

MISSING=""

# Check 1: SKILL.md reference — match the path itself, regardless of prefix wording
if ! printf '%s' "$PROMPT" | grep -qE '\.claude/skills/reviewer-[a-z]+/SKILL\.md'; then
  MISSING="SKILL.md reference (e.g., 'SKILL: Read and follow .claude/skills/reviewer-correctness/SKILL.md')"
fi

# Check 2: REQUIRED STANDARDS reference
if ! printf '%s' "$PROMPT" | grep -qiE 'REQUIRED STANDARDS|standards/quality\.md|standards/correctness-patterns\.md'; then
  if [[ -n "$MISSING" ]]; then
    MISSING="$MISSING; and REQUIRED STANDARDS reference"
  else
    MISSING="REQUIRED STANDARDS reference (e.g., listing .claude/skills/standards/quality.md)"
  fi
fi

if [[ -n "$MISSING" ]]; then
  cat <<EOF
BLOCKED: Reviewer subagent spawned without loading canonical standards.

Missing: $MISSING

Reviewer subagents MUST be spawned with:
1. SKILL: Read and follow .claude/skills/reviewer-<type>/SKILL.md
2. REQUIRED STANDARDS listing the standards files to read

This ensures reviewers follow the project's canonical checklists instead of
ad-hoc inline instructions. See .claude/skills/coordinator/SKILL.md § 4a
for the correct spawn templates.
EOF
  exit 2
fi

exit 0
