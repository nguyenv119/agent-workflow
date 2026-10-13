"""Tests for `agent-cli lock acquire|release|status`, run as real subprocesses against a temp state dir."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CLEAN, agent_cli, make_project


class Lock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-lock-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = make_project(self.tmp / "proj")
        self.state = self.tmp / "state"

    def live(self):
        """The pid of a live process, killed at cleanup."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc.pid

    def dead(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def lock(self, *args):
        return agent_cli(self.p, "lock", *args, env={"AGENT_CLI_STATE": str(self.state)})

    def owner(self, name):
        return json.loads((self.state / "locks" / name / "owner.json").read_text())

    def test_acquire_status_release_round_trip(self):
        """acquire writes owner.json, status shows it alive, release by the owner removes it."""
        # GIVEN a live owner
        pid = self.live()
        # WHEN it acquires, status is read, and it releases
        a = self.lock("acquire", "gates", "--pid", str(pid), "--cap", "120m")
        s = self.lock("status", "--json")
        r = self.lock("release", "gates", "--pid", str(pid))
        # THEN each step succeeds and the lock is gone
        self.assertEqual((a.returncode, a.stdout), (0, f"acquired: gates (pid {pid})\n"), a.stderr)
        row = json.loads(s.stdout)
        self.assertEqual((row["name"], row["pid"], row["alive"], row["stale"], row["cap_s"]),
                         ("gates", pid, True, False, 7200))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("released: gates"))
        self.assertFalse((self.state / "locks/gates").exists())

    def test_pid_zero_is_refused(self):
        """--pid 0 is not a process: acquire must not record it and release must not drop a lock whose
        corrupt owner.json reads as pid 0; both refuse (2) and write nothing."""
        for verb in ("acquire", "release"):
            # GIVEN no locks
            # WHEN acquire or release runs with --pid 0
            r = self.lock(verb, "gates", "--pid", "0")
            # THEN refused, and no lock written
            with self.subTest(verb=verb):
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("--pid 0 is not a process id", r.stderr)
                self.assertFalse((self.state / "locks/gates").exists())
        self.assertEqual(self.lock("status").stdout, "no locks\n")

    def test_same_pid_acquire_is_already_held(self):
        """R4: acquiring a lock you already hold is a no-op, exit 0."""
        # GIVEN a held lock
        pid = self.live()
        self.lock("acquire", "gates", "--pid", str(pid))
        started = self.owner("gates")["started"]
        # WHEN the same pid acquires again
        r = self.lock("acquire", "gates", "--pid", str(pid))
        # THEN it says already held and leaves the owner untouched
        self.assertEqual((r.returncode, r.stdout), (0, f"already held: gates (pid {pid})\n"))
        self.assertEqual(self.owner("gates")["started"], started)

    def test_second_live_pid_is_blocked_with_3_naming_the_holder(self):
        """Another live process cannot take a held lock: exit 3, the holder named, an example given."""
        # GIVEN a lock held by one live pid
        holder, other = self.live(), self.live()
        self.lock("acquire", "gates", "--pid", str(holder))
        # WHEN another live pid tries with --wait 1s
        t = time.monotonic()
        r = self.lock("acquire", "gates", "--pid", str(other), "--wait", "1")
        # THEN it waited, then exit 3 naming the holder; the lock is unchanged
        self.assertGreaterEqual(time.monotonic() - t, 1)
        self.assertEqual(r.returncode, 3)
        self.assertIn(f"error: gates held by pid {holder} on ", r.stderr)
        self.assertIn("example: agent-cli lock acquire gates --pid $$ --wait 10m", r.stderr)
        self.assertEqual(self.owner("gates")["pid"], holder)

    def test_dead_holder_is_reaped(self):
        """A lock whose holder pid is gone is reaped and taken, printing the old owner."""
        # GIVEN a lock held by a pid that has exited
        gone, me = self.dead(), self.live()
        self.lock("acquire", "gates", "--pid", str(gone))
        # WHEN a live pid acquires
        r = self.lock("acquire", "gates", "--pid", str(me))
        # THEN it reaps the dead owner and holds the lock
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, rf"^reaped: pid {gone} on .*\nacquired: gates \(pid {me}\)\n$")
        self.assertEqual(self.owner("gates")["pid"], me)

    def test_past_cap_holder_is_reaped_even_if_alive(self):
        """A live holder past its cap is reaped: a hung lane must not hold a resource forever."""
        # GIVEN a live holder whose lock started 3 minutes ago with a 1-minute cap
        holder, me = self.live(), self.live()
        self.lock("acquire", "gates", "--pid", str(holder), "--cap", "1m")
        f = self.state / "locks/gates/owner.json"
        f.write_text(json.dumps(dict(self.owner("gates"), started=time.time() - 180)))
        # WHEN another pid acquires (dry run first, then for real)
        dry = self.lock("acquire", "gates", "--pid", str(me), "--dry-run", "--json")
        r = self.lock("acquire", "gates", "--pid", str(me), "--json")
        # THEN the dry run changes nothing and the real one reaps it
        self.assertEqual(json.loads(dry.stdout)["status"], "would-acquire")
        out = json.loads(r.stdout)
        self.assertEqual((r.returncode, out["status"]), (0, "acquired"))
        self.assertIn(f"pid {holder}", out["reaped"])
        self.assertEqual(self.owner("gates")["pid"], me)

    def test_other_host_holder_is_not_reaped_while_in_cap(self):
        """A pid on another host cannot be checked, so its lock holds until its cap."""
        # GIVEN a lock owned by a dead-looking pid on another host
        self.lock("acquire", "gates", "--pid", str(self.dead()))
        f = self.state / "locks/gates/owner.json"
        f.write_text(json.dumps(dict(self.owner("gates"), host="other-" + socket.gethostname())))
        # WHEN this host acquires
        r = self.lock("acquire", "gates", "--pid", str(self.live()))
        # THEN it is blocked
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_release_by_non_owner_is_refused_without_force(self):
        """Only the owner releases; anyone else gets exit 2 unless --force, and not-held is already done."""
        # GIVEN a lock held by one live pid
        holder, other = self.live(), self.live()
        self.lock("acquire", "gates", "--pid", str(holder))
        # WHEN another pid releases without and then with --force, then once more
        refused = self.lock("release", "gates", "--pid", str(other))
        forced = self.lock("release", "gates", "--pid", str(other), "--force")
        again = self.lock("release", "gates", "--pid", str(other))
        # THEN refused (2), released, then already done (0)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("example: agent-cli lock release gates --pid $$ --force", refused.stderr)
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual((again.returncode, again.stdout), (0, "already done: gates is not held\n"))

    def test_status_of_a_lock_not_held(self):
        """status <name> --json on a free lock is one not-held object, so scripts can parse every answer."""
        # GIVEN no locks
        # WHEN one is asked about
        r = self.lock("status", "gates", "--json")
        # THEN it says not-held
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, {"name": "gates", "status": "not-held"}))

    def test_pid_is_required(self):
        """Without --pid the owner would be the one-shot calling shell, reaped on the next acquire: refuse (2)."""
        for verb in ("acquire", "release"):
            # GIVEN no --pid
            # WHEN acquire or release runs
            r = self.lock(verb, "gates")
            # THEN refused with an example that passes --pid, and nothing written
            with self.subTest(verb=verb):
                self.assertEqual(r.returncode, 2)
                self.assertIn("required: --pid", r.stderr)
                self.assertRegex(r.stderr, r"example: .*agent-cli lock " + verb + r" gates --pid \$\$")
        self.assertFalse((self.state / "locks/gates").exists())

    def race(self, n):
        """Start n acquires of `race` for n distinct live pids at once; returns [(pid, CompletedProcess-like)]."""
        env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        env["AGENT_CLI_STATE"] = str(self.state)
        pids = [self.live() for _ in range(n)]
        procs = [(pid, subprocess.Popen([sys.executable, str(self.p / ".claude/bin/agent-cli"), "lock", "acquire",
                                         "race", "--pid", str(pid), "--wait", "0"], env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)) for pid in pids]
        return [(pid, p.returncode, out, err) for pid, p in procs for out, err in [p.communicate(timeout=30)]]

    def test_simultaneous_acquires_have_exactly_one_winner(self):
        """Five processes racing for a free lock: one exits 0 and owns it, four exit 3."""
        # GIVEN a free lock
        # WHEN five live pids acquire it at the same moment
        res = self.race(5)
        # THEN exactly one wins, and owner.json names it
        self.assertEqual(sorted(r[1] for r in res), [0, 3, 3, 3, 3], res)
        winner = next(pid for pid, code, _, _ in res if code == 0)
        self.assertEqual(self.owner("race")["pid"], winner)

    def test_racing_reapers_reap_once(self):
        """Five processes racing for a dead holder's lock: one reaped: line, one winner."""
        # GIVEN a lock held by a pid that has exited
        self.lock("acquire", "race", "--pid", str(self.dead()))
        # WHEN five live pids acquire it at the same moment
        res = self.race(5)
        # THEN exactly one reaps and wins
        self.assertEqual(sorted(r[1] for r in res), [0, 3, 3, 3, 3], res)
        self.assertEqual(sum(out.count("reaped:") for _, _, out, _ in res), 1, res)

    def test_bad_name_is_refused(self):
        """A lock name is one path segment; ../x would escape the state dir."""
        # GIVEN a path-like name
        # WHEN acquired
        r = self.lock("acquire", "../x", "--pid", str(self.live()))
        # THEN refused with exit 2 and nothing written outside the locks dir
        self.assertEqual(r.returncode, 2)
        self.assertIn("bad lock name", r.stderr)
        self.assertFalse((self.state / "x").exists())


if __name__ == "__main__":
    unittest.main()
