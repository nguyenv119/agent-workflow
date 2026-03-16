#!/usr/bin/env bash
# Tests for bd-dolt-pull.sh and bd-dolt-push.sh
#
# These tests verify behavioral contracts of the Dolt sync hooks by stubbing
# external dependencies (dolt, bd) and feeding hook inputs via stdin JSON.
#
# Run: bash .claude/hooks/test-hooks.sh
# All tests must pass (exit 0) when hooks are correctly implemented.

set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)"
PULL_HOOK="$HOOKS_DIR/bd-dolt-pull.sh"
PUSH_HOOK="$HOOKS_DIR/bd-dolt-push.sh"

PASS=0
FAIL=0

# ──────────────────────────────────────────────────────────────────────────────
# Test harness
# ──────────────────────────────────────────────────────────────────────────────

run_test() {
  local name="$1"
  local result="$2"   # "pass" or "fail"
  if [[ "$result" == "pass" ]]; then
    echo "  PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $name"
    FAIL=$((FAIL + 1))
  fi
}

# Create a fresh isolated stub environment.
# Sets STUB_DIR, FAKE_BEADS_DIR, DOLT_CALL_LOG, STUB_DOLT_CALLS in the caller.
# Caller must call teardown_env after each test.
setup_env() {
  local has_remote="${1:-yes}"
  local dirty="${2:-yes}"
  local network_fail="${3:-0}"

  _STUB_DIR="$(mktemp -d)"
  _FAKE_BEADS_DIR="$(mktemp -d)"
  _DOLT_CALL_LOG="$(mktemp)"
  mkdir -p "$_FAKE_BEADS_DIR/dolt"

  # Write the DOLT_CALL_LOG path into the stub dir so the dolt stub can find it
  # even when called from deep subshells. We write the path to a known location.
  echo "$_DOLT_CALL_LOG" > "$_STUB_DIR/.dolt_call_log_path"

  local beads_dir="$_FAKE_BEADS_DIR"

  # Fake `bd` — `bd where` returns _FAKE_BEADS_DIR
  cat > "$_STUB_DIR/bd" <<BDEOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "where" ]]; then
  echo "$beads_dir"
fi
BDEOF
  chmod +x "$_STUB_DIR/bd"

  local log_path="$_DOLT_CALL_LOG"
  local remote="$has_remote"
  local dirty_flag="$dirty"
  local net_fail="$network_fail"

  # REVIEW: mocking core dependency — test may not reflect real dolt behavior
  # Fake `dolt` — records all invocations; simulates configurable behavior
  cat > "$_STUB_DIR/dolt" <<DOLTEOF
#!/usr/bin/env bash
# Read the call log path from the stub dir (robust across subshell nesting)
CALL_LOG="$log_path"
printf 'dolt %s\n' "\$*" >> "\$CALL_LOG"

case "\$1" in
  remote)
    if [[ "$remote" == "yes" ]]; then
      echo "origin  https://doltremoteapi.dolthub.com/org/repo  (fetch)"
    fi
    ;;
  status)
    if [[ "$dirty_flag" == "yes" ]]; then
      echo "On branch main"
      echo "Changes not staged for commit:"
      echo ""
      echo "	modified:	tasks"
    else
      echo "On branch main"
      echo "nothing to commit, working tree clean"
    fi
    ;;
  pull|push)
    exit $net_fail
    ;;
  add|commit)
    exit 0
    ;;
esac
DOLTEOF
  chmod +x "$_STUB_DIR/dolt"
}

teardown_env() {
  rm -rf "$_STUB_DIR" "$_FAKE_BEADS_DIR"
  rm -f "$_DOLT_CALL_LOG"
}

# Run a hook, return its exit code.
# Usage: run_hook HOOK_SCRIPT STDIN_JSON
run_hook() {
  local script="$1"
  local stdin_json="$2"
  local ec=0
  PATH="$_STUB_DIR:$PATH" bash "$script" <<< "$stdin_json" > /dev/null 2>&1 || ec=$?
  return $ec
}

# Count occurrences of a pattern in the call log.
count_calls() {
  local pattern="$1"
  grep -c "$pattern" "$_DOLT_CALL_LOG" 2>/dev/null || true
}

# ──────────────────────────────────────────────────────────────────────────────
# Check that hook scripts exist (will fail until Phase 2)
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "=== Hook existence ==="

