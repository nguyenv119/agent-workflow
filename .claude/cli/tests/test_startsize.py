"""Tests for `agent-cli harness startsize`: bytes of text a session loads before its first reply, per piece.

Why: instruction files, the memory index, the skill listing and hook output all reach the model before the first
message, and nobody sees them grow. The command reads them back from the saved transcript so a number replaces a
guess. Run through the real CLI on small made-up transcripts in temp dirs.
"""
import json
import sys
import unittest

from helpers import CLAUDE, TmpCase, agent_cli, make_project

sys.path.insert(0, str(CLAUDE / "cli"))
from agent_cli import startsize  # noqa: E402


def att(kind, **fields):
    return {"type": "attachment", "attachment": {"type": kind, **fields}}


def instructions(*files):
    return att("instructions", files=[{"path": p, "type": t, "content": c} for p, t, c in files])


def hook(name, command, content):
    return att("hook_success", hookName=name, hookEvent="SessionStart", command=command, content=content,
               stdout=content + "x" * 500, exitCode=0)


ASSISTANT = {"type": "assistant", "message": {"role": "assistant", "content": "ok"}}
USER = {"type": "user", "message": {"role": "user", "content": "hello"}}


class Startsize(TmpCase):
    def setUp(self):
        super().setUp()
        self.p = make_project(self.tmp / "proj")

    def transcript(self, *records, name="t.jsonl"):
        f = self.tmp / name
        f.write_text("".join(json.dumps(r) + "\n" for r in records))
        return f

    def run_cli(self, f, *extra):
        return agent_cli(self.p, "harness", "startsize", str(f), *extra)

    def basic(self):
        return self.transcript(
            hook("SessionStart:startup", "echo hi", "A" * 10),
            hook("SessionStart:startup", "cat notes.md", "B" * 30),
            USER,
            att("skill_listing", content="S" * 100, skillCount=2),
            instructions(("/h/CLAUDE.md", "User", "I" * 40), ("/p/CLAUDE.md", "Project", "J" * 60),
                         ("/h/mem/MEMORY.md", "AutoMem", "M" * 200)),
            att("deferred_tools_delta", addedLines=["t1", "t2"]),
            att("agent_listing_delta", addedLines=["agent-a"]),
            att("mcp_instructions_delta", addedBlocks=["block"]),
            att("output_style_instructions", style={"name": "x", "prompt": "P" * 7}),
            att("environment", snapshot={"platform": "x" * 999}),
            ASSISTANT)

    def test_reports_bytes_per_piece_and_the_total(self):
        """
        Verifies each piece is the sum of its attachments' text and the total is the sum of the pieces.

        This matters because the budget is set per piece; a piece that is lumped into another hides what grew.

        If it breaks, the report blames the wrong file and the wrong thing gets trimmed.
        """
        # GIVEN a transcript with hooks, skills, instruction files, memory and tool listings before the reply
        f = self.basic()
        # WHEN startsize runs
        p = self.run_cli(f, "--json")
        # THEN every piece holds its own bytes and the environment attachment is not counted
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        got = {k: v["bytes"] for k, v in out["pieces"].items()}
        self.assertEqual(got, {"instructions": 100, "memory": 200, "skills": 100, "hooks": 40,
                               "listings": len("t1t2agent-ablock"), "other": 7})
        self.assertEqual(out["total"], sum(got.values()))

    def test_counts_utf8_bytes_not_characters(self):
        """
        Verifies a multibyte character counts as its bytes, like `wc -c` on the stored text.

        This matters because the hand check is wc -c; characters would undercount any accented or CJK text.

        If it breaks, the report and the hand count disagree on every non-ASCII file.
        """
        # GIVEN one instruction file of 3 two-byte characters
        f = self.transcript(instructions(("/h/a.md", "User", "é" * 3)), ASSISTANT)
        # WHEN startsize runs
        out = json.loads(self.run_cli(f, "--json").stdout)
        # THEN it reports 6 bytes
        self.assertEqual(out["pieces"]["instructions"]["bytes"], 6)

    def test_ignores_everything_after_the_first_reply(self):
        """
        Verifies attachments after the first assistant record are left out.

        This matters because a long session re-sends the skill listing and hook text after a compaction; counting
        those would report a start size no fresh session has.

        If it breaks, long transcripts always look over budget.
        """
        # GIVEN a skill listing before the reply and a second one after it
        f = self.transcript(att("skill_listing", content="S" * 10), ASSISTANT,
                            att("skill_listing", content="S" * 500))
        # WHEN startsize runs
        out = json.loads(self.run_cli(f, "--json").stdout)
        # THEN only the first counts
        self.assertEqual(out["total"], 10)

    def test_each_hook_is_listed_with_its_command_and_bytes(self):
        """
        Verifies every hook's output is its own row, named by its command, and counts what the model got.

        This matters because a hook's stored `content` (the model's copy, cut when the output was large) differs
        from its raw stdout, and the biggest hook is the one to fix.

        If it breaks, the report counts raw stdout or cannot say which hook is big.
        """
        # GIVEN two hooks, the second with a long raw stdout but a short stored content
        f = self.basic()
        # WHEN startsize runs in text mode
        p = self.run_cli(f)
        # THEN both hooks appear with the bytes of their stored content
        self.assertIn("echo hi", p.stdout)
        self.assertIn("cat notes.md", p.stdout)
        rows = json.loads(self.run_cli(f, "--json").stdout)["parts"]
        self.assertEqual([r["bytes"] for r in rows if r["piece"] == "hooks"], [10, 30])

    def test_cancelled_hook_and_extra_context_lists(self):
        """
        Verifies a timed-out hook adds nothing and a hook's additional-context list is joined into one row.

        This matters because both appear in real transcripts; a crash or a miscount would break the budget check.

        If it breaks, startsize fails on a session whose hook timed out.
        """
        # GIVEN a cancelled hook and an additional-context hook with two strings
        f = self.transcript(
            att("hook_cancelled", hookName="SessionStart:startup", command="slow", timedOut=True),
            att("hook_additional_context", hookName="SessionStart:startup", content=["ab", "cd"]), ASSISTANT)
        # WHEN startsize runs
        out = json.loads(self.run_cli(f, "--json").stdout)
        # THEN only the context hook counts, as "ab\ncd"
        self.assertEqual(out["pieces"]["hooks"]["bytes"], 5)

    def test_over_budget_piece_exits_1_and_names_the_overage(self):
        """
        Verifies a piece over its budget fails the command and the line says by how much.

        This matters because the SessionStart check and a person both act on exactly this line.

        If it breaks, an over-budget session starts without anyone being told.
        """
        # GIVEN a budget that gives memory 1 KB while the transcript holds 2,000 bytes of it
        f = self.transcript(instructions(("/h/MEMORY.md", "AutoMem", "m" * 2000)), ASSISTANT)
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"total_kb": 75, "memory_kb": 1}))
        # WHEN startsize runs with the budget
        p = self.run_cli(f, "--budget", str(b))
        # THEN exit 1 and the memory line shows the budget and the overage (2000 - 1024)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("memory: 2000 bytes, budget 1024, over by 976", p.stdout)

    def test_total_can_be_over_when_every_piece_is_under(self):
        """
        Verifies the total budget is checked on its own.

        This matters because many pieces each just under their cap still add up to a bloated start.

        If it breaks, the per-piece caps hide growth spread across all of them.
        """
        # GIVEN two pieces of 600 bytes each under a 1 KB cap, and a total cap of 1 KB
        f = self.transcript(att("skill_listing", content="s" * 600), instructions(("/a", "User", "i" * 600)),
                            ASSISTANT)
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"total_kb": 1, "skills_kb": 1, "instructions_kb": 1}))
        # WHEN startsize runs with the budget
        out = self.run_cli(f, "--budget", str(b), "--json")
        # THEN exit 1, only the total is listed as over
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertEqual(json.loads(out.stdout)["over"], ["total"])

    def test_under_budget_exits_0(self):
        """
        Verifies a session inside every cap exits 0 with an empty `over` list.

        This matters because the SessionStart check must stay silent when nothing is wrong.

        If it breaks, a clean start files a card every time.
        """
        # GIVEN a small transcript and a generous budget
        f = self.basic()
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"total_kb": 75, "skills_kb": 18, "memory_kb": 20, "instructions_kb": 20,
                                 "hooks_kb": 10}))
        # WHEN startsize runs with the budget
        p = self.run_cli(f, "--budget", str(b), "--json")
        # THEN exit 0, nothing over
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["over"], [])

    def test_bad_budget_key_is_refused(self):
        """
        Verifies a budget file with an unknown key or a non-number is refused with a correct example.

        This matters because a typo such as `memroy_kb` would otherwise leave that piece unbudgeted for good.

        If it breaks, a misspelt cap is silently never enforced.
        """
        # GIVEN a budget with a misspelt key
        f = self.basic()
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"memroy_kb": 20}))
        # WHEN startsize runs with it
        p = self.run_cli(f, "--budget", str(b))
        # THEN exit 2 and the error names the key
        self.assertEqual(p.returncode, 2)
        self.assertIn("memroy_kb", p.stderr)
        self.assertIn("example:", p.stderr)

    def test_missing_file_is_refused(self):
        """
        Verifies a path that does not exist exits 2 with an example.

        This matters because a wrong path reading as `0 bytes` would look like a perfect session.

        If it breaks, a typo reports a clean start.
        """
        # GIVEN a path that does not exist
        # WHEN startsize runs on it
        p = self.run_cli(self.tmp / "nope.jsonl")
        # THEN exit 2 with an example
        self.assertEqual(p.returncode, 2)
        self.assertIn("is not a file", p.stderr)
        self.assertIn("example:", p.stderr)

    def test_transcript_without_start_attachments_is_refused(self):
        """
        Verifies a file with no start attachments exits 2 with an example.

        This matters because a file that is not a session transcript must not read as a clean start.

        If it breaks, any jsonl file reports 0 bytes and passes.
        """
        # GIVEN a transcript with only a user turn
        empty = self.transcript(USER, ASSISTANT)
        # WHEN startsize runs on it
        p = self.run_cli(empty)
        # THEN exit 2 with an example
        self.assertEqual(p.returncode, 2)
        self.assertIn("no start attachments", p.stderr)
        self.assertIn("example:", p.stderr)

    def test_exclude_listings_drops_them_from_the_total_only(self):
        """
        Verifies --exclude leaves the named piece out of the total and out of the cap check, but still reports it.

        This matters because whether the tool, agent and MCP listings count against the budget is a decision that
        may go either way; one flag must settle it without a code change.

        If it breaks, the total cannot be made to ignore listings the project does not control.
        """
        # GIVEN 100 bytes of skills and 1,000 bytes of listings against a 1 KB total cap
        f = self.transcript(att("skill_listing", content="S" * 100, skillCount=1),
                            att("deferred_tools_delta", addedLines=["t" * 1000]), ASSISTANT)
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"total_kb": 1}))
        # WHEN startsize runs with and without --exclude listings
        counted = self.run_cli(f, "--json", "--budget", str(b))
        excluded = self.run_cli(f, "--json", "--budget", str(b), "--exclude", "listings")
        # THEN the listings count by default (1,100 > 1,024) and are left out with the flag, still reported
        c, e = json.loads(counted.stdout), json.loads(excluded.stdout)
        self.assertEqual((counted.returncode, c["total"], c["over"]), (1, 1100, ["total"]))
        self.assertEqual((excluded.returncode, e["total"], e["over"]), (0, 100, []))
        self.assertEqual(e["pieces"]["listings"]["bytes"], 1000)
        self.assertEqual(e["total_excludes"], ["listings"])

    def test_unknown_exclude_piece_is_refused(self):
        """
        Verifies a misspelt --exclude piece exits 2 naming the real pieces.

        This matters because a typo would silently leave the total unchanged.

        If it breaks, the caller believes the listings are excluded when they are not.
        """
        # GIVEN a valid transcript
        # WHEN --exclude names a piece that does not exist
        p = self.run_cli(self.basic(), "--exclude", "tools")
        # THEN exit 2 and the error lists the pieces
        self.assertEqual(p.returncode, 2)
        self.assertIn("listings", p.stderr)

    def test_exact_cap_is_ok_and_one_byte_over_fails(self):
        """
        Verifies a piece of exactly cap bytes passes and cap + 1 fails.

        This matters because the budget is an upper bound; off by one either way misjudges a session at the line.

        If it breaks, a session exactly at the cap is flagged, or one just over it passes.
        """
        # GIVEN a 1 KB skills cap
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"skills_kb": 1}))
        at = self.transcript(att("skill_listing", content="s" * 1024, skillCount=1), ASSISTANT, name="at.jsonl")
        over = self.transcript(att("skill_listing", content="s" * 1025, skillCount=1), ASSISTANT, name="over.jsonl")
        # WHEN startsize runs on each
        # THEN exactly 1,024 bytes passes and 1,025 fails
        self.assertEqual(self.run_cli(at, "--budget", str(b)).returncode, 0)
        self.assertEqual(self.run_cli(over, "--budget", str(b)).returncode, 1)

    def test_json_reports_total_overage_and_unbudgeted_shape(self):
        """
        Verifies --json carries budget_total and total_over_by, and nulls them without a budget.

        This matters because the SessionStart check reads these fields to word its card.

        If it breaks, the card cannot say by how much the total is over.
        """
        # GIVEN 2,000 bytes of skills and a 1 KB total cap
        f = self.transcript(att("skill_listing", content="s" * 2000, skillCount=1), ASSISTANT)
        b = self.tmp / "budget.json"
        b.write_text(json.dumps({"total_kb": 1}))
        # WHEN startsize runs with and without the budget
        with_b = json.loads(self.run_cli(f, "--json", "--budget", str(b)).stdout)
        without = json.loads(self.run_cli(f, "--json").stdout)
        # THEN the overage is 976 with it, and null with an empty over list without it
        self.assertEqual((with_b["budget_total"], with_b["total_over_by"]), (1024, 976))
        self.assertEqual((without["budget_total"], without["total_over_by"], without["over"]), (None, None, []))

    def test_unicode_line_separators_inside_a_record_do_not_split_it(self):
        """
        Verifies raw U+2028 and U+0085 inside a JSON string do not break a record across lines.

        This matters because transcripts written with raw UTF-8 can hold them, and a record is one `\\n` line.

        If it breaks, a valid transcript is refused as "not JSON".
        """
        # GIVEN a skill listing that holds raw U+2028 and U+0085 (written without ASCII escaping)
        text = "a\u2028b\u0085c"
        f = self.tmp / "u.jsonl"
        f.write_text(json.dumps(att("skill_listing", content=text, skillCount=1), ensure_ascii=False) + "\n"
                     + json.dumps(ASSISTANT) + "\n", encoding="utf-8")
        # WHEN startsize runs
        p = self.run_cli(f, "--json")
        # THEN it counts the UTF-8 bytes of the text
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["pieces"]["skills"]["bytes"], len(text.encode("utf-8")))

    def test_bad_line_after_the_first_reply_is_never_read(self):
        """
        Verifies a half-written or corrupt tail after the first reply does not fail the count.

        This matters because a live session's transcript is still being appended to.

        If it breaks, measuring a running session exits 2 on its last partial line.
        """
        # GIVEN a good prefix, the first reply, then a truncated line
        f = self.tmp / "tail.jsonl"
        f.write_text(json.dumps(att("skill_listing", content="s" * 10, skillCount=1)) + "\n"
                     + json.dumps(ASSISTANT) + "\n{bad", encoding="utf-8")
        # WHEN startsize runs
        p = self.run_cli(f, "--json")
        # THEN the prefix is counted and the tail ignored
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["total"], 10)

    def test_wrong_typed_fields_count_zero_instead_of_crashing(self):
        """
        Verifies fields of an unexpected type (list for a string, string for a dict, null file) add no bytes and no crash.

        This matters because the transcript format is not ours; a changed shape must not turn "checked" into a traceback.

        If it breaks, exit 1 from a Python traceback is read as "over budget".
        """
        # GIVEN attachments with wrong-typed fields next to one good one
        f = self.transcript(att("hook_success", hookName="h", command="c", content=["x"]),
                            att("instructions", files=[None, "str", {"path": "p", "type": "User", "content": ["z"]}]),
                            att("session_context", context=["not", "a", "dict"]),
                            att("output_style_instructions", style="plain string"),
                            att("deferred_tools_delta", addedLines=["ok", 7]),
                            att("hook_additional_context", hookName="h", content="hello"), ASSISTANT)
        # WHEN startsize runs
        p = self.run_cli(f, "--json")
        # THEN it exits 0; the string context is counted once as 5 bytes, not joined per character
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["pieces"]["hooks"]["bytes"], 5 + 1)
        self.assertEqual(out["pieces"]["listings"]["bytes"], 2)

    def test_unparsable_line_is_refused_with_its_number(self):
        """
        Verifies a line that is not JSON stops the command and is named by line number.

        This matters because skipping it could drop the one attachment that holds most of the bytes.

        If it breaks, a damaged transcript reports a smaller start than the real one.
        """
        # GIVEN a transcript whose second line is cut off
        f = self.tmp / "bad.jsonl"
        f.write_text(json.dumps(att("skill_listing", content="s")) + "\n" + '{"type": "att\n')
        # WHEN startsize runs
        p = self.run_cli(f)
        # THEN exit 2 and the error says line 2
        self.assertEqual(p.returncode, 2)
        self.assertIn("line 2", p.stderr)

    def test_measure_is_importable_and_pure(self):
        """
        Verifies startsize.measure takes parsed records and returns the same numbers as the CLI.

        This matters because other tools import it instead of shelling out.

        If it breaks, the import path and the command line disagree.
        """
        # GIVEN parsed records
        recs = [att("skill_listing", content="abc"), ASSISTANT]
        # WHEN measured directly
        m = startsize.measure(recs)
        # THEN the total is 3
        self.assertEqual(m["total"], 3)


if __name__ == "__main__":
    unittest.main()
