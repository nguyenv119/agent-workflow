#!/usr/bin/env bash
# Acceptance tests for heavy-test-lock.sh (bead agent-workflow-2ux.2).
#
# heavy-test-lock.sh's lock path is machine-global by design
# ("${TMPDIR:-/tmp}/agent-workflow-heavy-test.lock") so it serializes heavy
# runs across every worktree/repo on the box. That means these tests MUST
# NOT use the real ambient TMPDIR — colliding with a real run, with each
# other, or leaking a stale lock into `/tmp` would be its own bug. Every
# test case below creates its own throwaway sandbox via `mktemp -d` and
# points the wrapper at it with `TMPDIR=<sandbox>`, so each test's lock path
# is unique. All sandboxes are removed by a single EXIT trap so cleanup
# happens even if a test errors out early.
#
# Run: bash .claude/bin/heavy-test-lock.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_SCRIPT="$SCRIPT_DIR/heavy-test-lock.sh"

failures=0
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; failures=$((failures + 1)); }

# Sandboxes accumulate here and are wiped by the EXIT trap below — this is
# the "clean up temp dirs in a trap" requirement, done once centrally rather
# than duplicated in every test function.
SANDBOXES=()
# Sets $SANDBOX to a fresh throwaway directory and records it for cleanup.
# Must be called as a bare statement, NOT inside $(...) — command
# substitution forks a subshell, and the SANDBOXES+=() append below would
# mutate only that subshell's copy of the array, silently discarded when it
# exits, leaking every sandbox this script creates.
new_sandbox() {
  SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/heavy-test-lock-test.XXXXXX")"
  SANDBOXES+=("$SANDBOX")
}
cleanup_sandboxes() {
  local dir
  for dir in "${SANDBOXES[@]:-}"; do
    [ -n "$dir" ] && rm -rf "$dir"
  done
}
trap cleanup_sandboxes EXIT

# Shared assertion: does the log produced by concurrent marker runs show
# fully non-overlapping "start N" / "end N" pairs (each start immediately
# followed by its own matching end, before any other start appears)?
# Used by both the serialization test and the concurrent-stale-reclaim test.
log_is_serialized() {
  local log="$1" expect_end="" line
  while IFS= read -r line; do
    case "$line" in
      start\ *)
        [ -n "$expect_end" ] && return 1
        expect_end="end ${line#start }"
        ;;
      end\ *)
        [ "$line" = "$expect_end" ] || return 1
        expect_end=""
        ;;
    esac
  done < "$log"
  [ -z "$expect_end" ]
}

