#!/usr/bin/env bash
# heavy-test-lock.sh — machine-global one-heavy-run-at-a-time lock.
#
# Serializes CPU-heavy test invocations (e.g. a full test suite) to one at a
# time across ALL worktrees/repos on the machine, so N concurrent agents
# can't each spawn a heavy suite and starve the machine (see bead
# agent-workflow-2ux for the "meltdown" this prevents).
#
# Lock is an atomic `mkdir` directory — macOS has no `flock`. A holder whose
# pid is no longer alive is treated as stale and reclaimed TOCTOU-safely
# (rename-then-delete, not rm-then-mkdir: see acquire_lock below). On
# acquire, both the holder's pid and process-group id (pgid) are recorded so
# a companion Stop-hook reaper (bead 2ux.3) can kill exactly a crashed
# holder's orphaned workers without touching a live sibling's run.
#
# A lock ACQUIRE timeout (HEAVY_TEST_LOCK_TIMEOUT) is contention, not
# failure: it degrades to an unlocked run with a loud stderr warning and
# forwards the wrapped command's own exit code. It must never itself
# manufacture a non-zero ("FAIL") exit — that would turn ordinary machine
# contention into a phantom test failure, exactly what this epic exists to
# stop. A genuine TEST timeout (the wrapped command itself hanging) is a
# separate concern and is untouched here — it still fails normally, since
# `wait` simply returns whatever exit code the child produces.
#
# Deliberately no `set -e`: acquire_lock's control flow depends on `mkdir`,
# `mv`, and `kill -0` "failing" as ordinary, expected branches (stale
# reclaim, contention polling). Reasoning explicitly about every branch is
# clearer here than auditing which fallible commands set -e happens to
# exempt (if/while conditions, non-last members of && chains, ...).
#
# ponytail: this is a semaphore of 1 (mutex) — correct for a dev laptop
# where one scoped heavy run already fills the cores. To allow N>1
# concurrent heavy runs on a bigger machine, generalize to N slot-dirs
# ($LOCK.slot-1..N) gated by env HEAVY_TEST_SLOTS (default 1). Not built now
# (YAGNI) — no caller needs more than 1 slot yet.
#
# Usage:
#   heavy-test-lock.sh <command> [args...]
#
# Env:
#   HEAVY_TEST_LOCK_TIMEOUT   Seconds to wait for the lock before degrading
#                             to an unlocked run. Default 1800 (30 min).
set -u

LOCK="${TMPDIR:-/tmp}/agent-workflow-heavy-test.lock"
TIMEOUT="${HEAVY_TEST_LOCK_TIMEOUT:-1800}"
POLL_INTERVAL=0.2

# Set before the child is spawned so that, under `set -u`, a signal arriving
# before "child=$!" runs can still safely reference (an empty) $child in the
# INT/TERM trap instead of aborting the trap on an unbound variable.
child=""

if [ "$#" -eq 0 ]; then
  echo "usage: heavy-test-lock.sh <command> [args...]" >&2
  exit 2
fi

# Acquires $LOCK, recording this process's pid + pgid on success. Returns 0
# once acquired, or 1 if $TIMEOUT seconds elapse first (never blocks past
# that — the caller degrades to running unlocked).
acquire_lock() {
  local start=$SECONDS
  local pid pgid recorded

  while true; do
    if mkdir "$LOCK" 2>/dev/null; then
      # Acquired. Record identity immediately, then re-read it back.
      #
      # ponytail: this re-read narrows, but cannot fully close, a
      # single-statement race — a waiter could see an empty pid file (ours,
      # not yet written), judge us stale, and reclaim between our `mkdir`
      # and this write. The re-check catches the common ordering (a
      # reclaimer's write lands before ours, so we see THEIR pid here and
      # correctly retry); it cannot catch a reclaimer that also finishes
      # its own re-check before we resume from a deep preemption. Not
      # exercised by this suite's real concurrency, which only ever
      # contends over genuinely dead pids. A fully airtight fix needs a
      # primitive `mkdir` doesn't have (e.g. flock, unavailable on macOS).
      echo "$$" > "$LOCK/pid" 2>/dev/null
      pgid=$(ps -o pgid= -p "$$" 2>/dev/null | tr -d ' ')
      echo "$pgid" > "$LOCK/pgid" 2>/dev/null
      recorded=$(cat "$LOCK/pid" 2>/dev/null || true)
      if [ "$recorded" = "$$" ]; then
        return 0
      fi
      continue # lost the race to a reclaimer; retry from the top
    fi

    pid=$(cat "$LOCK/pid" 2>/dev/null || true)
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      # Stale holder (dead pid, or a lock dir with no pid file at all).
      #
      # TOCTOU-safe reclaim (S2): rename-then-delete, NOT rm-then-mkdir.
      # `mv` on a shared source path can succeed for at most one concurrent
      # waiter (the source stops existing the instant it succeeds); every
      # other waiter's `mv` fails and falls through to retry `mkdir`, which
      # correctly finds the path either still taken (another waiter won) or
      # free (the winner hasn't recreated it yet) — either way, no two
      # waiters ever both believe they hold the lock from the same stale
      # dir. A plain `rm -rf "$LOCK"; mkdir "$LOCK"` would let every
      # waiter's `rm` succeed independently, and every one of them would
      # then `mkdir` successfully too.
      mv "$LOCK" "$LOCK.stale.$$" 2>/dev/null && rm -rf "$LOCK.stale.$$"
      continue
    fi

    # Held by a live pid — this is ordinary contention, not staleness.
    if [ $((SECONDS - start)) -ge "$TIMEOUT" ]; then
      return 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# Runs "$@" in the background, waits for it, and exits this script with its
# exit code. Never `exec`s (S1): exec would replace this shell, so the
# EXIT trap that releases the lock would never fire.
run_command() {
  "$@" &
  child=$!
  wait "$child"
  exit $?
}

if acquire_lock; then
  # Traps are registered only AFTER a successful acquire — a run that never
  # held the lock (see the timeout branch below) must never remove a lock
  # dir, since that dir could belong to someone else entirely.
  trap 'rm -rf "$LOCK"' EXIT
  # A signal must also stop the still-running wrapped command — otherwise
  # the lock frees for the next waiter while our own child keeps running
  # unlocked, exactly recreating the pile-up this lock exists to prevent.
  trap 'kill "$child" 2>/dev/null; rm -rf "$LOCK"; exit 143' INT TERM
  run_command "$@"
else
  echo "heavy-test-lock: could not acquire within ${TIMEOUT}s; running unlocked" >&2
  run_command "$@"
fi
