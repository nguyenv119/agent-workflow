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

    def lock(self, *args, env=None):
        return agent_cli(self.p, "lock", *args, env={"AGENT_CLI_STATE": str(self.state),
                                                     "AGENT_CLI_TSC_PATTERN": self.TSC, **(env or {})})

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

    # Burst slots: tests never read the real box; the load and memory readings are planted through env,
    # the way TSC is. LOW is a quiet machine, so only the reading a test changes can refuse.
    LOW = {"AGENT_CLI_GATES_BURST": "1", "AGENT_CLI_LOADAVG": "0", "AGENT_CLI_MEM_AVAILABLE_GB": "64"}

    def age(self, name, seconds):
        """Rewrite the lock's start time to `seconds` ago."""
        f = self.state / "locks" / name / "owner.json"
        f.write_text(json.dumps(dict(self.owner(name), started=time.time() - seconds)))

    def fill_base(self, n=3, age=120):
        """n live pids in gates.1..n (--slots n), each started `age` s ago; their pids."""
        pids = [self.live() for _ in range(n)]
        for i, p in enumerate(pids, 1):
            r = self.lock("acquire", "gates", "--slots", str(n), "--pid", str(p))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.age(f"gates.{i}", age)
        return pids

    def burst(self, pid, env=None, *more):
        return self.lock("acquire", "gates", "--slots", "3", "--pid", str(pid), *more, env={**self.LOW, **(env or {})})

    def test_burst_off_by_default_keeps_the_fixed_semaphore(self):
        """Without AGENT_CLI_GATES_BURST a full gates lock refuses the fourth holder, load or no load."""
        # GIVEN three base slots held for two minutes and a quiet machine, burst env unset
        self.fill_base()
        # WHEN a fourth pid acquires
        r = self.lock("acquire", "gates", "--slots", "3", "--pid", str(self.live()),
                      env={"AGENT_CLI_LOADAVG": "0", "AGENT_CLI_MEM_AVAILABLE_GB": "64"})
        # THEN exit 3 as before, no burst clause, no gates.4
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("all 3 slots of gates held by", r.stderr)
        self.assertNotIn("burst", r.stderr)
        self.assertFalse((self.state / "locks/gates.4").exists())

    def test_burst_prefers_a_free_base_slot(self):
        """With burst on and a base slot free, the base slot is taken, not a burst one."""
        # GIVEN two of three base slots held
        self.fill_base(2)
        # WHEN a third pid acquires with burst on
        r = self.burst(self.live())
        # THEN it gets gates.3, plain output
        self.assertRegex(r.stdout, r"^acquired: gates\.3 \(pid \d+\)\n$")
        self.assertFalse((self.state / "locks/gates.4").exists())

    def test_burst_slot_taken_when_base_full_and_machine_quiet(self):
        """Base full, low load, enough memory, newest slot 2 min old: gates.4 is taken and marked burst."""
        # GIVEN three base slots held for two minutes
        self.fill_base()
        # WHEN a fourth pid acquires in text and a fifth in --json
        me = self.live()
        r = self.burst(me)
        self.age("gates.4", 120)
        j = self.burst(self.live(), None, "--json")
        # THEN the fourth got gates.4 (burst); the fifth finds the one burst slot taken
        self.assertEqual((r.returncode, r.stdout), (0, f"acquired: gates.4 (pid {me}) (burst)\n"), r.stderr)
        self.assertEqual(self.owner("gates.4")["pid"], me)
        self.assertEqual(j.returncode, 3, j.stdout)
        self.assertIn("burst refused: all 1 burst slots held", j.stderr)

    def test_burst_json_says_burst_true(self):
        """--json carries burst: true for a burst slot and false for a base slot."""
        # GIVEN base full
        self.fill_base()
        # WHEN a burst slot is taken, and after release a base slot
        me = self.live()
        b = json.loads(self.burst(me, None, "--json").stdout)
        self.lock("release", "gates", "--pid", str(self.owner("gates.1")["pid"]))
        base = json.loads(self.burst(self.live(), None, "--json").stdout)
        # THEN the flag matches
        self.assertEqual((b["name"], b["burst"]), ("gates.4", True))
        self.assertEqual((base["name"], base["burst"]), ("gates.1", False))

    def test_burst_refused_by_high_load(self):
        """Load over the threshold refuses the burst and the exit-3 message says so in one clause."""
        # GIVEN base full and a busy machine (load far over the cores)
        self.fill_base()
        # WHEN a fourth acquires
        r = self.burst(self.live(), {"AGENT_CLI_LOADAVG": "1000"})
        # THEN exit 3 with the load clause, nothing written
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertRegex(r.stderr, r"all 3 slots of gates held by .*; burst refused: load [\d.]+ of cores >= 0\.75")
        self.assertFalse((self.state / "locks/gates.4").exists())

    def test_burst_max_load_env_overrides_the_threshold(self):
        """AGENT_CLI_BURST_MAX_LOAD=0 means no load is low enough; 5 lets a heavy-looking reading through."""
        # GIVEN base full, load 0
        self.fill_base()
        # WHEN threshold 0 then threshold 1000 (load 1)
        no = self.burst(self.live(), {"AGENT_CLI_BURST_MAX_LOAD": "0"})
        yes = self.burst(self.live(), {"AGENT_CLI_BURST_MAX_LOAD": "1000", "AGENT_CLI_LOADAVG": "1"})
        # THEN refused, then taken
        self.assertEqual(no.returncode, 3, no.stdout)
        self.assertIn("burst refused: load 0.00 of cores >= 0", no.stderr)
        self.assertEqual(yes.returncode, 0, yes.stderr)

    def test_burst_refused_by_low_memory(self):
        """MemAvailable under 10 GB refuses; AGENT_CLI_BURST_MIN_FREE_GB moves the line."""
        # GIVEN base full, 4 GB available
        self.fill_base()
        # WHEN default line, then a 2 GB line
        no = self.burst(self.live(), {"AGENT_CLI_MEM_AVAILABLE_GB": "4"})
        yes = self.burst(self.live(), {"AGENT_CLI_MEM_AVAILABLE_GB": "4", "AGENT_CLI_BURST_MIN_FREE_GB": "2"})
        # THEN refused with the memory clause, then taken
        self.assertEqual(no.returncode, 3, no.stdout)
        self.assertIn("burst refused: 4.0 GB memory available < 10 GB", no.stderr)
        self.assertEqual(yes.returncode, 0, yes.stderr)

    def test_burst_refused_when_newest_slot_is_too_new(self):
        """A slot started 5 s ago (load average has not caught up) refuses; AGENT_CLI_BURST_SPACING_S=1 lets it in."""
        # GIVEN base full, slot 3 started 5 s ago
        self.fill_base()
        self.age("gates.3", 5)
        # WHEN default spacing, then 1 s spacing
        no = self.burst(self.live())
        yes = self.burst(self.live(), {"AGENT_CLI_BURST_SPACING_S": "1"})
        # THEN refused naming the age, then taken
        self.assertEqual(no.returncode, 3, no.stdout)
        self.assertRegex(no.stderr, r"burst refused: newest slot started \d+s ago < 60s")
        self.assertEqual(yes.returncode, 0, yes.stderr)

    def test_burst_spacing_counts_the_newest_burst_slot_too(self):
        """With two burst slots, a burst slot taken 5 s ago blocks the next one though all base slots are old."""
        # GIVEN base full (old), gates.4 taken just now, B=2
        self.fill_base()
        env = {"AGENT_CLI_GATES_BURST": "2"}
        first = self.burst(self.live(), env)
        # WHEN another pid asks for gates.5 straight away, then after gates.4 is 2 min old
        soon = self.burst(self.live(), env)
        self.age("gates.4", 120)
        later = self.burst(self.live(), env)
        # THEN the first is refused for spacing, the later one gets gates.5
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(soon.returncode, 3, soon.stdout)
        self.assertIn("burst refused: newest slot started", soon.stderr)
        self.assertRegex(later.stdout, r"^acquired: gates\.5 \(pid \d+\) \(burst\)\n$")

    def test_burst_refused_when_type_checkers_fill_n_plus_b(self):
        """Running tsc count must stay under N+B: 3 tsc leave room for a burst slot (N=3, B=1), 4 do not."""
        # GIVEN base full and three tsc running
        self.fill_base()
        tsc = self.fake_tsc(3)
        # WHEN a fourth tries, then a fourth tsc appears
        ok = self.burst(self.live())
        self.lock("release", "gates", "--pid", str(self.owner("gates.4")["pid"]))
        self.fake_tsc(1)
        no = self.burst(self.live())
        # THEN 3 tsc allow it, 4 refuse with the type-checker clause
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(no.returncode, 3, no.stdout)
        self.assertIn("burst refused: 4 type checkers running >= 4", no.stderr)

    def test_no_burst_flag_ignores_the_env(self):
        """--no-burst keeps a full-suite run at N slots even with the env on and the machine quiet."""
        # GIVEN base full and a quiet machine, burst env on
        self.fill_base()
        # WHEN a fourth acquires with --no-burst
        r = self.burst(self.live(), None, "--no-burst")
        # THEN exit 3 without a burst clause
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertNotIn("burst", r.stderr)

    def test_burst_env_applies_only_to_the_gates_lock(self):
        """Another lock name never bursts: the env is read for names in TSC_GATED only."""
        # GIVEN a held 1-slot lock "other" and burst env on
        self.lock("acquire", "other", "--pid", str(self.live()))
        # WHEN another pid asks for it
        r = self.lock("acquire", "other", "--pid", str(self.live()), env=self.LOW)
        # THEN blocked
        self.assertEqual(r.returncode, 3, r.stdout)

    def test_bad_burst_env_is_refused(self):
        """A non-numeric AGENT_CLI_GATES_BURST is exit 2 naming the variable, not silently off."""
        r = self.lock("acquire", "gates", "--pid", str(self.live()), env={"AGENT_CLI_GATES_BURST": "lots"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("AGENT_CLI_GATES_BURST", r.stderr)
        self.assertFalse((self.state / "locks").exists() and any((self.state / "locks").glob("gates*")))

    def test_burst_slot_shows_in_status_and_releases_by_owner(self):
        """status lists gates.4 with its owner; release gates --pid <burst pid> removes only that slot."""
        # GIVEN a burst slot held
        self.fill_base()
        me = self.live()
        self.burst(me)
        # WHEN status and then release by the burst owner run
        s = self.lock("status", "--json")
        rel = self.lock("release", "gates", "--pid", str(me))
        # THEN the slot is listed, released (the message names the lock as given), base slots stay
        rows = {json.loads(line)["name"]: json.loads(line) for line in s.stdout.splitlines()}
        self.assertEqual(rows["gates.4"]["pid"], me)
        self.assertEqual(rel.returncode, 0, rel.stderr)
        self.assertTrue(rel.stdout.startswith("released: gates "), rel.stdout)
        self.assertFalse((self.state / "locks/gates.4").exists())
        self.assertTrue((self.state / "locks/gates.1").exists())

    def test_dead_burst_slot_is_reaped_and_retaken(self):
        """A burst slot whose holder died is reaped by the next burst acquire, printing the old owner."""
        # GIVEN base full and gates.4 held by a pid that has exited, started 2 min ago
        self.fill_base()
        gone = self.dead()
        self.burst(gone)
        self.age("gates.4", 120)
        # WHEN a live pid bursts
        me = self.live()
        r = self.burst(me)
        # THEN it reaps gates.4 and holds it
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, rf"^reaped: pid {gone} on .*\nacquired: gates\.4 \(pid {me}\) \(burst\)\n$")
        self.assertEqual(self.owner("gates.4")["pid"], me)

    def test_dead_burst_holder_does_not_count_for_spacing(self):
        """A dead burst holder started 1 s ago is stale, so it must not make the newest-slot check refuse."""
        # GIVEN base full (old), gates.4 held by a dead pid just now
        self.fill_base()
        self.burst(self.dead())
        # WHEN a live pid bursts
        r = self.burst(self.live())
        # THEN it is admitted
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
