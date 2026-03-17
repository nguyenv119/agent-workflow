#!/usr/bin/env bash
# Tests that AGENTS.md contains the Multi-Machine Collaboration documentation section.
#
# These tests verify structural and content invariants of AGENTS.md so that
# the multi-machine sync documentation cannot be accidentally removed without
# a test failure alerting the author.

set -uo pipefail

AGENTS_MD="$(dirname "$0")/../AGENTS.md"
PASS=0
FAIL=0

pass() { echo "PASS: $1"; ((PASS++)); }
fail() { echo "FAIL: $1"; ((FAIL++)); }

# ---------------------------------------------------------------------------
# Test: multi_machine_section_header_exists
#
# What: Verifies that AGENTS.md contains a "Multi-Machine Collaboration" heading.
# Why:  This heading is the entry point for new team members discovering how to
#       set up cross-machine beads sync. Without it, the feature is invisible.
# What breaks: New team members have no documented path to configure DoltHub
#              remote sync; they will not know the feature exists.
# ---------------------------------------------------------------------------
if grep -q "## Multi-Machine Collaboration" "$AGENTS_MD"; then
  pass "multi_machine_section_header_exists"
else
  fail "multi_machine_section_header_exists"
fi

# ---------------------------------------------------------------------------
# Test: setup_remote_command_documented
#
# What: Verifies that the /setup-remote command name appears in AGENTS.md.
# Why:  /setup-remote is the one-time-per-project command that enables sync.
#       Documenting the correct command name prevents users from trying /setup
#       (wrong) and getting a "command not found" error.
# What breaks: Users run /setup instead of /setup-remote and the command fails.
# ---------------------------------------------------------------------------
if grep -q "/setup-remote" "$AGENTS_MD"; then
  pass "setup_remote_command_documented"
else
  fail "setup_remote_command_documented"
fi

# ---------------------------------------------------------------------------
# Test: dolthub_mentioned
#
# What: Verifies that "DoltHub" appears in AGENTS.md.
# Why:  DoltHub is the external service that hosts the remote beads database.
#       Users need to know they must create an account and a database there.
# What breaks: Users have no idea where to create the remote database; setup
#              stalls at the "provide your remote URL" step.
# ---------------------------------------------------------------------------
if grep -q "DoltHub" "$AGENTS_MD"; then
  pass "dolthub_mentioned"
else
  fail "dolthub_mentioned"
fi

# ---------------------------------------------------------------------------
# Test: dolt_pull_documented
#
# What: Verifies that "dolt pull" appears in AGENTS.md.
# Why:  Auto-pull behaviour (hooks run dolt pull before bd reads) is a key
#       part of the sync contract. Documenting it sets the right expectations:
#       beads data is always fresh when an agent runs a bd command.
# What breaks: Users assume beads data is stale and manually pull unnecessarily,
#              or worse, do not understand why their issue list updated itself.
# ---------------------------------------------------------------------------
if grep -q "dolt pull" "$AGENTS_MD"; then
  pass "dolt_pull_documented"
else
  fail "dolt_pull_documented"
fi

# ---------------------------------------------------------------------------
# Test: dolt_push_documented
#
# What: Verifies that "dolt push" appears in AGENTS.md.
# Why:  Auto-push behaviour (hooks run dolt push after bd writes) completes
#       the sync loop. Without documenting this, users do not know their writes
#       are automatically propagated to remote.
# What breaks: Users manually push thinking it is required, or fear that their
#              beads changes were lost when switching machines.
# ---------------------------------------------------------------------------
if grep -q "dolt push" "$AGENTS_MD"; then
  pass "dolt_push_documented"
else
  fail "dolt_push_documented"
fi

# ---------------------------------------------------------------------------
# Test: dolt_login_new_machine_step_documented
#
# What: Verifies that "dolt login" appears in AGENTS.md.
# Why:  New team members must authenticate before pull/push work. Missing this
#       step is the most common onboarding failure point.
# What breaks: New machines fail to sync with a DoltHub auth error, with no
#              in-repo documentation explaining why or how to fix it.
# ---------------------------------------------------------------------------
if grep -q "dolt login" "$AGENTS_MD"; then
  pass "dolt_login_new_machine_step_documented"
else
  fail "dolt_login_new_machine_step_documented"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
