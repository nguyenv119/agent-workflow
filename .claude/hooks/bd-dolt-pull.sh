#!/usr/bin/env bash
# PreToolUse hook: pull from Dolt remote before bd commands.
#
# Claude Code fires this hook before every Bash tool call. The hook:
#   1. Exits immediately for non-bd commands (no latency added).
#   2. Exits 0 if no dolt remote named "origin" is configured (local-only safe).
#   3. Runs `dolt pull origin main` before the bd command executes.
#   4. Always exits 0 — network failures must never block agent work.
#
# Note: dolt does NOT support a -C flag. All dolt commands use (cd dir && dolt …).

set -euo pipefail

# Read the tool input JSON from stdin.
INPUT="$(cat)"

# Extract the bash command string.
COMMAND="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""')"

# Only act on commands that start with "bd" (literal word boundary).
if ! printf '%s' "$COMMAND" | grep -qE '^bd( |$)'; then
  exit 0
fi

# Locate the Dolt directory. bd where returns the .beads directory.
DOLT_DIR="$(bd where 2>/dev/null)/dolt"

if [[ ! -d "$DOLT_DIR" ]]; then
  exit 0
fi

# Check whether a remote named "origin" is configured.
if ! (cd "$DOLT_DIR" && dolt remote -v 2>/dev/null | grep -q origin); then
  exit 0
fi

# Pull from remote. Suppress output; errors are non-fatal.
(cd "$DOLT_DIR" && dolt pull origin main 2>/dev/null) || true

exit 0
