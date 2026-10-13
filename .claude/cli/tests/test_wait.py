"""Tests for `agent-cli wait`: real `bash -c` status commands, a fake clock for the interval and cap."""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import CLAUDE, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import wait  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def run_wait(until, every="5", cap="20s"):
    """(exit code, JSON rows, fake clock) for one in-process wait."""
    clock, buf = FakeClock(), io.StringIO()
    a = argparse.Namespace(until=until, every=every, cap=cap, json=True)
    with redirect_stdout(buf):
        code = wait.wait(a, clock=clock, sleep=clock.sleep)
    return code, [json.loads(ln) for ln in buf.getvalue().splitlines()], clock


class Wait(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-wait-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_stops_with_0_once_the_command_succeeds(self):
        """Exit 0 from the status command ends the wait with exit 0 and result met, one row per attempt."""
        # GIVEN a command that fails twice, then succeeds (a counter file)
        f = self.tmp / "n"
        cmd = f'n=$(cat {f} 2>/dev/null || echo 0); echo $((n+1)) > {f}; echo "try $n"; [ "$n" -ge 2 ]'
        # WHEN waited on every 5s
        code, rows, clock = run_wait(cmd)
        # THEN it stops on attempt 3 at 10s with the last stdout line
        self.assertEqual(code, 0)
        self.assertEqual([r["exit"] for r in rows], [1, 1, 0])
        self.assertEqual(rows[-1], {"attempt": 3, "exit": 0, "elapsed_s": 10.0, "last": "try 2", "result": "met"})
        self.assertNotIn("result", rows[0])
        self.assertEqual(clock.sleeps, [5, 5])

    def test_refused_and_blocked_pass_through_at_once(self):
        """Exit 2 or 3 (refused / blocked under R8) stops on the first attempt with that same code."""
        for status, result in ((2, "refused"), (3, "blocked")):
            # GIVEN a command that refuses or blocks
            # WHEN waited on
            code, rows, clock = run_wait(f"exit {status}")
            # THEN the code passes through after one attempt, no sleep
            with self.subTest(status=status):
                self.assertEqual(code, status)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["result"], result)
                self.assertEqual(clock.sleeps, [])

    def test_cap_reached_exits_4(self):
        """A command that never succeeds ends with exit 4 once the cap passes; the last sleep is cut to the cap."""
        # GIVEN a failing command, every 5s, cap 12s
        # WHEN waited on
        code, rows, clock = run_wait("exit 1", cap="12s")
        # THEN attempts at 0, 5, 10, 12, then a timeout row
        self.assertEqual(code, 4)
        self.assertEqual([r["elapsed_s"] for r in rows[:-1]], [0.0, 5.0, 10.0, 12.0])
        self.assertEqual(rows[-1], {"attempts": 4, "elapsed_s": 12.0, "result": "timeout"})
        self.assertEqual(clock.sleeps, [5, 5, 2])

    def test_missing_command_is_refused_at_once(self):
        """126/127 (not executable / not found) never fix themselves: refused (2) on the first attempt."""
        # GIVEN a command that does not exist
        # WHEN waited on
        code, rows, _ = run_wait("no-such-command-xyz")
        # THEN refused after one attempt, the shell's exit kept in the row
        self.assertEqual(code, 2)
        self.assertEqual((len(rows), rows[0]["exit"], rows[0]["result"]), (1, 127, "refused"))

    def test_only_the_tail_of_attempt_output_is_kept(self):
        """A chatty status command keeps its last line without holding all its output."""
        # GIVEN a command printing ~1 MB, then a last line
        # WHEN waited on
        code, rows, _ = run_wait("yes x | head -c 1000000; echo; echo final; exit 0")
        # THEN the last line survives
        self.assertEqual((code, rows[0]["last"]), (0, "final"))

    def test_slow_command_is_killed_at_the_cap(self):
        """A hung status command cannot outlive the cap: real sleeps with --cap 3s exit 4 in about 3s, and the
        whole process group dies, including a backgrounded child, so no orphan keeps running."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN waiting on a command slower than the cap (odd durations: pgrep never matches other sleeps)
        t = time.monotonic()
        r = agent_cli(p, "wait", "--until", "sleep 30.0417 & sleep 31.0417", "--every", "5", "--cap", "3s", "--json")
        took = time.monotonic() - t
        # THEN exit 4 within ~5s; the attempt is marked timed out
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertLess(took, 6)
        rows = [json.loads(ln) for ln in r.stdout.splitlines()]
        self.assertEqual((rows[0]["exit"], rows[0]["timed_out"]), (None, True))
        self.assertEqual(rows[-1]["result"], "timeout")
        left = subprocess.run(["pgrep", "-f", "sleep 3[01]\\.0417"], capture_output=True, text=True).stdout
        self.assertEqual(left, "", "a sleep from the timed-out attempt is still running")

    def test_every_under_5s_is_refused_with_an_example(self):
        """R3: --every 2 is refused (exit 2) before running anything, with a correct invocation."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN --every is below the minimum, and when a duration is malformed
        low = agent_cli(p, "wait", "--until", "true", "--every", "2")
        bad = agent_cli(p, "wait", "--until", "true", "--cap", "soon")
        # THEN both exit 2 naming the problem and an example
        for r, msg in ((low, "under the 5s minimum"), (bad, "bad duration")):
            self.assertEqual(r.returncode, 2)
            self.assertIn(msg, r.stderr)
            self.assertIn("example: agent-cli wait --until", r.stderr)

    def test_cli_prints_one_text_line_per_attempt(self):
        """Through the real CLI, without --json, each attempt is one key=value line."""
        # GIVEN an installed project
        p = make_project(self.tmp / "proj")
        # WHEN waiting on a command that succeeds at once
        r = agent_cli(p, "wait", "--until", "echo ready", "--cap", "10s")
        # THEN one line, exit 0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"^attempt=1 exit=0 elapsed_s=\d+\.\d last=ready result=met\n$")


if __name__ == "__main__":
    unittest.main()
