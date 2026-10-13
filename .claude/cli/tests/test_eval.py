"""Tests for `agent-cli eval init|run|controls`: tiny real eval scripts run through the installed CLI."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import CLAUDE, GIT, agent_cli, make_project, run

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import hook_rules  # noqa: E402

SENTINEL = "<promise>WIN</promise>"
LIB_SH = 'source "$(agent-cli eval lib --sh)"\n'


class EvalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-cli-eval-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.p = make_project(self.tmp / "proj")

    def eval_dir(self, body, name="win.sh", where="ev"):
        """An eval dir holding one entry script with body; returns the dir."""
        d = self.tmp / where
        d.mkdir(exist_ok=True)
        (d / name).write_text(body)
        return d

    def cli(self, *args, **kw):
        return agent_cli(self.p, *args, **kw)

    def run_json(self, d, *extra, **kw):
        r = self.cli("eval", "run", str(d), "--json", *extra, **kw)
        return r, json.loads(r.stdout.splitlines()[-1])


class Run(EvalCase):
    def test_win_needs_exit_0_and_the_sentinel_last_and_writes_evidence(self):
        """WIN only when the script exits 0 AND its last stdout line is the sentinel; evidence records the run.

        The loop stops on this verdict, so it must match the win-condition skill's dual condition exactly,
        and every run must leave a log plus a JSON record a reviewer can check later.
        """
        # GIVEN an eval that passes a check and calls win
        d = self.eval_dir(LIB_SH + 'ok "rows landed: 3"\necho "to stderr" >&2\nwin\n')
        # WHEN run
        r, out = self.run_json(d)
        # THEN exit 0, verdict WIN, and evidence json + log exist with the run's facts
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["verdict"], "WIN")
        ev = json.loads(Path(out["evidence"]).read_text())
        self.assertEqual({k: ev[k] for k in ("exit", "last_line", "verdict")}, {"exit": 0, "last_line": SENTINEL,
                                                                                "verdict": "WIN"})
        self.assertEqual(len(ev["entry_sha256"]), 64)
        self.assertIsInstance(ev["duration_s"], float)
        log = Path(out["evidence"]).with_suffix(".log").read_text()
        self.assertIn("ok: rows landed: 3", log)
        self.assertIn("to stderr", log)
        self.assertEqual(Path(out["evidence"]).parent, d / "evidence")

    def test_exit_0_without_the_sentinel_is_no_sentinel_exit_1(self):
        """A script that exits 0 but never printed WIN (it stopped early) is not a win."""
        # GIVEN an eval that exits 0 silently after one check
        d = self.eval_dir('echo "checked one thing"\nexit 0\n')
        # WHEN run
        r, out = self.run_json(d)
        # THEN NO_SENTINEL and exit 1
        self.assertEqual((r.returncode, out["verdict"]), (1, "NO_SENTINEL"))

    def test_sentinel_not_on_the_last_line_is_no_sentinel(self):
        """The sentinel must be the last non-empty stdout line; output after it means the eval kept going."""
        # GIVEN an eval printing the sentinel, then more output, exit 0
        d = self.eval_dir(f"printf '%s\\n' '{SENTINEL}'\necho 'one more check'\n\n")
        # WHEN run
        r, out = self.run_json(d)
        # THEN NO_SENTINEL, exit 1, last_line is the trailing output
        self.assertEqual((r.returncode, out["verdict"]), (1, "NO_SENTINEL"))
        ev = json.loads(Path(out["evidence"]).read_text())
        self.assertEqual(ev["last_line"], "one more check")

    def test_failing_check_is_fail_exit_1(self):
        """A check calling fail ends the run with verdict FAIL, exit 1."""
        # GIVEN an eval whose check fails
        d = self.eval_dir(LIB_SH + 'fail "0 rows"\nwin\n')
        # WHEN run
        r, out = self.run_json(d)
        # THEN FAIL, exit 1
        self.assertEqual((r.returncode, out["verdict"]), (1, "FAIL"))

    def test_refused_and_blocked_pass_through(self):
        """Script exits 2 / 3 (refused / blocked under R8) come out as the CLI's own exit code."""
        for code, verdict in ((2, "REFUSED"), (3, "BLOCKED")):
            # GIVEN an eval exiting with that code
            d = self.eval_dir(f"exit {code}\n", where=f"ev{code}")
            # WHEN run
            r, out = self.run_json(d)
            # THEN the same code and verdict
            with self.subTest(code=code):
                self.assertEqual((r.returncode, out["verdict"]), (code, verdict))

    def test_cap_kills_the_eval_and_exits_4(self):
        """An eval running past --cap is killed (its whole process group) and the run exits 4 TIMEOUT."""
        # GIVEN an eval that would sleep far past the cap, child process included
        d = self.eval_dir("sleep 47.31 &\nsleep 47.32\n")
        # WHEN run with a 1s cap
        r, out = self.run_json(d, "--cap", "1s")
        # THEN exit 4, verdict TIMEOUT, well before the sleep would end
        self.assertEqual((r.returncode, out["verdict"]), (4, "TIMEOUT"))
        self.assertLess(out["duration_s"], 10)
        left = subprocess.run(["pgrep", "-f", "sleep 47.3"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    def test_sentinel_with_extra_spaces_is_not_win(self):
        """The sentinel must match exactly; a padded line is a hand-printed look-alike, not the library's verdict."""
        for i, line in enumerate((" " + SENTINEL, SENTINEL + " ")):
            # GIVEN an eval whose last line is the sentinel with a leading or trailing space
            d = self.eval_dir(f"printf '%s\\n' '{line}'\n", where=f"pad{i}")
            # WHEN run
            r, out = self.run_json(d)
            # THEN not WIN: NO_SENTINEL, exit 1, and the line is recorded as printed
            with self.subTest(line=line):
                self.assertEqual((r.returncode, out["verdict"]), (1, "NO_SENTINEL"))
                self.assertEqual(json.loads(Path(out["evidence"]).read_text())["last_line"], line)

    def test_background_child_dies_when_the_eval_ends(self):
        """A child the eval left running in the background is killed with its process group when the run ends."""
        # GIVEN an eval that starts a long background sleep and then WINs at once
        d = self.eval_dir(LIB_SH + "sleep 47.51 &\nwin\n")
        # WHEN run
        r, out = self.run_json(d)
        # THEN WIN, and the background sleep is gone
        self.assertEqual(out["verdict"], "WIN", r.stderr)
        left = subprocess.run(["pgrep", "-f", "sleep 47.51"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    def test_scripts_own_exit_4_is_fail_not_timeout(self):
        """TIMEOUT means the cap fired; a script that exits 4 itself is an ordinary FAIL, exit 1."""
        # GIVEN an eval that exits 4 at once
        d = self.eval_dir("exit 4\n")
        # WHEN run
        r, out = self.run_json(d)
        # THEN FAIL, exit 1, and the script's exit recorded
        self.assertEqual((r.returncode, out["verdict"], out["exit"]), (1, "FAIL", 4))

    def test_empty_only_is_refused(self):
        """`--only ""` names no check; it is refused (exit 2) rather than run as a silent partial."""
        # GIVEN a winning eval
        d = self.eval_dir(LIB_SH + "win\n")
        # WHEN run with an empty --only
        r = self.cli("eval", "run", str(d), "--only", "")
        # THEN exit 2 with an example, and no evidence written
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"^error: .+\n  example: agent-cli eval run ")
        self.assertFalse((d / "evidence").exists())

    def test_non_executable_entry_without_suffix_is_refused(self):
        """An entry that is neither .sh/.py nor executable cannot run; exit 2 says how to fix it."""
        # GIVEN a plain non-executable entry named check
        d = self.eval_dir("echo hi\n", name="check")
        # WHEN run with --entry check
        r = self.cli("eval", "run", str(d), "--entry", "check")
        # THEN exit 2 with a chmod example, no traceback
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("  example: chmod +x ", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_entry_that_cannot_start_is_error_with_evidence(self):
        """An executable entry with no shebang fails to start: verdict ERROR, exit 1, evidence still written."""
        # GIVEN an executable entry with no shebang line
        d = self.eval_dir("echo hi\n", name="check")
        (d / "check").chmod(0o755)
        # WHEN run
        r, out = self.run_json(d, "--entry", "check")
        # THEN ERROR, exit 1, exit null, and both evidence files name the error
        self.assertEqual((r.returncode, out["verdict"], out["exit"]), (1, "ERROR", None), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        ev = json.loads(Path(out["evidence"]).read_text())
        self.assertEqual((ev["verdict"], ev["exit"]), ("ERROR", None))
        self.assertIn("could not start", ev["error"])
        self.assertIn("could not start", Path(out["evidence"]).with_suffix(".log").read_text())

    def test_only_exports_eval_only_and_never_wins(self):
        """--only runs one check: EVAL_ONLY is set and the verdict library refuses WIN with exit 5 (partial)."""
        # GIVEN an eval that echoes EVAL_ONLY then passes and calls win
        d = self.eval_dir(LIB_SH + 'echo "only=$EVAL_ONLY"\nok "a"\nwin\n')
        # WHEN run with --only rows
        r, out = self.run_json(d, "--only", "rows")
        # THEN exit 5, PARTIAL, EVAL_ONLY reached the script, and no sentinel in the log
        self.assertEqual((r.returncode, out["verdict"]), (5, "PARTIAL"))
        log = Path(out["evidence"]).with_suffix(".log").read_text()
        self.assertIn("only=rows", log)
        self.assertNotIn(SENTINEL, log)

    def test_only_with_a_hand_printed_sentinel_is_still_partial(self):
        """A script that ignores EVAL_ONLY and prints the sentinel itself still cannot WIN a partial run."""
        # GIVEN an eval that prints the sentinel by hand
        d = self.eval_dir(f"printf '%s\\n' '{SENTINEL}'\n")
        # WHEN run with --only
        r, out = self.run_json(d, "--only", "rows")
        # THEN PARTIAL, exit 5
        self.assertEqual((r.returncode, out["verdict"]), (5, "PARTIAL"))

    def test_python_entry_is_found_and_run(self):
        """With no win.sh, win.py is the entry; it imports the verdict library the documented way."""
        # GIVEN a python eval
        body = ("import os, subprocess, sys\n"
                "lib = subprocess.check_output(['agent-cli', 'eval', 'lib', '--py'], text=True).strip()\n"
                "sys.path.insert(0, os.path.dirname(lib))\nfrom verdict import ok, win\nok('a')\nwin()\n")
        d = self.eval_dir(body, name="win.py")
        # WHEN run
        r, out = self.run_json(d)
        # THEN WIN
        self.assertEqual((r.returncode, out["verdict"]), (0, "WIN"), r.stderr)

    def test_entry_runs_with_cwd_the_eval_dir(self):
        """Relative paths inside an eval resolve against its own dir, wherever the CLI is called from."""
        # GIVEN an eval reading a file next to it
        d = self.eval_dir(LIB_SH + '[ -s input.txt ] && ok "input" || fail "no input"\nwin\n')
        (d / "input.txt").write_text("x\n")
        # WHEN run from the project dir
        r, out = self.run_json(d)
        # THEN WIN
        self.assertEqual(out["verdict"], "WIN", r.stdout)

    def test_git_head_of_the_callers_repo_or_null(self):
        """Evidence names the code version under test: HEAD of the repo at the caller's cwd, else null."""
        # GIVEN a project repo with one commit, and a non-repo dir
        run(GIT + ["commit", "-q", "--allow-empty", "-m", "c"], self.p)
        head = run(["git", "rev-parse", "HEAD"], self.p).stdout.strip()
        d = self.eval_dir(LIB_SH + "win\n")
        bare = self.tmp / "bare"
        bare.mkdir()
        # WHEN run from each
        _, a = self.run_json(d)
        _, b = self.run_json(d, cwd=bare)
        # THEN the head, then null
        self.assertEqual(json.loads(Path(a["evidence"]).read_text())["git_head"], head)
        self.assertIsNone(json.loads(Path(b["evidence"]).read_text())["git_head"])

    def test_two_runs_in_one_second_keep_both_evidence_files(self):
        """Evidence is never overwritten: back-to-back runs each get their own file."""
        # GIVEN a fast eval
        d = self.eval_dir(LIB_SH + "win\n")
        # WHEN run twice
        _, a = self.run_json(d)
        _, b = self.run_json(d)
        # THEN two distinct evidence files exist
        self.assertNotEqual(a["evidence"], b["evidence"])
        self.assertEqual(len(list((d / "evidence").glob("*.json"))), 2)

    def test_missing_dir_or_entry_is_refused_with_an_example(self):
        """A typo'd dir or entry fails fast (exit 2) with a correct invocation, never a traceback."""
        # GIVEN an empty dir
        empty = self.tmp / "empty"
        empty.mkdir()
        # WHEN run on a missing dir and on a dir without an entry
        rs = [self.cli("eval", "run", str(self.tmp / "nope")), self.cli("eval", "run", str(empty))]
        # THEN both exit 2 with error + example (eval init for the missing entry)
        for r in rs:
            self.assertEqual(r.returncode, 2)
            self.assertRegex(r.stderr, r"^error: .+\n  example: agent-cli eval (run|init) ")


class Controls(EvalCase):
    EVAL = LIB_SH + 'if [ "${INPUT:-full}" = empty ]; then fail "0 rows"; fi\nok "rows"\nwin\n'

    def control(self, d, name, text, executable=False):
        """Write controls/<name> with text (made executable on request); returns its path."""
        c = d / "controls" / name
        c.parent.mkdir(exist_ok=True)
        c.write_text(text)
        if executable:
            c.chmod(c.stat().st_mode | stat.S_IXUSR)
        return c

    def test_fail_control_that_does_not_win_and_pass_control_that_wins_exit_0(self):
        """A fail control must be rejected and a pass control accepted; both behaving -> exit 0."""
        # GIVEN an eval, an env fail control pointing it at empty input, and a pass control
        d = self.eval_dir(self.EVAL)
        self.control(d, "fail-empty.env", "# empty input\nexport INPUT=\"empty\"\n")
        self.control(d, "pass-baseline.env", "INPUT=full\n")
        self.control(d, "README.md", "not a control\n")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN both ok, 2/2, exit 0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("fail-empty: no WIN (ok)", r.stdout)
        self.assertIn("pass-baseline: WIN (ok)", r.stdout)
        self.assertIn("controls: 2/2", r.stdout)

    def test_fail_control_that_wins_exits_1_naming_it(self):
        """An eval that WINs on input it must reject cannot discriminate; the control's name is reported."""
        # GIVEN an executable fail control that skips the assertion and runs the entry
        d = self.eval_dir(self.EVAL)
        self.control(d, "fail-skips-check", '#!/bin/bash\nexec bash "$EVAL_ENTRY"\n', executable=True)
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN exit 1 naming the control, and its evidence pins the control script's content
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("fail-skips-check: WIN (MISMATCH", r.stdout)
        ev = json.loads(next((d / "evidence").glob("*fail-skips-check.json")).read_text())
        want = hashlib.sha256((d / "controls/fail-skips-check").read_bytes()).hexdigest()
        self.assertEqual(ev["control_sha256"], want)

    def test_env_fail_control_that_wins_exits_1_naming_it(self):
        """A .env fail control whose input the eval accepts proves the eval cannot discriminate."""
        # GIVEN an env fail control that feeds the eval good input
        d = self.eval_dir(self.EVAL)
        self.control(d, "fail-not-empty.env", "INPUT=full\n")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN exit 1 naming it in the line and the summary
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("fail-not-empty: WIN (MISMATCH", r.stdout)
        self.assertIn("mismatched: fail-not-empty", r.stdout)

    def test_control_that_cannot_start_is_reported_and_the_rest_still_run(self):
        """A broken executable control (no shebang) tested nothing: it is named as ERROR and the others run."""
        # GIVEN a broken executable fail control and a working env fail control after it
        d = self.eval_dir(self.EVAL)
        self.control(d, "fail-a-broken", "echo no shebang\n", executable=True)
        self.control(d, "fail-b-empty.env", "INPUT=empty\n")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN exit 1 naming the broken one, the next control still ran, and the broken one left evidence
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("fail-a-broken: no WIN (ERROR: could not start", r.stdout)
        self.assertIn("fail-b-empty: no WIN (ok)", r.stdout)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(list((d / "evidence").glob("*fail-a-broken.log")))

    def test_control_exiting_4_itself_does_not_stop_the_run(self):
        """Only the cap stops controls early; a control whose script exits 4 is a plain no-WIN."""
        # GIVEN a fail control making the eval exit 4, then a pass control
        d = self.eval_dir(LIB_SH + '[ -n "$FOUR" ] && exit 4\nwin\n')
        self.control(d, "fail-four.env", "FOUR=1\n")
        self.control(d, "pass-ok.env", "")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN both ran and both behaved: exit 0
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("controls: 2/2", r.stdout)

    def test_pass_control_that_does_not_win_exits_1(self):
        """A pass control that fails means the eval rejects a known-good case: exit 1."""
        # GIVEN a pass control feeding empty input
        d = self.eval_dir(self.EVAL)
        self.control(d, "fail-empty.env", "INPUT=empty\n")
        self.control(d, "pass-wrong.env", "INPUT=empty\n")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN exit 1 naming it
        self.assertEqual(r.returncode, 1)
        self.assertIn("pass-wrong: no WIN (MISMATCH", r.stdout)

    def test_control_name_reaches_the_entry_as_control(self):
        """The entry sees CONTROL=<name>, so one script can branch on which control is running."""
        # GIVEN an eval that fails when CONTROL names the fail control
        d = self.eval_dir(LIB_SH + '[ "$CONTROL" = fail-by-name ] && fail "control"\nwin\n')
        self.control(d, "fail-by-name.env", "")
        # WHEN controls run with --json
        r = self.cli("eval", "controls", str(d), "--json")
        rows = [json.loads(ln) for ln in r.stdout.splitlines()]
        # THEN the control did not win and the summary row says 1/1
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(rows[0]["control"], "fail-by-name")
        self.assertFalse(rows[0]["win"])
        self.assertEqual(rows[-1], {"controls": 1, "ok": 1, "mismatched": []})

    def test_control_past_the_cap_exits_4(self):
        """A control that hangs is killed at --cap and the run exits 4, not a silent pass or fail."""
        # GIVEN an eval that hangs under its fail control
        d = self.eval_dir(LIB_SH + '[ -n "$HANG" ] && sleep 47.41\nwin\n')
        self.control(d, "fail-hang.env", "HANG=1\n")
        # WHEN controls run with a 1s cap
        r = self.cli("eval", "controls", str(d), "--cap", "1s")
        # THEN exit 4, and the hung sleep was killed with its process group
        self.assertEqual(r.returncode, 4, r.stdout)
        left = subprocess.run(["pgrep", "-f", "sleep 47.41"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "")

    def test_no_fail_control_is_blocked_exit_3(self):
        """An eval with no fail control has not been tested (win-condition skill): exit 3 says so."""
        # GIVEN an eval with only a pass control
        d = self.eval_dir(self.EVAL)
        self.control(d, "pass-baseline.env", "")
        # WHEN controls run
        r = self.cli("eval", "controls", str(d))
        # THEN exit 3 quoting the rule
        self.assertEqual(r.returncode, 3)
        self.assertIn("An eval with no fail control has not been tested", r.stderr)


class Init(EvalCase):
    def test_scaffold_cannot_win(self):
        """A fresh scaffold's TODO assertion calls fail, so running it unedited is never WIN."""
        for lang in ("sh", "py"):
            # GIVEN a fresh scaffold
            d = self.tmp / f"new-{lang}"
            init = self.cli("eval", "init", str(d), "--lang", lang)
            # WHEN run
            r, out = self.run_json(d)
            # THEN not WIN, and the scaffold made its entry and controls README
            with self.subTest(lang=lang):
                self.assertEqual(init.returncode, 0, init.stderr)
                self.assertTrue((d / f"win.{lang}").is_file())
                self.assertIn("fail control", (d / "controls/README.md").read_text())
                self.assertEqual((r.returncode, out["verdict"]), (1, "FAIL"), r.stderr)

    def test_init_twice_never_overwrites(self):
        """R4: a second init reports `exists:` per file and keeps an edited eval as it is."""
        # GIVEN an init, then an edit to win.sh
        d = self.tmp / "x"
        self.cli("eval", "init", str(d))
        (d / "win.sh").write_text("edited\n")
        # WHEN init runs again
        r = self.cli("eval", "init", str(d))
        # THEN exists for both files and the edit survives
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"exists: {d / 'win.sh'}", r.stdout)
        self.assertIn(f"exists: {d / 'controls/README.md'}", r.stdout)
        self.assertEqual((d / "win.sh").read_text(), "edited\n")

    def test_dry_run_writes_nothing(self):
        """R5: --dry-run says what it would create and leaves the disk alone."""
        # GIVEN no dir
        d = self.tmp / "dry"
        # WHEN init --dry-run
        r = self.cli("eval", "init", str(d), "--dry-run")
        # THEN it names the files and nothing exists
        self.assertIn(f"would create: {d / 'win.sh'}", r.stdout)
        self.assertFalse(d.exists())


class HandVerdictRule(unittest.TestCase):
    def test_deny_message_names_eval_run(self):
        """The hand-verdict deny tells a worker how to get a verdict: agent-cli eval run."""
        # GIVEN the built-in rules
        # WHEN the hand-verdict rule's message is read
        msg = next(r for r in hook_rules.builtin(hook_rules.CMD) if r["id"] == "agent-cli-hand-verdict")["message"]
        # THEN it names eval run and the library
        self.assertIn("agent-cli eval run <dir>", msg)
        self.assertIn('source "$(agent-cli eval lib --sh)"', msg)
        self.assertNotIn("coming in", msg)


class LockOption(EvalCase):
    """--lock NAME holds `agent-cli lock` NAME for the whole run, so evals sharing a resource take turns."""

    def setUp(self):
        super().setUp()
        self.env = {"AGENT_CLI_STATE": str(self.tmp / "state")}
        # the entry WINs only while the lock dir exists, i.e. while the run holds the lock
        self.d = self.eval_dir(LIB_SH + 'if [ -d "$AGENT_CLI_STATE/locks/gates" ]; then ok held; '
                                        'else fail "lock not held"; fi\nwin\n')

    def test_run_holds_the_lock_during_the_run_and_releases_it(self):
        """The entry sees the lock held, and it is free again after the run."""
        # GIVEN a free lock
        # WHEN the eval runs with --lock gates
        r, out = self.run_json(self.d, "--lock", "gates", env=self.env)
        # THEN WIN (the lock was held inside) and the lock dir is gone afterwards
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out["verdict"], "WIN")
        self.assertFalse((self.tmp / "state/locks/gates").exists())

    def test_busy_lock_exits_3_with_an_example_and_runs_nothing(self):
        """A lock a live process holds blocks the run: exit 3, holder and example on stderr, no evidence."""
        # GIVEN gates held by this (live) test process
        held = self.cli("lock", "acquire", "gates", "--pid", str(os.getpid()), env=self.env)
        self.assertEqual(held.returncode, 0, held.stderr)
        for args in (("run", str(self.d), "--lock", "gates"), ("controls", str(self.d), "--lock", "gates")):
            (self.d / "controls").mkdir(exist_ok=True)
            (self.d / "controls/fail-x.env").write_text("X=1\n")
            # WHEN the eval (or its controls) asks for the same lock
            r = self.cli("eval", *args, env=self.env)
            # THEN exit 3 naming the holder, with an example, and the eval never ran
            self.assertEqual(r.returncode, 3, (args, r.stdout))
            self.assertIn(f"gates held by pid {os.getpid()}", r.stderr)
            self.assertIn("example: agent-cli eval", r.stderr)
            self.assertFalse((self.d / "evidence").exists())
        # AND the holder's lock is untouched
        self.assertTrue((self.tmp / "state/locks/gates").is_dir())

    def test_lock_wait_keeps_trying_before_exit_3(self):
        """--lock-wait reuses `lock acquire --wait`: it keeps trying that long, then exits 3."""
        # GIVEN gates held by this (live) test process
        self.cli("lock", "acquire", "gates", "--pid", str(os.getpid()), env=self.env)
        # WHEN the eval waits 2 s for it
        start = time.monotonic()
        r = self.cli("eval", "run", str(self.d), "--lock", "gates", "--lock-wait", "2", env=self.env)
        # THEN it gave up only after the wait, with exit 3
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertGreaterEqual(time.monotonic() - start, 2)

    def test_controls_hold_the_lock_across_every_control(self):
        """eval controls --lock holds it for all controls: the pass control WINs only under the lock."""
        # GIVEN a pass control (runs the entry) and a fail control that forces a failure
        (self.d / "controls").mkdir()
        (self.d / "controls/pass-held.env").write_text("X=1\n")
        (self.d / "controls/fail-free.env").write_text("AGENT_CLI_STATE=/nonexistent\n")
        # WHEN controls run with --lock gates
        r = self.cli("eval", "controls", str(self.d), "--lock", "gates", "--json", env=self.env)
        # THEN both controls behave and the lock is released afterwards
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["mismatched"], [])
        self.assertFalse((self.tmp / "state/locks/gates").exists())

    def test_failed_run_releases_the_lock(self):
        """A run that fails still releases the lock, so the next eval is not blocked."""
        # GIVEN an entry that fails
        d = self.eval_dir(LIB_SH + 'fail "broken"\n', where="bad")
        # WHEN run with --lock gates
        r, out = self.run_json(d, "--lock", "gates", env=self.env)
        # THEN verdict FAIL and the lock is gone
        self.assertEqual((r.returncode, out["verdict"]), (1, "FAIL"))
        self.assertFalse((self.tmp / "state/locks/gates").exists())

    def test_sigterm_kills_the_eval_and_releases_the_lock(self):
        """SIGTERM to `eval run --lock` kills the eval's processes and releases the lock (no orphan holder)."""
        # GIVEN an eval that records its pid and then sleeps
        pidfile = self.tmp / "child.pid"
        d = self.eval_dir(f'echo $$ > "{pidfile}"\nsleep 60\n', where="slow")
        e = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PROJECT_DIR", "AGENT_CLI_HOOKS")}
        e.update(self.env)
        proc = subprocess.Popen([sys.executable, str(self.p / ".claude/bin/agent-cli"), "eval", "run", str(d),
                                 "--lock", "gates"], cwd=str(self.p), env=e,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        child = int(pidfile.read_text())
        # WHEN the eval process gets SIGTERM
        proc.terminate()
        code = proc.wait(timeout=20)
        # THEN it exits 143, the lock is gone and the entry's process is dead
        self.assertEqual(code, 143)
        self.assertFalse((self.tmp / "state/locks/gates").exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_without_lock_no_lock_is_taken(self):
        """No --lock, no lock: the default run is unchanged (here the entry sees no lock and fails)."""
        # GIVEN a free lock
        # WHEN run without --lock
        r, out = self.run_json(self.d, env=self.env)
        # THEN the entry saw no lock
        self.assertEqual((r.returncode, out["verdict"]), (1, "FAIL"))


if __name__ == "__main__":
    unittest.main()
