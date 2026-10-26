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

    # Tests never see the laptop's real tsc processes: the pattern points at a path only fake_tsc plants.
    TSC = "agent-cli-test-planted/bin/tsc"

    def lock(self, *args):
        return agent_cli(self.p, "lock", *args, env={"AGENT_CLI_STATE": str(self.state),
                                                     "AGENT_CLI_TSC_PATTERN": self.TSC})

    def fake_tsc(self, n=1):
        """n live processes whose command line ends in a path matching TSC, killed at cleanup; their pids."""
        procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/x/{self.TSC}"])
                 for _ in range(n)]
        for proc in procs:
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        time.sleep(0.3)  # let each exec so ps shows its command line
        return [proc.pid for proc in procs]

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
        """cli-for-agents "Idempotency": acquiring a lock you already hold is a no-op, exit 0."""
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

    def test_slots_let_n_holders_in_and_block_the_next(self):
        """--slots 3: three live pids each take a slot (gates.1..3), a fourth gets exit 3 naming all three,
        and release by one owner frees exactly its slot for the fourth."""
        # GIVEN three live pids holding the three slots
        pids = [self.live() for _ in range(3)]
        got = [self.lock("acquire", "gates", "--slots", "3", "--pid", str(p)) for p in pids]
        # WHEN a fourth tries, then the second owner releases, then the fourth tries again
        fourth = self.live()
        blocked = self.lock("acquire", "gates", "--slots", "3", "--pid", str(fourth))
        rel = self.lock("release", "gates", "--pid", str(pids[1]))
        again = self.lock("acquire", "gates", "--slots", "3", "--pid", str(fourth))
        # THEN slots fill in order, the fourth waits, and it takes the freed slot 2
        self.assertEqual([(r.returncode, r.stdout) for r in got],
                         [(0, f"acquired: gates.{i} (pid {p})\n") for i, p in enumerate(pids, 1)])
        self.assertEqual(blocked.returncode, 3, blocked.stderr)
        self.assertIn("all 3 slots of gates held by", blocked.stderr)
        for p in pids:
            self.assertIn(f"pid {p} on", blocked.stderr)
        self.assertEqual(rel.returncode, 0, rel.stderr)
        self.assertEqual(again.stdout, f"acquired: gates.2 (pid {fourth})\n", again.stderr)
        self.assertEqual([self.owner(f"gates.{i}")["pid"] for i in (1, 2, 3)], [pids[0], fourth, pids[2]])

    def test_slots_reap_a_dead_holder_and_same_pid_is_already_held(self):
        """A slot whose holder died is reaped by the next acquire; a pid already in a slot gets
        already-held for that slot instead of a second one."""
        # GIVEN slot 1 held by a live pid and slot 2 by a dead one
        live = self.live()
        self.lock("acquire", "gates", "--slots", "2", "--pid", str(live))
        self.lock("acquire", "gates", "--slots", "2", "--pid", str(self.dead()))
        # WHEN a new pid acquires, and the live holder acquires again
        new = self.live()
        r = self.lock("acquire", "gates", "--slots", "2", "--pid", str(new))
        same = self.lock("acquire", "gates", "--slots", "2", "--pid", str(live))
        # THEN the dead slot is reaped and taken; the live holder keeps its one slot
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reaped:", r.stdout)
        self.assertIn(f"acquired: gates.2 (pid {new})", r.stdout)
        self.assertEqual(same.stdout, f"already held: gates.1 (pid {live})\n", same.stderr)

    def test_slots_zero_is_refused(self):
        """--slots 0 would mean nobody may ever hold it: refused, nothing written."""
        r = self.lock("acquire", "gates", "--slots", "0", "--pid", str(self.live()))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("--slots 0", r.stderr)
        self.assertEqual(self.lock("status").stdout, "no locks\n")

    def test_running_tsc_counts_as_busy_even_when_its_holder_died(self):
        """gates.1's owner died but its tsc children run on: with --slots 2, two such tsc fill the lock
        (exit 3, naming the tsc pids), one does not, and when they are gone the dead slot is reaped."""
        # GIVEN slot 1 held by a dead pid and two tsc processes still running
        self.lock("acquire", "gates", "--slots", "2", "--pid", str(self.dead()))
        tsc = self.fake_tsc(2)
        # WHEN a new pid tries to acquire, with the count at 2 and then at 1
        new = self.live()
        full = self.lock("acquire", "gates", "--slots", "2", "--pid", str(new))
        os.kill(tsc[0], 9)
        time.sleep(0.3)
        one = self.lock("acquire", "gates", "--slots", "2", "--pid", str(new))
        # THEN two tsc block it and name them; one tsc leaves a slot, so the dead slot is reaped and taken
        self.assertEqual(full.returncode, 3, full.stderr)
        self.assertIn("all 2 slots of gates held by", full.stderr)
        self.assertIn(f"tsc pid {tsc[0]}", full.stderr)
        self.assertIn(f"tsc pid {tsc[1]}", full.stderr)
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertIn(f"acquired: gates.1 (pid {new})", one.stdout)

    def test_tsc_count_does_not_touch_other_lock_names_or_free_gates(self):
        """Only the gates lock counts tsc: a lock with any other name ignores running type checkers."""
        # GIVEN three tsc processes running
        self.fake_tsc(3)
        # WHEN a plain lock named "other" is acquired
        other = self.lock("acquire", "other", "--pid", str(self.live()))
        # THEN the plain lock is taken
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_tsserver_and_unrelated_commands_are_not_tsc(self):
        """A command line that only mentions the path in one token ending elsewhere (tsserver, an editor
        with the path as a substring) does not count."""
        # GIVEN a process whose argument contains the path but does not end with it
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/x/{self.TSC}/tsserver.js"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.3)
        # WHEN gates --slots 1 is acquired
        r = self.lock("acquire", "gates", "--pid", str(self.live()))
        # THEN it is not counted as a type checker
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
