"""Tests for `agent-cli harness held`: did the mistake a harness fix targets come back after the fix merged?

Why: a fix that ships and then fails silently leaves the same mistake in every later session. The command counts
the pattern's hits in the transcripts for N days after the merge, so a failed fix shows up as a number. Run through
the real CLI on small made-up transcripts and ledgers in temp dirs.
"""
import datetime as dt
import json
import os
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))

CHATTY = "PR merged! Here is the full recap"
TZ = {"TZ": "Asia/Ho_Chi_Minh"}  # the window is local time, so the tests pin it


def assistant(ts, text, uuid=None):
    return {"type": "assistant", "timestamp": ts, "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool(ts, command, uuid=None):
    return {"type": "assistant", "timestamp": ts, "uuid": uuid,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def hook(ts, text, uuid=None, command="guard.sh"):
    return {"type": "attachment", "timestamp": ts, "uuid": uuid,
            "attachment": {"type": "hook_blocking_error", "hookName": "Stop", "command": command,
                           "blockingError": {"blockingError": text, "command": command}}}


def hook_att(ts, kind, **fields):
    return {"type": "attachment", "timestamp": ts, "attachment": {"type": kind, "hookName": "Stop", **fields}}


def tool_result(ts, content, is_error=False):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": content, "is_error": is_error}]}}


def fix(bead, pattern, where="assistant", merged="2026-10-13"):
    return {"bead": bead, "merged_date": merged, "pattern": pattern, "where": where}


class Held(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")
        self.projects = self.tmp / "projects"
        self.projects.mkdir()

    def transcript(self, *records, name="a.jsonl", project="p1"):
        f = self.projects / project / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(json.dumps(r) + "\n" for r in records))
        return f

    def ledger(self, *fixes):
        f = self.tmp / "ledger.jsonl"
        f.write_text("".join(json.dumps(x) + "\n" for x in fixes))
        return f

    def run_cli(self, ledger, *extra):
        return agent_cli(self.p, "harness", "held", "--ledger", str(ledger), "--projects", str(self.projects), *extra,
                         env=TZ)

    def results(self, ledger, *extra):
        p = self.run_cli(ledger, "--json", *extra)
        return p, {r["bead"]: r for r in map(json.loads, p.stdout.splitlines())}

    def test_a_mistake_seen_twice_after_the_merge_came_back(self):
        """
        Verifies a fix whose pattern shows up twice in the 14 days after its merge date reports "came back 2 times"
        and exits 1, and that hits before the merge date, on the merge day itself, and after day 14 are not counted.

        This matters because the fix is judged only on what happened after it shipped; an old hit, or one from the
        hours before the merge, would blame a fix for a mistake it never had a chance to stop.

        If it breaks, a fix that worked reads as failed (or the reverse) and the wrong thing moves up a rung.
        """
        # GIVEN one fix merged 13 Oct and a transcript with 2 hits in the window and 3 outside it
        self.transcript(assistant("2026-10-10T09:00:00.000Z", CHATTY),
                        assistant("2026-10-13T12:00:00.000Z", CHATTY),
                        assistant("2026-10-14T09:00:00.000Z", CHATTY),
                        assistant("2026-10-20T09:00:00.000Z", CHATTY),
                        assistant("2026-10-20T09:05:00.000Z", "a quiet reply"),
                        assistant("2026-11-05T09:00:00.000Z", CHATTY))
        # WHEN the counter runs
        p = self.run_cli(self.ledger(fix("b.1", "PR merged!")))
        # THEN it reports the 2 in-window hits and fails
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("b.1: came back 2 times", p.stdout)

    def test_a_pattern_with_no_hits_after_the_merge_held(self):
        """
        Verifies a control fix whose pattern never appears after its date reports "held 14 days" and exits 0, in the
        same run where another fix came back.

        This matters because the counter is only trusted if it can say "held": a counter that always finds something
        proves nothing.

        If it breaks, every fix looks failed and the weekly report stops separating the fixes that worked.
        """
        # GIVEN a transcript with chatty text, a control fix whose pattern is not in it
        self.transcript(assistant("2026-10-20T09:00:00.000Z", CHATTY), assistant("2026-10-21T09:00:00.000Z", CHATTY))
        # WHEN both fixes are counted, and the control alone
        both = self.run_cli(self.ledger(fix("b.1", "PR merged!"), fix("ctl", "never-appears-zz9")))
        alone = self.run_cli(self.ledger(fix("ctl", "never-appears-zz9")))
        # THEN the control holds beside the one that came back, and alone the run passes
        self.assertIn("ctl: held 14 days", both.stdout)
        self.assertIn("b.1: came back 2 times", both.stdout)
        self.assertEqual((both.returncode, alone.returncode), (1, 0), both.stderr + alone.stderr)
        self.assertIn("ctl: held 14 days", alone.stdout)

    def test_a_pattern_only_counts_in_its_own_message_kind(self):
        """
        Verifies `where` picks the message kind: assistant text, tool-call input, or hook attachment text, and the
        same words in another kind are not a hit.

        This matters because one mistake lives in one place: the words of a rule quoted in a tool call or a hook
        message are not the agent repeating the mistake in chat.

        If it breaks, a fix is charged for text it was never meant to catch.
        """
        # GIVEN the same words in assistant text, a tool call and a hook message
        self.transcript(assistant("2026-10-20T09:00:00.000Z", "wake-up words"),
                        tool("2026-10-20T10:00:00.000Z", "echo wake-up words"),
                        hook("2026-10-20T11:00:00.000Z", "wake-up words"),
                        hook("2026-10-20T11:30:00.000Z", "wake-up words"))
        # WHEN one fix per kind is counted
        p, got = self.results(self.ledger(fix("a", "wake-up words", "assistant"), fix("t", "wake-up words", "tool"),
                                          fix("h", "wake-up words", "hook")))
        # THEN each fix sees only its own kind
        self.assertEqual({k: v["hits"] for k, v in got.items()}, {"a": 1, "t": 1, "h": 2})

    def test_a_record_copied_into_two_files_counts_once(self):
        """
        Verifies a record with the same uuid in two transcript files (a resumed session copies its history) is one hit.

        This matters because resumes are common and each copy would double the count of one real mistake.

        If it breaks, a single slip reads as "came back 2 times" and trips the threshold.
        """
        # GIVEN one hit present in two files under the same uuid
        rec = assistant("2026-10-20T09:00:00.000Z", CHATTY, uuid="u-1")
        self.transcript(rec, name="a.jsonl")
        self.transcript(rec, name="b.jsonl", project="p2")
        # WHEN the counter runs
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")))
        # THEN it is one hit
        self.assertEqual(got["b.1"]["hits"], 1)

    def test_days_narrows_the_window(self):
        """
        Verifies --days sets the window length: a hit on day 10 counts at 14 days and not at 5.

        This matters because the ledger rule is a fixed 14 days but a short check (a quick look 5 days in) must not
        be charged for later days.

        If it breaks, --days is ignored and every check reads the same window.
        """
        # GIVEN a hit 10 days after the merge day
        self.transcript(assistant("2026-10-24T09:00:00.000Z", CHATTY))
        led = self.ledger(fix("b.1", "PR merged!"))
        # WHEN the counter runs with 14 days and with 5
        _, wide = self.results(led)
        _, narrow = self.results(led, "--days", "5")
        # THEN only the wider window sees it
        self.assertEqual((wide["b.1"]["hits"], narrow["b.1"]["hits"]), (1, 0))
        self.assertEqual(narrow["b.1"]["days"], 5)

    def test_json_gives_one_object_per_fix_with_status_and_dates(self):
        """
        Verifies --json prints one object per ledger line with bead, status, hits, first and last hit time.

        This matters because the weekly report and evals read the status instead of parsing a sentence.

        If it breaks, callers fall back to scraping the text line.
        """
        # GIVEN a fix with two hits and a control
        self.transcript(assistant("2026-10-20T09:00:00.000Z", CHATTY), assistant("2026-10-22T09:00:00.000Z", CHATTY))
        # WHEN the counter runs with --json
        p, got = self.results(self.ledger(fix("b.1", "PR merged!"), fix("ctl", "zz9")))
        # THEN each line is one object with the status
        self.assertEqual(len(p.stdout.splitlines()), 2)
        self.assertEqual((got["b.1"]["status"], got["b.1"]["hits"]), ("came_back", 2))
        self.assertEqual(got["b.1"]["first"], "2026-10-20T09:00:00.000Z")
        self.assertEqual(got["b.1"]["last"], "2026-10-22T09:00:00.000Z")
        self.assertEqual((got["ctl"]["status"], got["ctl"]["hits"]), ("held", 0))

    def test_a_window_still_open_says_so(self):
        """
        Verifies a fix merged too recently for its window to have closed reports "held N days so far", not "held 14 days".

        This matters because "held 14 days" is a claim about 14 days that have not happened yet.

        If it breaks, a fix merged yesterday is declared safe.
        """
        # GIVEN a fix with a merge date far in the future and no transcripts
        # WHEN the counter runs
        p = self.run_cli(self.ledger(fix("b.1", "x", merged="2099-01-01")))
        # THEN the window is reported open
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("b.1: held 0 days so far (window open)", p.stdout)

    def test_a_transcript_not_modified_since_the_merge_is_skipped(self):
        """
        Verifies a file whose modified time is before the window start is not read, so a hit stamped inside it is ignored.

        This matters because the default scan covers every session ever saved and old files cannot hold new records.

        If it breaks, the scan reads gigabytes of old transcripts for nothing.
        """
        # GIVEN a file with an in-window record whose mtime is set to 1 Oct
        f = self.transcript(assistant("2026-10-20T09:00:00.000Z", CHATTY))
        old = dt.datetime(2026, 10, 1).timestamp()
        os.utime(f, (old, old))
        # WHEN the counter runs
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")))
        # THEN the file was skipped
        self.assertEqual((got["b.1"]["hits"], got["b.1"]["files"]), (0, 0))

    def test_subagent_transcripts_are_scanned(self):
        """
        Verifies hits in a session's subagent files, including workflow subagents one level deeper, are counted.

        This matters because most tool calls run in subagents and their files outnumber the session files ten to one.

        If it breaks, a mistake that only subagents make reads as "held".
        """
        # GIVEN a session file, a subagent file and a workflow subagent file with one hit each
        self.transcript(tool("2026-10-20T09:00:00.000Z", "echo wake-up words"), project="p1")
        sub = self.projects / "p1" / "sess-1" / "subagents"
        (sub / "workflows" / "wf-1").mkdir(parents=True)
        rec = lambda ts: json.dumps(tool(ts, "echo wake-up words")) + "\n"
        (sub / "agent-a.jsonl").write_text(rec("2026-10-21T09:00:00.000Z"))
        (sub / "workflows" / "wf-1" / "agent-b.jsonl").write_text(rec("2026-10-22T09:00:00.000Z"))
        # WHEN a tool fix is counted
        _, got = self.results(self.ledger(fix("t", "wake-up words", "tool")))
        # THEN all three files were read and all three hits count
        self.assertEqual((got["t"]["hits"], got["t"]["files"]), (3, 3))

    def test_a_hook_that_only_ran_is_not_a_hit_but_a_caught_error_is(self):
        """
        Verifies a hook_success whose text matches is no hit, a blocking-error attachment whose only match is its
        `command` is no hit, and a non-blocking-error attachment whose stderr matches is one hit.

        This matters because a hook hit means the hook caught the mistake; a hook that ran, or whose command line
        names the pattern, says nothing about the agent's behaviour.

        If it breaks, every run of a guard hook is charged to the fix the guard belongs to.
        """
        # GIVEN a successful run with matching content, a block whose command matches, and a non-blocking error
        self.transcript(hook_att("2026-10-20T09:00:00.000Z", "hook_success", content="wake-up words",
                                 command="guard.sh"),
                        hook("2026-10-20T10:00:00.000Z", "something else", command="wake-up words"),
                        hook_att("2026-10-20T11:00:00.000Z", "hook_non_blocking_error", stderr="wake-up words",
                                 stdout="", exitCode=1, command="guard.sh"))
        # WHEN a hook fix is counted
        _, got = self.results(self.ledger(fix("h", "wake-up words", "hook")))
        # THEN only the non-blocking error counts
        self.assertEqual(got["h"]["hits"], 1)
        self.assertEqual(got["h"]["first"], "2026-10-20T11:00:00.000Z")

    def test_a_pretooluse_block_in_a_tool_result_is_a_hook_hit(self):
        """
        Verifies a user record's tool_result whose text starts `PreToolUse:` (string or text-block list), or that is
        an error saying "hook error", is a hook hit, and an ordinary result with the same words is not.

        This matters because a PreToolUse hook that denies a call leaves no hook attachment: the block only shows in
        the result the agent got back.

        If it breaks, every mistake caught before a tool ran is invisible to the counter.
        """
        # GIVEN two blocks, an error naming a hook error, and an ordinary result with the same words
        self.transcript(
            tool_result("2026-10-20T09:00:00.000Z", "PreToolUse:Bash says no: wake-up words"),
            tool_result("2026-10-20T10:00:00.000Z", [{"type": "text", "text": "PreToolUse:Edit denied wake-up words"}]),
            tool_result("2026-10-20T11:00:00.000Z", "Agent hook error: [x.sh]: wake-up words", is_error=True),
            tool_result("2026-10-20T12:00:00.000Z", "file contents: wake-up words"))
        # WHEN a hook fix is counted
        _, got = self.results(self.ledger(fix("h", "wake-up words", "hook")))
        # THEN the three blocks count and the ordinary result does not
        self.assertEqual(got["h"]["hits"], 3)

    def test_a_bash_call_is_searched_by_its_command_only(self):
        """
        Verifies for Bash only input.command is searched, while another tool is searched by name and every input string.

        This matters because a Bash call's description or heredoc can quote the ledger's own pattern text; that is
        the agent editing the ledger, not repeating the mistake.

        If it breaks, seeding the ledger charges the new fix for the session that wrote it.
        """
        # GIVEN a Bash call quoting the words in its description, one in its command, and a Read of a path with them
        bash = lambda ts, inp: {"type": "assistant", "timestamp": ts, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": inp}]}}
        self.transcript(bash("2026-10-20T09:00:00.000Z", {"command": "ls", "description": "wake-up words"}),
                        bash("2026-10-20T10:00:00.000Z", {"command": "echo wake-up words"}),
                        {"type": "assistant", "timestamp": "2026-10-20T11:00:00.000Z", "message": {"content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/wake-up words.md"}}]}})
        # WHEN a tool fix is counted
        _, got = self.results(self.ledger(fix("t", "wake-up words", "tool")))
        # THEN the command and the Read count, the description does not
        self.assertEqual(got["t"]["hits"], 2)

    def test_the_window_edges_are_local_midnights_to_the_minute(self):
        """
        Verifies with TZ Asia/Ho_Chi_Minh and merged_date 2026-10-13 the window is 14 Oct 00:00 local (13 Oct 17:00Z)
        up to 28 Oct 00:00 local (27 Oct 17:00Z): a minute inside each edge counts, a minute outside does not.

        This matters because merged_date is a local date and the next local midnight is the contract: an off-by-hours
        window charges the fix for the day it landed, or drops its last day.

        If it breaks, a fix is judged on the wrong 14 days.
        """
        # GIVEN hits a minute either side of the window start, of the 5-day end and of the 14-day end
        self.transcript(*(assistant(f"2026-10-{d}T{t}:00.000Z", CHATTY) for d, t in (
            ("13", "16:59"), ("13", "17:01"), ("18", "16:59"), ("18", "17:01"), ("27", "16:59"), ("27", "17:01"))))
        # WHEN the 14-day window is counted
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")))
        # THEN the four hits from 13 Oct 17:01Z to 27 Oct 16:59Z count
        self.assertEqual((got["b.1"]["hits"], got["b.1"]["first"], got["b.1"]["last"]),
                         (4, "2026-10-13T17:01:00.000Z", "2026-10-27T16:59:00.000Z"))

    def test_days_5_closes_at_the_local_midnight_ending_day_5(self):
        """
        Verifies --days 5 closes at 19 Oct 00:00 local (18 Oct 17:00Z): a minute inside counts, a minute after does not.

        This matters because the short check must close on the same local-midnight rule as the 14-day one.

        If it breaks, --days shifts the end by hours and a day-6 hit leaks into the day-5 check.
        """
        # GIVEN hits a minute either side of the window start and of the 5-day end
        self.transcript(*(assistant(f"2026-10-{d}T{t}:00.000Z", CHATTY) for d, t in (
            ("13", "16:59"), ("13", "17:01"), ("18", "16:59"), ("18", "17:01"))))
        # WHEN the 5-day window is counted
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")), "--days", "5")
        # THEN the two inside hits count
        self.assertEqual((got["b.1"]["hits"], got["b.1"]["first"], got["b.1"]["last"]),
                         (2, "2026-10-13T17:01:00.000Z", "2026-10-18T16:59:00.000Z"))

    def test_a_closed_window_with_no_transcripts_is_not_held(self):
        """
        Verifies a closed window where no transcript file was read prints "no transcripts in window" and exits 5, not
        "held".

        This matters because a mistyped --projects or a pruned store has nothing to count, and "held" would be a
        claim about data nobody looked at.

        If it breaks, an empty scan certifies every fix.
        """
        # GIVEN an empty transcript root
        # WHEN a fix merged long ago is counted
        p, got = self.results(self.ledger(fix("b.1", "x")))
        text = self.run_cli(self.ledger(fix("b.1", "x")))
        # THEN it is reported as no transcripts, exit 5
        self.assertEqual((p.returncode, got["b.1"]["status"]), (5, "no_transcripts"))
        self.assertIn("b.1: no transcripts in window", text.stdout)
        self.assertNotIn("held", text.stdout.split("scanned")[0])

    def test_a_half_written_last_line_does_not_stop_the_scan(self):
        """
        Verifies a line that is not JSON (a live session's cut-off tail) is skipped and counted, never fatal.

        This matters because the weekly scan runs while sessions are open.

        If it breaks, one live transcript makes the whole report fail.
        """
        # GIVEN a hit followed by a cut-off line
        f = self.transcript(assistant("2026-10-20T09:00:00.000Z", CHATTY))
        with f.open("a") as fh:
            fh.write('{"type": "assistant", "timest')
        # WHEN the counter runs
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")))
        # THEN the hit counts and the bad line is reported
        self.assertEqual((got["b.1"]["hits"], got["b.1"]["bad_lines"]), (1, 1))

    def test_a_bad_ledger_is_refused_with_the_line_and_an_example(self):
        """
        Verifies each ledger fault (not JSON, bad date, bad regex, unknown where, missing key, repeated bead) exits 2
        and names the line.

        This matters because a ledger typo that is skipped silently looks like "held".

        If it breaks, a fix with a broken pattern is reported safe forever.
        """
        good = fix("b.1", "x")
        for name, line in (("not json", "{nope"), ("bad date", json.dumps({**good, "merged_date": "13 Oct"})),
                           ("bad regex", json.dumps({**good, "pattern": "("})),
                           ("bad where", json.dumps({**good, "where": "chat"})),
                           ("missing key", json.dumps({"bead": "b.1"})),
                           ("repeat", json.dumps(good) + "\n" + json.dumps(good))):
            with self.subTest(name):
                # GIVEN a ledger with one fault on its line 1 (the repeat on line 2)
                f = self.tmp / "bad.jsonl"
                f.write_text(line + "\n")
                # WHEN the counter runs
                p = self.run_cli(f)
                # THEN it is refused, names the line and gives an example
                self.assertEqual(p.returncode, 2, p.stdout)
                self.assertIn("line", p.stderr)
                self.assertIn("example:", p.stderr)

    def test_a_missing_ledger_is_refused(self):
        """
        Verifies a ledger path that does not exist exits 2 with an example, and a missing --ledger flag does too.

        This matters because the command never prompts: an agent given no ledger must learn the right call.

        If it breaks, the command hangs or crashes with a traceback.
        """
        # GIVEN no ledger file
        # WHEN the counter runs with a missing path and with no flag
        a = self.run_cli(self.tmp / "nope.jsonl")
        b = agent_cli(self.p, "harness", "held")
        # THEN both are exit 2 with an example
        self.assertEqual((a.returncode, b.returncode), (2, 2))
        self.assertIn("example:", a.stderr)
        self.assertIn("agent-cli harness held --ledger", b.stderr)

    def test_a_projects_dir_that_is_missing_or_a_days_below_one_is_refused(self):
        """
        Verifies a --projects path that is not a directory, and --days 0, each exit 2 with an example.

        This matters because a mistyped transcript root would otherwise scan nothing and report every fix as held.

        If it breaks, "held" can mean "looked at no files".
        """
        # GIVEN a valid ledger
        led = self.ledger(fix("b.1", "x"))
        # WHEN the counter runs with a missing transcript root and with --days 0
        a = agent_cli(self.p, "harness", "held", "--ledger", str(led), "--projects", str(self.tmp / "none"))
        b = self.run_cli(led, "--days", "0")
        # THEN both are refused with an example
        self.assertEqual((a.returncode, b.returncode), (2, 2))
        self.assertIn("example:", a.stderr + b.stderr)


if __name__ == "__main__":
    unittest.main()
