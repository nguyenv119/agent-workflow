#!/usr/bin/env bash
# PreToolUse hook: enforce that reviewer subagents are spawned with SKILL.md
# and injected checklist content (verified by content signatures).
#
# Claude Code fires this hook before every Agent tool call. The hook:
#   1. Exits immediately for non-Agent calls (matcher handles this, but guard anyway).
#   2. Checks if the ROLE: line identifies a reviewer spawn.
#   3. If it is a reviewer spawn, verifies the prompt includes:
#      - A SKILL.md reference (so the reviewer reads its canonical process)
#      - Content signatures matching the reviewer type (verifies the coordinator
#        actually injected checklist content, not just mentioned file paths)
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

# Detect reviewer spawns by checking only the ROLE: line.
# This avoids false positives where the word "Reviewer" appears elsewhere in
# the prompt text (e.g., in standards content injected by the coordinator).
ROLE_LINE="$(printf '%s' "$PROMPT" | grep -m1 '^ROLE:' || true)"
IS_REVIEWER=false
if printf '%s' "$ROLE_LINE" | grep -qiE '(Correctness Reviewer|Test Quality Reviewer|Architecture Reviewer)'; then
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

# Check 2: Content signature verification — verify the coordinator injected actual
# checklist content, not just mentioned file paths.
#
# Strategy: read the standards files from disk to build the set of expected section
# headers, then count how many appear in the prompt. Require a threshold match.
# This is resilient to minor wording changes while still catching absent content.

# Determine reviewer type from the ROLE: line.
REVIEWER_TYPE=""
if printf '%s' "$ROLE_LINE" | grep -qi "Correctness Reviewer"; then
  REVIEWER_TYPE="correctness"
elif printf '%s' "$ROLE_LINE" | grep -qi "Test Quality Reviewer"; then
  REVIEWER_TYPE="tests"
elif printf '%s' "$ROLE_LINE" | grep -qi "Architecture Reviewer"; then
  REVIEWER_TYPE="architecture"
fi

# Define expected signatures per reviewer type.
# These are key section headers from the standards files. The coordinator must
# inject content from these sections; their presence signals actual injection.
case "$REVIEWER_TYPE" in
  correctness)
    # All of correctness-patterns.md + quality.md §F + §G
    EXPECTED_SIGS=(
      "Race/Select"
      "Unbounded"
      "Multi-Step"
      "Retry Scope"
      "Type Narrowing"
      "Derived Data"
      "Dual Code Paths"
      "Refactor Cleanup"
    )
    THRESHOLD=4
    ;;
  tests)
    # quality.md §A + §B + §C + §D + §E + §G
    EXPECTED_SIGS=(
      "GIVEN"
      "WHEN"
      "THEN"
      "Mock Discipline"
      "Docstring"
      "Test Naming"
      "Core Dependency"
    )
    THRESHOLD=4
    ;;
  architecture)
    # correctness-patterns.md Retry Scope + Dual Code Paths + Derived Data + quality.md §F + §G
    EXPECTED_SIGS=(
      "Retry Scope"
      "Dual Code Paths"
      "Derived Data"
      "Refactor Cleanup"
      "Review Discipline"
    )
    THRESHOLD=3
    ;;
  *)
    # Unknown reviewer type — require at least one known standards signature
    EXPECTED_SIGS=(
      "Race/Select"
      "Unbounded"
      "GIVEN"
      "WHEN"
      "Mock Discipline"
      "Refactor Cleanup"
      "Review Discipline"
    )
    THRESHOLD=2
    ;;
esac

FOUND=0
for sig in "${EXPECTED_SIGS[@]}"; do
  if printf '%s' "$PROMPT" | grep -qi "$sig"; then
    ((FOUND++)) || true
  fi
done

TOTAL="${#EXPECTED_SIGS[@]}"
if [[ $FOUND -lt $THRESHOLD ]]; then
  SIG_MSG="checklist content ($FOUND/$TOTAL signatures found; need $THRESHOLD)"
  if [[ -n "$MISSING" ]]; then
    MISSING="$MISSING; $SIG_MSG"
  else
    MISSING="$SIG_MSG"
  fi
fi

if [[ -n "$MISSING" ]]; then
  cat <<EOF
BLOCKED: Reviewer subagent spawned without loading canonical standards.

Missing: $MISSING

Reviewer subagents MUST be spawned with:
1. SKILL: Read and follow .claude/skills/reviewer-<type>/SKILL.md
2. Injected checklist content — the coordinator must read the standards files
   and inject relevant sections directly into the reviewer prompt.

The hook verifies content signatures (section headers) are present in the prompt,
not just file path references. See .claude/skills/coordinator/SKILL.md § 4a
for the correct spawn templates.
EOF
  exit 2
fi

exit 0