if [[ -f "$PULL_HOOK" ]]; then
  run_test "bd-dolt-pull.sh exists" pass
else
  run_test "bd-dolt-pull.sh exists" fail
fi

if [[ -f "$PUSH_HOOK" ]]; then
  run_test "bd-dolt-push.sh exists" pass
else
  run_test "bd-dolt-push.sh exists" fail
fi

if [[ -x "$PULL_HOOK" ]]; then
  run_test "bd-dolt-pull.sh is executable" pass
else
  run_test "bd-dolt-pull.sh is executable" fail
fi

if [[ -x "$PUSH_HOOK" ]]; then
  run_test "bd-dolt-push.sh is executable" pass
else
  run_test "bd-dolt-push.sh is executable" fail
fi

# Exit early if scripts don't exist — remaining tests would error out
if [[ ! -f "$PULL_HOOK" || ! -f "$PUSH_HOOK" ]]; then
  echo ""
  echo "Hook scripts missing — skipping behavioral tests."
  echo "Results: $PASS passed, $FAIL failed"
  exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# Behavioral tests: bd-dolt-pull.sh
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "=== bd-dolt-pull.sh ==="

# Test: non-bd command is ignored immediately (no dolt calls)
#
# What: If the Bash command does not start with "bd", the pull hook must
#       exit 0 immediately without contacting dolt or running bd.
# Why:  Every Bash tool use in the agent fires this hook. If it doesn't
#       short-circuit on non-bd commands, latency is added to ALL bash calls
#       and dolt/bd must be installed for unrelated work.
# What breaks: Unexpected latency on git, npm, etc. commands; hook errors on
#              machines without bd installed would block unrelated work.
setup_env yes no 0
run_hook "$PULL_HOOK" '{"tool_input": {"command": "git status"}}' || true
CALL_COUNT=$(count_calls "dolt")
if [[ "$CALL_COUNT" -eq 0 ]]; then
  run_test "non-bd command: no dolt calls made" pass
else
  run_test "non-bd command: no dolt calls made" fail
fi
teardown_env

# Test: pull hook exits 0 even on network failure
#
# What: The pull hook must always exit 0, even when dolt pull fails.
# Why:  A hook that returns non-zero blocks the Claude Code tool call.
#       Network failures are transient — they must never prevent agent work.
# What breaks: If this fails, a network blip would prevent every bd command
#              from running, completely blocking the agent.
setup_env yes no 1
exit_code=0
run_hook "$PULL_HOOK" '{"tool_input": {"command": "bd list"}}' || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
  run_test "pull hook exits 0 on network failure" pass
else
  run_test "pull hook exits 0 on network failure" fail
fi
teardown_env

# Test: pull hook exits 0 when no remote configured
#
# What: When no dolt remote named "origin" exists, the hook must exit 0
#       without attempting a pull.
# Why:  Local-only setups must work exactly as before — no latency, no errors.
# What breaks: Local-only projects would fail or hang trying to reach a remote
#              that doesn't exist.
setup_env no no 0
exit_code=0
run_hook "$PULL_HOOK" '{"tool_input": {"command": "bd list"}}' || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
  run_test "pull hook exits 0 when no remote" pass
else
  run_test "pull hook exits 0 when no remote" fail
fi
PULL_CALLS=$(count_calls "dolt pull")
if [[ "$PULL_CALLS" -eq 0 ]]; then
  run_test "pull hook makes no pull call when no remote" pass
else
  run_test "pull hook makes no pull call when no remote" fail
fi
teardown_env

# Test: pull hook calls dolt pull when remote exists and command starts with bd
#
# What: When a bd command is run and a remote exists, dolt pull must be called.
# Why:  This is the core sync behavior — ensures the local db is current before
#       reading.
# What breaks: Multi-machine collaboration would silently break; agents on
#              different machines would see stale data.
setup_env yes no 0
run_hook "$PULL_HOOK" '{"tool_input": {"command": "bd show foo"}}' || true
PULL_CALLS=$(count_calls "dolt pull")
if [[ "$PULL_CALLS" -ge 1 ]]; then
  run_test "pull hook calls dolt pull for bd command with remote" pass
else
  run_test "pull hook calls dolt pull for bd command with remote" fail
fi
teardown_env

