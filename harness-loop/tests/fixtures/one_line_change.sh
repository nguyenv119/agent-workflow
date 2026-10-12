#!/usr/bin/env bash
# Stub improver for loop.py: one added line, one commit, one proposal.md. No model call.
# loop.py runs it with cwd = the round's worktree and HARNESS_LOOP_ROUND_DIR set.
# STUB_TRY_PATHS: two or more try folders, space separated, written as runs/... or
#   ~/.claude/tries/agent-workflow/... (keep the literal ~).
# Optional: STUB_LINE replaces the added line; STUB_PROPOSAL_EXTRA is appended to proposal.md.
set -euo pipefail
: "${HARNESS_LOOP_ROUND_DIR:?set by loop.py}" "${STUB_TRY_PATHS:?two or more try-folder paths}"
f=.claude/skills/implementer/SKILL.md
printf '%s\n' "${STUB_LINE:-- Before committing, re-read the acceptance criteria and name the test that covers each one.}" >> "$f"
git add "$f"
git -c user.name=harness-loop-stub -c user.email=stub@example.invalid -c commit.gpgsign=false \
  commit -q -m "implementer: re-read the acceptance criteria before committing (loop stub)"
{
  echo "Idea: The implementer re-reads the acceptance criteria before committing, so a missed criterion is caught before review."
  echo "Files: $f"
  echo "Evidence:"
  for p in $STUB_TRY_PATHS; do echo "  $p - stub citation"; done
  echo "Expected effect: none; this is the loop's test stub."
  echo "Risk: none."
  if [ -n "${STUB_PROPOSAL_EXTRA:-}" ]; then printf '%s\n' "$STUB_PROPOSAL_EXTRA"; fi
} > "$HARNESS_LOOP_ROUND_DIR/proposal.md"
