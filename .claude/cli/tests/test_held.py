"""Tests for `agent-cli harness held`: did the mistake a harness fix targets come back after the fix merged?

Why: a fix that ships and then fails silently leaves the same mistake in every later session. The command counts
the pattern's hits in the transcripts for N days after the merge, so a failed fix shows up as a number. Run through
the real CLI on small made-up transcripts and ledgers in temp dirs.
"""
import json
import os
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))

CHATTY = "PR merged! Here is the full recap"


def assistant(ts, text, uuid=None):
    return {"type": "assistant", "timestamp": ts, "uuid": uuid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool(ts, command, uuid=None):
    return {"type": "assistant", "timestamp": ts, "uuid": uuid,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def hook(ts, text, uuid=None):
    return {"type": "attachment", "timestamp": ts, "uuid": uuid,
            "attachment": {"type": "hook_blocking_error", "hookName": "Stop", "blockingError": {"blockingError": text}}}


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
        return agent_cli(self.p, "harness", "held", "--ledger", str(ledger), "--projects", str(self.projects), *extra)

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
        # WHEN both fixes are counted
        p = self.run_cli(self.ledger(fix("b.1", "PR merged!"), fix("ctl", "never-appears-zz9")))
        # THEN the control holds and the other came back
        self.assertIn("ctl: held 14 days", p.stdout)
        self.assertIn("b.1: came back 2 times", p.stdout)

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
        os.utime(f, (1759300000, 1759300000))
        # WHEN the counter runs
        _, got = self.results(self.ledger(fix("b.1", "PR merged!")))
        # THEN the file was skipped
        self.assertEqual((got["b.1"]["hits"], got["b.1"]["files"]), (0, 0))

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