# ──────────────────────────────────────────────────────────────────────────────
# Behavioral tests: bd-dolt-push.sh
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "=== bd-dolt-push.sh ==="

# Test: non-bd command is ignored immediately
#
# What: Non-bd Bash commands must not trigger any dolt operations.
# Why:  Same reasoning as pull hook — every Bash call fires this hook.
# What breaks: Spurious pushes on every git or npm call; latency on all bash.
setup_env yes yes 0
run_hook "$PUSH_HOOK" '{"tool_input": {"command": "git commit -m foo"}}' || true
CALL_COUNT=$(count_calls "dolt")
if [[ "$CALL_COUNT" -eq 0 ]]; then
  run_test "push hook: non-bd command makes no dolt calls" pass
else
  run_test "push hook: non-bd command makes no dolt calls" fail
fi
teardown_env

# Test: read-only bd commands do not trigger a push
#
# What: Subcommands like `bd show`, `bd list`, `bd search` must not trigger
#       a dolt push — they don't modify the database.
# Why:  Pushing after reads wastes network bandwidth and time; it could also
#       push a stale commit over changes made by another machine.
# What breaks: Every read triggers a push, causing unnecessary network traffic
#              and potential race conditions in team settings.
for readonly_cmd in show list ready search query count diff history status types blocked stale find-duplicates lint where version help doctor prime recall preflight children orphans; do
  setup_env yes yes 0
  run_hook "$PUSH_HOOK" "{\"tool_input\": {\"command\": \"bd $readonly_cmd foo\"}}" || true
  PUSH_CALLS=$(count_calls "dolt push")
  if [[ "$PUSH_CALLS" -eq 0 ]]; then
    run_test "push hook: read-only 'bd $readonly_cmd' triggers no push" pass
  else
    run_test "push hook: read-only 'bd $readonly_cmd' triggers no push" fail
  fi
  teardown_env
done

# Test: write bd command triggers a push
#
# What: Subcommands that mutate the database (e.g., `bd create`, `bd update`,
#       `bd close`) must trigger dolt add + commit + push.
# Why:  This is the core sync behavior — ensures writes are propagated to the
#       remote so collaborating machines see the new state.
# What breaks: Changes from one machine would never reach other machines,
#              breaking multi-machine collaboration silently.
for write_cmd in create update close reopen delete assign unassign label unlabel link unlink; do
  setup_env yes yes 0
  run_hook "$PUSH_HOOK" "{\"tool_input\": {\"command\": \"bd $write_cmd foo\"}}" || true
  PUSH_CALLS=$(count_calls "dolt push")
  if [[ "$PUSH_CALLS" -ge 1 ]]; then
    run_test "push hook: write command 'bd $write_cmd' triggers push" pass
  else
    run_test "push hook: write command 'bd $write_cmd' triggers push" fail
  fi
  teardown_env
done

# Test: push hook skips commit when working tree is clean (auto-commit mode)
#
# What: When dolt status shows a clean tree, the hook must skip `dolt add`
#       and `dolt commit` but still run `dolt push`.
# Why:  When dolt auto-commit is on, bd already committed. Attempting to
#       commit again creates an empty commit error, breaking the push.
# What breaks: In auto-commit mode, every bd write would fail to sync due to
#              a "nothing to commit" error aborting the hook before push.
setup_env yes no 0   # dirty=no → clean tree
run_hook "$PUSH_HOOK" '{"tool_input": {"command": "bd create test"}}' || true
ADD_CALLS=$(count_calls "dolt add")
COMMIT_CALLS=$(count_calls "dolt commit")
PUSH_CALLS=$(count_calls "dolt push")
if [[ "$ADD_CALLS" -eq 0 && "$COMMIT_CALLS" -eq 0 ]]; then
  run_test "push hook: clean tree skips dolt add+commit" pass
else
  run_test "push hook: clean tree skips dolt add+commit" fail
fi
if [[ "$PUSH_CALLS" -ge 1 ]]; then
  run_test "push hook: clean tree still runs dolt push" pass
else
  run_test "push hook: clean tree still runs dolt push" fail
fi
teardown_env

# Test: push hook exits 0 on network failure
#
# What: The push hook must always exit 0, even when dolt push fails.
# Why:  Network failures are transient — they must never block agent work.
# What breaks: A flaky network would make every bd write command abort, leaving
#              the agent unable to manage tasks.
setup_env yes yes 1   # network_fail=1
exit_code=0
run_hook "$PUSH_HOOK" '{"tool_input": {"command": "bd create test"}}' || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
  run_test "push hook exits 0 on network failure" pass
