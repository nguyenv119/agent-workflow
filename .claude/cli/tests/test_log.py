"""Tests for `agent-cli log append|show`, mostly through the real CLI against a temp .tsv."""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

from helpers import CLAUDE, CLEAN, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import log  # noqa: E402

HEADER = "ts\tphase\taction\tresult\tevidence\tverdict\tcost_usd\tnote"


class Log(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.f = self.tmp / "loop-demo.tsv"

    def log(self, *args):
        return agent_cli(self.p, "log", *args)

    def append(self, *extra, phase="iter-1", action="RUN_BEAD demo.1", result="pass"):
        return self.log("append", str(self.f), "--phase", phase, "--action", action, "--result", result, *extra)

    def show_json(self, *args):
        r = self.log("show", str(self.f), "--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(ln) for ln in r.stdout.splitlines()]

    def test_header_is_written_once_and_rows_are_numbered(self):
        """The first append creates the file with the header row; later appends add rows and print their number."""
        # GIVEN no log file
        # WHEN two different rows are appended
        one = self.append(phase="start", action="init", result="ok")
        two = self.append("--verdict", "NO")
        # THEN one header, two rows, row numbers 1 and 2
        self.assertEqual((one.returncode, one.stdout), (0, "row: 1\n"), one.stderr)
        self.assertEqual((two.returncode, two.stdout), (0, "row: 2\n"), two.stderr)
        lines = self.f.read_text().split("\n")
        self.assertEqual(lines[0], HEADER)
        self.assertEqual(len(lines), 4)  # header, 2 rows, trailing ""
        self.assertEqual(lines[-1], "")
        self.assertRegex(lines[1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\tstart\tinit\tok\t\t\t\t$")

    def test_escaping_round_trips_tabs_newlines_and_backslashes(self):
        """A note with a tab, newlines and a backslash stays on one 8-field line and comes back unchanged."""
        # GIVEN a note and evidence that would break a naive TSV
        note = "reviewer: missing index\ntry:\t~/tries/demo.2-a2\\n literal\r\nend\\"
        # WHEN appended and shown
        r = self.append("--note", note, "--evidence", "a\tb")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.show_json()
        # THEN the file holds one row of 8 fields, and show returns the original text
        lines = self.f.read_bytes().split(b"\n")
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(lines[1].split(b"\t")), 8)
        self.assertIn(b"reviewer: missing index\\ntry:\\t~/tries/demo.2-a2\\\\n literal\\r\\nend\\\\", lines[1])
        self.assertEqual((rows[0]["note"], rows[0]["evidence"]), (note, "a\tb"))

    def test_show_json_returns_every_appended_field(self):
        """show --json gives one object per row with all 8 keys, as appended."""
        # GIVEN a row with every field set
        r = self.append("--evidence", "acceptance 4/5", "--verdict", "NO", "--cost-usd", "1.25", "--note", "n",
                        phase="iter-3", action="RUN_BEAD demo.2", result="fail")
        self.assertEqual(r.returncode, 0, r.stderr)
        # WHEN shown as JSON
        rows = self.show_json()
        # THEN the fields match, with a UTC second-precision timestamp
        self.assertEqual(len(rows), 1)
        ts = rows[0].pop("ts")
        self.assertRegex(ts, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(rows[0], {"phase": "iter-3", "action": "RUN_BEAD demo.2", "result": "fail",
                                   "evidence": "acceptance 4/5", "verdict": "NO", "cost_usd": "1.25", "note": "n"})

    def test_show_filters_by_phase_and_keeps_the_last_n(self):
        """--phase keeps exact phase matches; --last N keeps the final N of what is left."""
        # GIVEN five rows over three phases, the last one not iter-1 (so --last alone and --phase + --last differ)
        for i, ph in enumerate(["start", "iter-1", "iter-1", "iter-1", "iter-2"]):
            self.assertEqual(self.append(phase=ph, action=f"a{i}").returncode, 0)
        # WHEN filtered
        last2 = self.show_json("--last", "2")
        it1 = self.show_json("--phase", "iter-1", "--last", "2")
        # THEN the right rows come back in file order: --last applies after --phase
        self.assertEqual([r["action"] for r in last2], ["a3", "a4"])
        self.assertEqual([r["action"] for r in it1], ["a2", "a3"])
        self.assertEqual([r["action"] for r in self.show_json("--phase", "iter-1")], ["a1", "a2", "a3"])

    def test_show_text_prints_non_empty_fields_per_row(self):
        """Without --json each row is `key: value` lines (empty fields left out), rows split by a blank line."""
        # GIVEN two rows, one with a two-line note
        self.append("--note", "x\ny", action="a0")
        self.append(action="a1")
        # WHEN shown as text
        r = self.log("show", str(self.f))
        # THEN two blocks; the note's second line is indented
        self.assertEqual(r.returncode, 0, r.stderr)
        blocks = r.stdout.strip("\n").split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertIn("action: a0\n", blocks[0])
        self.assertIn("note: x\n  y", blocks[0])
        self.assertNotIn("verdict:", blocks[1])

    def test_twenty_parallel_appends_give_twenty_intact_rows(self):
        """Concurrent appenders never interleave or lose a row, and each gets its own row number."""
        # GIVEN one new file and a clean environment
        env = {k: v for k, v in os.environ.items() if k not in CLEAN}
        # WHEN 20 appenders run at once
        procs = [subprocess.Popen([sys.executable, str(self.p / ".claude/bin/agent-cli"), "log", "append",
                                   str(self.f), "--phase", "iter-1", "--action", f"lane {i}", "--result", "pass",
                                   "--note", "x" * 3000 + f"\n{i}", "--json"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(20)]
        outs = [p.communicate() for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 20, [e for _, e in outs])
        # THEN header + 20 rows of 8 fields, rows 1..20 each handed out once, every lane present
        lines = self.f.read_text().split("\n")[:-1]
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[0], HEADER)
        self.assertEqual({len(ln.split("\t")) for ln in lines}, {8})
        self.assertEqual(sorted(json.loads(o)["row"] for o, _ in outs), list(range(1, 21)))
        self.assertEqual(sorted(r["action"] for r in self.show_json()), sorted(f"lane {i}" for i in range(20)))

    def test_bad_input_is_refused_with_an_example(self):
        """cli-for-agents "Fail fast with actionable errors": a missing required flag, a non-.tsv path, a non-number
        cost or --last 0 exit 2 with an example,
        and write nothing."""
        # GIVEN an installed project
        # WHEN called wrongly
        cases = [self.log("append", str(self.f), "--phase", "p", "--action", "a"),
                 self.log("append", str(self.tmp / "loop.log"), "--phase", "p", "--action", "a", "--result", "r"),
                 self.append("--cost-usd", "lots"),
                 self.append(phase=""),
                 self.log("show", str(self.f), "--last", "0")]
        # THEN each exits 2 with an example and no file appears
        for r in cases:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("example: agent-cli log ", r.stderr)
        self.assertFalse(self.f.exists())
        self.assertFalse((self.tmp / "loop.log").exists())

    def test_not_a_log_file_is_refused(self):
        """Appending to or showing a .tsv whose first line is not the log header fails without touching it;
        showing a missing file says so."""
        # GIVEN a foreign .tsv
        other = self.tmp / "other.tsv"
        other.write_text("a\tb\n1\t2\n")
        # WHEN appended to, shown, and a missing file shown
        app = self.log("append", str(other), "--phase", "p", "--action", "a", "--result", "r")
        show = self.log("show", str(other))
        missing = self.log("show", str(self.tmp / "nope.tsv"))
        # THEN all refuse; the foreign file is unchanged
        for r, msg in ((app, "not an agent-cli log"), (show, "not an agent-cli log"), (missing, "no such log")):
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn(msg, r.stderr)
        self.assertEqual(other.read_text(), "a\tb\n1\t2\n")

    def test_show_refuses_a_row_with_the_wrong_field_count(self):
        """A hand-edited row that lost its tabs fails show with exit 1, naming the row and its field count."""
        # GIVEN a log whose only row has 2 fields
        self.f.write_text(HEADER + "\na\tb\n")
        # WHEN shown
        r = self.log("show", str(self.f))
        # THEN exit 1 naming row 1
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("row 1 has 2 fields", r.stderr)

    def test_a_missing_parent_dir_is_refused_with_an_example(self):
        """Appending into a directory that does not exist exits 2 with the reason and an example, no traceback."""
        # GIVEN a path under a missing directory
        f = self.tmp / "no-such-dir" / "loop-demo.tsv"
        # WHEN appended to
        r = self.log("append", str(f), "--phase", "p", "--action", "a", "--result", "r")
        # THEN a clean refusal
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(f"cannot open {f}: No such file or directory", r.stderr)
        self.assertIn("example: agent-cli log ", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(f.parent.exists())

    def test_a_non_utf8_log_is_refused_with_an_example(self):
        """Appending to or showing a .tsv that is not UTF-8 exits 2 with an example and leaves it unchanged."""
        # GIVEN a binary .tsv
        raw = HEADER.encode() + b"\n\xff\xfe\n"
        self.f.write_bytes(raw)
        # WHEN appended to and shown
        for r in (self.append(), self.log("show", str(self.f))):
            # THEN each refuses cleanly
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("is not UTF-8 text", r.stderr)
            self.assertIn("example: agent-cli log ", r.stderr)
        self.assertEqual(self.f.read_bytes(), raw)

    def test_a_last_line_without_a_newline_is_ended_before_the_next_row(self):
        """When the file does not end in a newline, the next row starts on its own line and a note says so."""
        # GIVEN a log whose last row lost its trailing newline
        self.assertEqual(self.append(action="a0").returncode, 0)
        self.f.write_bytes(self.f.read_bytes()[:-1])
        # WHEN another row is appended
        r = self.append(action="a1")
        # THEN it is row 2 on its own line, and stderr notes the repair
        self.assertEqual((r.returncode, r.stdout), (0, "row: 2\n"), r.stderr)
        self.assertEqual(r.stderr, "repaired: missing newline\n")
        lines = self.f.read_text().split("\n")
        self.assertEqual(len(lines), 4)
        self.assertEqual({len(ln.split("\t")) for ln in lines[:-1]}, {8})
        self.assertEqual([x["action"] for x in self.show_json()], ["a0", "a1"])

    def test_cost_must_be_a_finite_number_and_is_stored_stripped(self):
        """--cost-usd nan, inf and -inf exit 2; a padded number is stored without the padding."""
        # GIVEN an installed project
        # WHEN non-finite costs and a padded one are appended
        bad = [self.append(f"--cost-usd={v}") for v in ("nan", "inf", "-inf", "Infinity")]
        good = self.append("--cost-usd", " 0.80 ")
        # THEN the non-finite ones refuse, the padded one is stored stripped
        for r in bad:
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("is not a finite number", r.stderr)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual([x["cost_usd"] for x in self.show_json()], ["0.80"])

    def test_help_has_examples_and_bare_group_prints_help(self):
        """cli-for-agents "`--help` that works": `log` alone and each verb's --help end in an Examples block."""
        # GIVEN an installed project
        # WHEN asking for help three ways
        outs = [self.log(), self.log("append", "--help"), self.log("show", "--help")]
        # THEN each prints its Examples
        for r in outs:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Examples:\n  agent-cli log ", r.stdout)


class Duplicate(TmpCase):
    """cli-for-agents "Idempotency" in-process with a fixed clock, so the same-second case is not a race against the
    wall clock."""

    def setUp(self):
        super().setUp()
        self.f = self.tmp / "loop-demo.tsv"

    def run_append(self, ts, json_mode=False, **kw):
        a = argparse.Namespace(file=str(self.f), phase="iter-1", action="a", result="pass", evidence="",
                               verdict="", cost_usd="", note="", json=json_mode)
        vars(a).update(kw)
        buf = io.StringIO()
        with redirect_stdout(buf):
            # REVIEW: mocking core dependency — test may not reflect real behavior
            code = log.append(a, now=lambda: ts)
        return code, buf.getvalue()

    def test_same_fields_in_the_same_second_is_a_no_op(self):
        """A retried append (same second, same fields) prints `already done` and adds nothing."""
        # GIVEN one row appended at a fixed second
        self.assertEqual(self.run_append("2026-01-01T00:00:00Z"), (0, "row: 1\n"))
        # WHEN the same row is appended again in that second
        code, out = self.run_append("2026-01-01T00:00:00Z")
        _, js = self.run_append("2026-01-01T00:00:00Z", json_mode=True)
        # THEN it is a no-op naming the existing row
        self.assertEqual((code, out), (0, "already done: row 1\n"))
        self.assertEqual(json.loads(js), {"status": "already-done", "row": 1, "file": str(self.f)})
        self.assertEqual(len(self.f.read_text().split("\n")), 3)

    def test_a_new_second_or_a_changed_field_appends(self):
        """Only an exact same-second duplicate is skipped: the next second, or one changed field, is a new row."""
        # GIVEN one row
        self.run_append("2026-01-01T00:00:00Z")
        # WHEN appended again a second later, and with a different note
        later = self.run_append("2026-01-01T00:00:01Z")
        changed = self.run_append("2026-01-01T00:00:01Z", note="other")
        # THEN both are new rows
        self.assertEqual(later, (0, "row: 2\n"))
        self.assertEqual(changed, (0, "row: 3\n"))


class HookRule(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")

    def test_hand_appended_loop_log_warns(self):
        """echo/printf >> a loop-*.log or .txt, quoted or not, gets the log-append hint and still runs."""
        # GIVEN hand-appended loop log lines
        cmds = ['echo "iter 3: pass" >> .claude/loop-demo.log',
                "printf '%s\\n' \"$line\" >> \"$HOME/x/.claude/loop-demo.log\"",
                "cd x && echo done >>'.claude/loop-e1.txt'"]
        for c in cmds:
            # WHEN the hook tests each
            r = agent_cli(self.p, "hook", "test", "--cmd", c, "--json")
            got = json.loads(r.stdout)
            # THEN one warn from hand-loop-log, exit 0
            with self.subTest(cmd=c):
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual([x["id"] for x in got["rules"]], ["hand-loop-log"])
                self.assertTrue(re.search(r"agent-cli log append \.claude/loop-<epic-id>\.tsv", r.stdout))


if __name__ == "__main__":
    unittest.main()