# ---------------------------------------------------------------------------
# WHAT: three concurrent heavy-test-lock.sh invocations never run their
#       wrapped command at the same time.
# WHY: this is the entire point of the lock — without it, N agents each
#      spawning a heavy suite starve the machine (the meltdown this epic
#      exists to prevent).
# BREAKS: if this fails, the lock is not actually mutually exclusive and
#         concurrent heavy runs pile up exactly like before the fix.
# ---------------------------------------------------------------------------
test_serialization() {
  local name="serializes three concurrent runs so they never overlap"
  # GIVEN — a fresh sandbox and a marker command whose start/end would
  # interleave in the log if run concurrently (each sleeps 1s between them).
  local sandbox log
  new_sandbox
  sandbox="$SANDBOX"
  log="$sandbox/log"
  : > "$log"

  # WHEN — three wrapped runs are launched at once.
  local pids=() i
  for i in 1 2 3; do
    TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c "echo \"start \$\$\" >>\"$log\"; sleep 1; echo \"end \$\$\" >>\"$log\"" &
    pids+=("$!")
  done
  local pid
  for pid in "${pids[@]}"; do
    wait "$pid"
  done

  # THEN — the log shows three well-paired, non-overlapping start/end runs.
  local line_count
  line_count="$(wc -l < "$log" | tr -d ' ')"
  if log_is_serialized "$log" && [ "$line_count" -eq 6 ]; then
    pass "$name"
  else
    fail "$name (log: $(tr '\n' '|' < "$log"))"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: while a wrapper holds the lock, $LOCK/pid equals the WRAPPER's own
#       actual pid, and $LOCK/pgid equals the WRAPPED COMMAND's actual
#       process group — not merely "some numeric value".
# WHY: bead 2ux.3's reaper hook keys off exactly these two files to identify
#      and PGID-scope-kill a crashed holder's orphans without touching a
#      live sibling. A numeric-but-wrong value (e.g. the wrapper's own pgid
#      instead of the wrapped command's) would pass a shallow "is it
#      numeric" check yet still make the reaper kill the wrong process group
#      or miss the real one entirely.
# BREAKS: a missing/non-numeric pid or pgid means the reaper either can't
#         detect a dead holder at all (wedges the machine forever) or, worse,
#         passes a garbage-but-plausible value to `kill` on a scoped process
#         group, killing an unrelated live sibling's workers.
# ---------------------------------------------------------------------------
test_records_pid_and_pgid() {
  local name="records the wrapper's real pid and the wrapped command's real pgid, not just any numeric value"
  # GIVEN — a wrapper holding the lock, running a command that exposes its
  # own pid to us via a marker file so we can independently query its real
  # pgid with `ps` — ground truth derived separately from anything the
  # script itself recorded, so a hardcoded or wrong-but-numeric value (e.g.
  # pgid=1, or the wrapper's own pgid) cannot pass by accident.
  local sandbox lock marker
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  marker="$sandbox/child.pid"

  # WHEN — the wrapper acquires; the wrapped command records its own pid,
  # then sleeps long enough for us to read everything before it exits.
  TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c "echo \$\$ > \"$marker\"; sleep 2" &
  local holder_pid=$!

  local waited=0
  while [ ! -s "$marker" ] && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  local child_actual_pid child_actual_pgid
  child_actual_pid="$(cat "$marker" 2>/dev/null || true)"
  child_actual_pgid="$(ps -o pgid= -p "$child_actual_pid" 2>/dev/null | tr -d ' ')"

  waited=0
  while [ ! -f "$lock/pgid" ] && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  local pid_val pgid_val
  pid_val="$(cat "$lock/pid" 2>/dev/null || true)"
  pgid_val="$(cat "$lock/pgid" 2>/dev/null || true)"
  wait "$holder_pid" 2>/dev/null

  # THEN — $LOCK/pid is exactly the wrapper's own pid, and $LOCK/pgid is
  # exactly the wrapped command's real, independently-queried pgid.
  if [ -n "$child_actual_pgid" ] && [ "$pid_val" = "$holder_pid" ] && [ "$pgid_val" = "$child_actual_pgid" ]; then
    pass "$name"
  else
    fail "$name (pid_val='$pid_val' holder_pid='$holder_pid' pgid_val='$pgid_val' child_actual_pgid='$child_actual_pgid')"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: a lock left behind by a dead pid is reclaimed quickly, not blocked
#       until HEAVY_TEST_LOCK_TIMEOUT.
# WHY: a killed agent must not wedge every future heavy run on the machine —
#      this is the load-bearing protection, since a hard `kill -9` skips the
#      wrapper's own EXIT trap.
# BREAKS: without this, one crashed agent permanently blocks all heavy test
#         runs on the machine until someone manually rm -rf's the lock.
# ---------------------------------------------------------------------------
test_stale_holder_release() {
  local name="reclaims a dead-pid lock within ~2s instead of blocking to the timeout"
  # GIVEN — a lock dir "held" by a pid guaranteed not to exist, and a large
  # default timeout so a pass can only mean the stale-reclaim path fired.
  local sandbox lock
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  mkdir "$lock"
  echo 999999 > "$lock/pid"
  echo 999999 > "$lock/pgid"

  # WHEN — a wrapper run is timed.
  local start_ts end_ts elapsed rc
  start_ts="$(date +%s)"
  TMPDIR="$sandbox" HEAVY_TEST_LOCK_TIMEOUT=1800 "$LOCK_SCRIPT" true
  rc=$?
  end_ts="$(date +%s)"
  elapsed=$((end_ts - start_ts))

  # THEN — it acquired (ran the command, rc 0) well within the timeout.
  if [ "$rc" -eq 0 ] && [ "$elapsed" -le 2 ]; then
    pass "$name"
  else
    fail "$name (rc=$rc elapsed=${elapsed}s)"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: with a dead-pid lock in place, three concurrent waiters reclaim it
#       without double-acquiring — exactly one runs at a time, all three
#       eventually complete, and none leaks a raw filesystem error to
#       stderr while doing so.
# WHY: regression test for the TOCTOU race in stale reclaim (S2): a naive
#      `rm -rf "$LOCK"; mkdir "$LOCK"` lets every waiter's `rm` succeed, so
#      all of them can `mkdir` and believe they hold the lock simultaneously.
#      This is also the highest-contention path over the mkdir-then-pid-write
#      window a losing waiter can yank the winner's freshly-created lock dir
#      out from under (see acquire_lock's ponytail comment) — bead 2ux.2
#      finding A2, where `echo ... > "$LOCK/pid" 2>/dev/null` does NOT
#      suppress the redirect-failure message once the target directory
#      disappears mid-write (bash applies `>` before `2>/dev/null`).
# BREAKS: if the serialization regresses, "stale holder released" silently
#         becomes "N agents now run heavy suites concurrently" — the exact
#         meltdown this epic exists to prevent, just moved to the reclaim
#         path. If the stderr-suppression regresses, a losing waiter's raw
#         "No such file or directory" leaks into whatever invoked the
#         wrapper (e.g. a CI log), which a strict caller could mistake for a
#         real failure even though the wrapper itself still degrades safely.
# ---------------------------------------------------------------------------
test_concurrent_stale_reclaim() {
  local name="exactly one of several concurrent waiters reclaims a dead-pid lock, none leaking raw errors to stderr"
  # GIVEN — a dead-pid lock and three waiters launched simultaneously, each
  # with its own stderr captured separately so a leaked redirect-failure
  # message from any one of them is individually attributable.
  local sandbox lock log
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  log="$sandbox/log"
  : > "$log"
  mkdir "$lock"
  echo 999999 > "$lock/pid"
  echo 999999 > "$lock/pgid"

  # WHEN — three wrapped runs race to reclaim the stale lock.
  local pids=() err_files=() i
  for i in 1 2 3; do
    TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c "echo \"start \$\$\" >>\"$log\"; sleep 0.5; echo \"end \$\$\" >>\"$log\"" 2>"$sandbox/err.$i" &
    pids+=("$!")
    err_files+=("$sandbox/err.$i")
  done
  local pid
  for pid in "${pids[@]}"; do
    wait "$pid"
  done

  # THEN — all three ran, strictly serialized (no double-acquire), and no
  # wrapper's stderr file picked up a raw bash/OS error from the race.
  local line_count leaked=0 err_file
  line_count="$(wc -l < "$log" | tr -d ' ')"
  for err_file in "${err_files[@]}"; do
    [ -s "$err_file" ] && leaked=1
  done
  if log_is_serialized "$log" && [ "$line_count" -eq 6 ] && [ "$leaked" -eq 0 ]; then
    pass "$name"
  else
    fail "$name (log: $(tr '\n' '|' < "$log") leaked=$leaked $(for err_file in "${err_files[@]}"; do [ -s "$err_file" ] && echo "[$err_file: $(cat "$err_file")]"; done))"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: after a normal (unsignaled) run, the lock directory is gone.
# WHY: the wrapper must release on every exit path, or the very next run
#      would see a "held" lock and either block or (if the holder pid was
#      reused) misjudge staleness.
# BREAKS: a leaked lock dir after a clean run wedges the machine identically
#         to the crash case this whole bead exists to avoid.
# ---------------------------------------------------------------------------
test_release_on_normal_exit() {
  local name="releases the lock directory after a normal run"
  # GIVEN — a fresh sandbox.
  local sandbox lock
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"

  # WHEN — a wrapped command runs to completion.
  TMPDIR="$sandbox" "$LOCK_SCRIPT" true

  # THEN — no lock dir remains.
  if [ ! -d "$lock" ]; then
    pass "$name"
  else
    fail "$name (lock dir still present: $lock)"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: SIGTERM-ing the wrapper releases the lock AND kills the still-running
#       wrapped child (not just the wrapper itself).
# WHY: the bead calls out a specific gap — "lock freed but test still
#      running unlocked" — where a signaled wrapper drops the lock but
#      leaves its heavy child running with no serialization protecting it.
# BREAKS: a signaled agent would free the lock for the next waiter while its
#         own orphaned test process keeps burning CPU unlocked — the two
#         heavy runs now overlap exactly as if there were no lock.
# ---------------------------------------------------------------------------
test_release_on_signal() {
  local name="SIGTERM releases the lock and kills the running child"
  # GIVEN — a wrapper running a long-sleeping child that records its own pid.
  local sandbox lock marker
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  marker="$sandbox/child.pid"

  TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c "echo \$\$ > \"$marker\"; sleep 30" &
  local wrapper_pid=$!

  local waited=0
  while [ ! -s "$marker" ] && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  local child_pid
  child_pid="$(cat "$marker" 2>/dev/null || true)"

  # WHEN — the wrapper is sent SIGTERM.
  kill -TERM "$wrapper_pid" 2>/dev/null
  wait "$wrapper_pid" 2>/dev/null
  sleep 0.3 # let the OS finish tearing down the killed child

  # THEN — the lock is gone AND the child is no longer running.
  local lock_gone=0 child_gone=0
  [ ! -d "$lock" ] && lock_gone=1
  if [ -n "$child_pid" ] && ! kill -0 "$child_pid" 2>/dev/null; then
    child_gone=1
  fi

  if [ "$lock_gone" -eq 1 ] && [ "$child_gone" -eq 1 ]; then
    pass "$name"
  else
    fail "$name (lock_gone=$lock_gone child_gone=$child_gone child_pid=$child_pid)"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: SIGTERM-ing the wrapper kills the wrapped command's ENTIRE process
#       tree, including a grandchild the wrapped command itself forked and
#       backgrounded — not just the direct child pid.
# WHY: real heavy commands (pnpm/turbo/vitest) fork+supervise worker
#      processes rather than doing the work in a single pid. A trap that
#      only signals its direct child (`kill "$child"`) reaches the
#      supervisor but not the workers it already spawned: the lock frees for
#      the next waiter while those workers keep running unlocked — bead
#      2ux.2 finding A1, and the exact meltdown this lock exists to prevent.
# BREAKS: a naive single-pid kill in the trap makes every "signaled cleanly"
#         heavy run leave orphaned, unlocked worker processes behind,
#         silently reproducing the machine-meltdown this whole lock exists
#         to stop — while every OTHER test in this suite (which only ever
#         signals single-process commands) would keep passing, masking it.
# ---------------------------------------------------------------------------
test_sigterm_reaps_entire_process_tree() {
  local name="SIGTERM kills the wrapped command's whole process tree, including a forked grandchild"
  # GIVEN — a wrapper running a command that itself forks and backgrounds a
  # long-sleeping grandchild (mimics a supervisor forking a worker) and
  # records that grandchild's pid so we can check on it independently.
  local sandbox lock marker
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  marker="$sandbox/grandchild.pid"

  TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c "sleep 300 & echo \$! > \"$marker\"; wait" &
  local wrapper_pid=$!

  local waited=0
  while [ ! -s "$marker" ] && [ "$waited" -lt 50 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  local grandchild_pid
  grandchild_pid="$(cat "$marker" 2>/dev/null || true)"

  # WHEN — the wrapper is sent SIGTERM.
  kill -TERM "$wrapper_pid" 2>/dev/null
  wait "$wrapper_pid" 2>/dev/null
  sleep 0.5 # generous: let the OS finish tearing down the whole tree

  # THEN — the grandchild the wrapped command forked is dead (not just the
  # direct child), and the lock dir is gone.
  local grandchild_gone=0 lock_gone=0
  if [ -n "$grandchild_pid" ] && ! kill -0 "$grandchild_pid" 2>/dev/null; then
    grandchild_gone=1
  fi
  [ ! -d "$lock" ] && lock_gone=1

  if [ "$grandchild_gone" -eq 1 ] && [ "$lock_gone" -eq 1 ]; then
    pass "$name"
  else
    fail "$name (grandchild_gone=$grandchild_gone lock_gone=$lock_gone grandchild_pid=$grandchild_pid)"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: when the lock cannot be acquired within HEAVY_TEST_LOCK_TIMEOUT, the
#       wrapper degrades to an unlocked run instead of failing — prints a
#       loud stderr warning, still runs the command, and exits with the
#       command's own exit code. The live holder's lock is left untouched.
# WHY: an acquire-timeout is a capacity/contention signal, not a test
#      failure — surfacing it as a FAIL would manufacture a phantom failure
#      (the same class of bug this whole epic exists to eliminate).
# BREAKS: without the degrade, legitimate heavy contention (e.g. an
#         unusually long-running sibling) would turn into spurious red
#         builds; and if the degrade path touched the lock, it could rip a
#         still-live holder's lock out from under it.
# ---------------------------------------------------------------------------
test_acquire_timeout_degrades() {
  local name="a timed-out acquire degrades to an unlocked run, never a FAIL"
  # GIVEN — the lock held by OUR OWN (definitely live) pid, and a 1s timeout.
  local sandbox lock err
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  err="$sandbox/stderr"
  mkdir "$lock"
  echo "$$" > "$lock/pid"
  echo "$$" > "$lock/pgid"

  # WHEN — a wrapper run cannot acquire within the timeout.
  local rc
  TMPDIR="$sandbox" HEAVY_TEST_LOCK_TIMEOUT=1 "$LOCK_SCRIPT" true 2>"$err"
  rc=$?

  # THEN — it warned on stderr, still ran the command (rc 0), and left the
  # live holder's lock exactly as it was (degrade must never rm a live lock).
  local warned=0 lock_untouched=0
  grep -qi "unlocked" "$err" && warned=1
  [ -d "$lock" ] && [ -f "$lock/pid" ] && lock_untouched=1

  if [ "$rc" -eq 0 ] && [ "$warned" -eq 1 ] && [ "$lock_untouched" -eq 1 ]; then
    pass "$name"
  else
    fail "$name (rc=$rc warned=$warned lock_untouched=$lock_untouched stderr=$(cat "$err" 2>/dev/null))"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: the wrapper exits with the wrapped command's own exit code (not 0,
#       not a lock-related code) — and still releases the lock.
# WHY: the bead requires "run and capture" instead of `exec` specifically so
#      the wrapper's own exit reflects the command's real result; a caller
#      (e.g. CI) branches on this exit code to decide pass/fail.
# BREAKS: a swallowed or wrong exit code would make a genuinely failing
#         heavy suite report success (or vice versa) to whatever invoked
#         the wrapper.
# ---------------------------------------------------------------------------
test_propagates_nonzero_exit_code() {
  local name="propagates the wrapped command's own non-zero exit code"
  # GIVEN — a fresh sandbox and a command with a distinctive exit code.
  local sandbox lock
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"

  # WHEN — the wrapper runs it.
  TMPDIR="$sandbox" "$LOCK_SCRIPT" bash -c 'exit 7'
  local rc=$?

  # THEN — the wrapper's exit code matches, and the lock is still released.
  if [ "$rc" -eq 7 ] && [ ! -d "$lock" ]; then
    pass "$name"
  else
    fail "$name (rc=$rc lock_dir_exists=$([ -d "$lock" ] && echo yes || echo no))"
  fi
}

# ---------------------------------------------------------------------------
# WHAT: invoking the wrapper with no command prints usage to stderr and
#       exits non-zero without ever touching the lock.
# WHY: this is a caller-error path (misconfigured quality-gate command) —
#      it must fail loudly and fast, not hang waiting on a lock it has
#      nothing to run under, and not leak a lock dir it never needed.
# BREAKS: a silent no-op or a hang here would make a misconfigured caller
#         invocation fail mysteriously instead of with a clear message.
# ---------------------------------------------------------------------------
test_no_args_shows_usage() {
  local name="no-args invocation prints usage, exits non-zero, touches no lock"
  # GIVEN — a fresh sandbox and no command to run.
  local sandbox lock err
  new_sandbox
  sandbox="$SANDBOX"
  lock="$sandbox/agent-workflow-heavy-test.lock"
  err="$sandbox/stderr"

  # WHEN — the wrapper is invoked with zero arguments.
  TMPDIR="$sandbox" "$LOCK_SCRIPT" 2>"$err"
  local rc=$?

  # THEN — non-zero exit, a usage message, and no lock dir was created.
  local usage_shown=0
  grep -qi "usage" "$err" && usage_shown=1

  if [ "$rc" -ne 0 ] && [ "$usage_shown" -eq 1 ] && [ ! -d "$lock" ]; then
    pass "$name"
  else
    fail "$name (rc=$rc usage_shown=$usage_shown stderr=$(cat "$err" 2>/dev/null))"
  fi
}

echo "=== heavy-test-lock.sh acceptance tests ==="
test_serialization
test_records_pid_and_pgid
test_stale_holder_release
test_concurrent_stale_reclaim
test_release_on_normal_exit
test_release_on_signal
test_sigterm_reaps_entire_process_tree
test_acquire_timeout_degrades
test_propagates_nonzero_exit_code
test_no_args_shows_usage
echo "==="

if [ "$failures" -eq 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "$failures test(s) FAILED"
  exit 1
fi