else
  run_test "push hook exits 0 on network failure" fail
fi
teardown_env

# Test: push hook exits 0 when no remote configured
#
# What: When no dolt remote named "origin" exists, the hook must exit 0
#       without attempting any dolt operations.
# Why:  Local-only setups must not be affected by the hook at all.
# What breaks: Local-only projects would get spurious errors or latency.
setup_env no yes 0
exit_code=0
run_hook "$PUSH_HOOK" '{"tool_input": {"command": "bd create test"}}' || exit_code=$?
if [[ "$exit_code" -eq 0 ]]; then
  run_test "push hook exits 0 when no remote" pass
else
  run_test "push hook exits 0 when no remote" fail
fi
PUSH_CALLS=$(count_calls "dolt push")
if [[ "$PUSH_CALLS" -eq 0 ]]; then
  run_test "push hook makes no push call when no remote" pass
else
  run_test "push hook makes no push call when no remote" fail
fi
teardown_env

# ──────────────────────────────────────────────────────────────────────────────
# Settings.json tests
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "=== settings.json ==="

SETTINGS="$(dirname "$HOOKS_DIR")/settings.json"

# Test: settings.json has PreToolUse hook for Bash
#
# What: The settings.json must contain a PreToolUse hook entry with matcher
#       "Bash" pointing to bd-dolt-pull.sh.
# Why:  Without this entry, Claude Code will not fire the pull hook before
#       tool calls, breaking the pre-read sync.
# What breaks: No pull happens before bd commands — multi-machine reads are stale.
if jq -e '.hooks.PreToolUse[]?.hooks[]?.command | select(contains("bd-dolt-pull.sh"))' "$SETTINGS" > /dev/null 2>&1; then
  run_test "settings.json: PreToolUse hook references bd-dolt-pull.sh" pass
else
  run_test "settings.json: PreToolUse hook references bd-dolt-pull.sh" fail
fi

# Test: settings.json has PostToolUse hook for Bash
#
# What: The settings.json must contain a PostToolUse hook entry with matcher
#       "Bash" pointing to bd-dolt-push.sh.
# Why:  Without this entry, Claude Code will not fire the push hook after
#       tool calls, breaking the post-write sync.
# What breaks: No push happens after bd writes — collaborating machines never
#              see new tasks/changes.
if jq -e '.hooks.PostToolUse[]?.hooks[]?.command | select(contains("bd-dolt-push.sh"))' "$SETTINGS" > /dev/null 2>&1; then
  run_test "settings.json: PostToolUse hook references bd-dolt-push.sh" pass
else
  run_test "settings.json: PostToolUse hook references bd-dolt-push.sh" fail
fi

# Test: PreToolUse hook matcher is "Bash"
#
# What: The PreToolUse matcher must be "Bash" (the tool name), not a command
#       string pattern.
# Why:  Claude Code only supports matching on tool name, not command content.
#       Using a wrong matcher silently skips the hook entirely.
# What breaks: Hooks are never fired — the entire sync system is silently dead.
if jq -e '.hooks.PreToolUse[]? | select(.hooks[]?.command | contains("bd-dolt-pull.sh")) | .matcher' "$SETTINGS" 2>/dev/null | grep -q "Bash"; then
  run_test "settings.json: PreToolUse matcher is 'Bash'" pass
else
  run_test "settings.json: PreToolUse matcher is 'Bash'" fail
fi

# Test: PostToolUse hook matcher is "Bash"
#
# What: The PostToolUse matcher must be "Bash" (the tool name), not a command
#       string pattern.
# Why:  Claude Code only supports matching on tool name, not command content.
#       Using a wrong matcher silently skips the hook entirely.
# What breaks: Hooks are never fired — the entire sync system is silently dead.
if jq -e '.hooks.PostToolUse[]? | select(.hooks[]?.command | contains("bd-dolt-push.sh")) | .matcher' "$SETTINGS" 2>/dev/null | grep -q "Bash"; then
  run_test "settings.json: PostToolUse matcher is 'Bash'" pass
else
  run_test "settings.json: PostToolUse matcher is 'Bash'" fail
fi

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo "Results: $PASS passed, $FAIL failed"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
