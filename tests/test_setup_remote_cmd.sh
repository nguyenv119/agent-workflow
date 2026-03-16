#!/usr/bin/env bash
# Tests for .claude/commands/setup-remote.md
#
# What: Verifies the setup-remote command file exists and contains all required
#       behavioral instructions for the agent, covering every step of the
#       acceptance criteria from task agent-workflow-1ro.1.
#
# Why: The setup-remote command is a pure agent-instruction document. Its
#      correctness is the presence and coherence of the instruction steps —
#      not runtime behavior. Automating this check ensures regressions
#      (e.g., accidentally deleting a step or the file) are caught immediately.
#
# What breaks if violated: A user running /setup-remote would receive an
#      incomplete or missing workflow, leaving their DoltHub remote unconfigured
#      and multi-machine collaboration broken.

set -uo pipefail

CMD_FILE="$(dirname "$0")/../.claude/commands/setup-remote.md"

pass=0
fail=0

assert_contains() {
    # Asserts that the command file contains the given pattern.
    # Usage: assert_contains <description> [grep-flags] <pattern>
    # $1 = description of what is being checked
    # $2 = optional grep flag (e.g. -i) OR pattern if no flag
    # $3 = pattern (if $2 is a flag)
    local desc="$1"
    shift
    if grep -q "$@" "$CMD_FILE" 2>/dev/null; then
        echo "  PASS: $desc"
        pass=$((pass + 1))
    else
        echo "  FAIL: $desc"
        echo "        Expected pattern not found: $*"
        fail=$((fail + 1))
    fi
}

echo "=== test_setup_remote_cmd.sh ==="
echo ""

# Test: file must exist
# What: The command file must exist at the canonical path.
# Why: Claude Code discovers slash commands by file name in .claude/commands/.
# What breaks: /setup-remote would not be available to users at all.
if [ -f "$CMD_FILE" ]; then
    echo "  PASS: command file exists at .claude/commands/setup-remote.md"
    pass=$((pass + 1))
else
    echo "  FAIL: command file does NOT exist at .claude/commands/setup-remote.md"
    fail=$((fail + 1))
fi

# Test: asks user for DoltHub remote URL
# What: The command must prompt the user for their DoltHub remote URL.
# Why: Without the URL there is nothing to configure — the agent cannot
#      proceed with dolt remote add.
# What breaks: Agent would have no URL to pass to dolt remote add, leaving
#              the remote unconfigured.
assert_contains \
    "prompts user for DoltHub remote URL" \
    -i "dolthub"

# Test: checks that dolt CLI is installed
# What: The command must verify dolt is installed before running dolt commands.
# Why: Running dolt on a machine without it produces a confusing "command not
#      found" error with no remediation guidance.
# What breaks: Agent fails with an opaque shell error instead of a clear
#              "please install dolt" message.
assert_contains \
    "checks dolt CLI is installed (which dolt)" \
    "which dolt"

# Test: checks for dolt login credentials
# What: The command must verify the user has run dolt login.
# Why: dolt push to DoltHub requires authentication; without credentials the
#      push will fail with an auth error after all other setup steps succeed.
# What breaks: The remote is configured locally but push fails silently or
#              with a confusing auth error.
assert_contains \
    "checks for dolt login credentials (~/.dolt/creds)" \
    "\.dolt/creds"

# Test: uses subshell form for dolt remote add (no -C flag)
# What: The dolt remote add command must use (cd .beads/dolt && dolt ...) form.
# Why: dolt does not support the -C flag that git supports; using -C would
#      cause a runtime failure.
# What breaks: /setup-remote errors on the dolt remote add step for all users.
assert_contains \
    "uses subshell form for dolt remote add" \
    "cd .beads/dolt"

assert_contains \
    "includes dolt remote add origin" \
    "dolt remote add origin"

# Test: uses subshell form for dolt push
# What: The dolt push command must use (cd .beads/dolt && dolt ...) form.
# Why: Same as above — dolt does not support -C.
# What breaks: /setup-remote errors on the push step, leaving the remote
#              configured locally but with no data on DoltHub.
assert_contains \
    "includes dolt push -u origin main via subshell" \
    "dolt push"

# Test: confirms success to the user
# What: The command must instruct the agent to confirm success after push.
# Why: Without explicit confirmation, users have no feedback that the operation
#      completed and may re-run it, creating duplicate remotes.
# What breaks: User is left uncertain whether setup succeeded.
assert_contains \
    "confirms success to user after push" \
    -iE "success|confirm|configured|complete"

# Test: instructs agent to verify with dolt remote -v
# What: The command should instruct verification via dolt remote -v.
# Why: Verifying the remote is listed closes the loop — it confirms dolt
#      accepted the configuration before the user walks away.
# What breaks: Setup could silently fail and user would not know until
#              they try to push from another machine.
assert_contains \
    "instructs agent to verify with dolt remote -v" \
    "dolt remote"

echo ""
echo "Results: $pass passed, $fail failed"

if [ "$fail" -gt 0 ]; then
    exit 1
fi
exit 0
