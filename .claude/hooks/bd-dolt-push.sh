#!/usr/bin/env bash
# PostToolUse hook: push to Dolt remote after bd write commands.
#
# Claude Code fires this hook after every Bash tool call. The hook:
#   1. Exits immediately for non-bd commands (no latency added).
#   2. Uses an inverted allowlist: read-only bd subcommands are listed; anything
#      else is treated as a write and triggers a push. This is more future-proof
#      than listing every write command.
#   3. Exits 0 if no dolt remote named "origin" is configured (local-only safe).
#   4. Dirty-checks before committing: if dolt auto-commit is on, bd already
#      committed and the tree is clean — skip add+commit to avoid empty-commit errors.
#   5. Runs `dolt push origin main` after bd write commands.
#   6. Always exits 0 — network failures must never block agent work.
#
# Note: dolt does NOT support a -C flag. All dolt commands use (cd dir && dolt …).

set -euo pipefail

# Read-only bd subcommands that never modify the database.
# Everything NOT in this list is treated as a potential write.
READONLY_COMMANDS=(
  show
  list
  ready
  search
  query
  count
  diff
  history
  status
  types
  blocked
  stale
  find-duplicates
  lint
  where
  version
  help
  doctor
  prime
  recall
  preflight
  children
  orphans
)

# Read the tool input JSON from stdin.
INPUT="$(cat)"

# Extract the bash command string.
COMMAND="$(printf '%s' "$INPUT" | jq -r '.tool_input.command // ""')"

# Only act on commands that start with "bd" (literal word boundary).
if ! printf '%s' "$COMMAND" | grep -qE '^bd( |$)'; then
  exit 0
fi

# Extract the bd subcommand (the first token after "bd").
SUBCOMMAND="$(printf '%s' "$COMMAND" | awk '{print $2}')"

# If the subcommand is in the read-only allowlist, no push needed.
for readonly_cmd in "${READONLY_COMMANDS[@]}"; do
  if [[ "$SUBCOMMAND" == "$readonly_cmd" ]]; then
    exit 0
  fi
done

# Locate the Dolt directory. bd where returns the .beads directory.
DOLT_DIR="$(bd where 2>/dev/null)/dolt"

if [[ ! -d "$DOLT_DIR" ]]; then
  exit 0
fi

# Check whether a remote named "origin" is configured.
if ! (cd "$DOLT_DIR" && dolt remote -v 2>/dev/null | grep -q origin); then
  exit 0
fi

# Dirty-check: only add+commit if there are uncommitted changes.
# When dolt auto-commit is on, bd already committed; the tree would be clean.
DOLT_STATUS="$(cd "$DOLT_DIR" && dolt status 2>/dev/null)"
if ! printf '%s' "$DOLT_STATUS" | grep -q "nothing to commit"; then
  (cd "$DOLT_DIR" && dolt add . && dolt commit -m "bd sync" 2>/dev/null) || true
fi

# Push to remote. Suppress output; errors are non-fatal.
(cd "$DOLT_DIR" && dolt push origin main 2>/dev/null) || true

exit 0
