"""Tests for `agent-cli eval init|run|controls`: tiny real eval scripts run through the installed CLI."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
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
        # THEN exit 1 naming the control
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("fail-skips-check: WIN (MISMATCH", r.stdout)

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
        # THEN exit 4
        self.assertEqual(r.returncode, 4, r.stdout)

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


if __name__ == "__main__":
    unittest.main()
