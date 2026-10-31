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
                               "other": len("t1t2agent-ablockPPPPPPP")})
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

    def test_missing_file_and_empty_transcript_are_refused(self):
        """
        Verifies a path that does not exist, and a file with no start attachments, exit 2 with an example.

        This matters because a wrong path reading as `0 bytes` would look like a perfect session.

        If it breaks, a typo reports a clean start.
        """
        # GIVEN a missing path and a transcript with only a user turn
        empty = self.transcript(USER, ASSISTANT)
        # WHEN startsize runs on each
        a, b = self.run_cli(self.tmp / "nope.jsonl"), self.run_cli(empty)
        # THEN both exit 2 with an example
        for p in (a, b):
            self.assertEqual(p.returncode, 2)
            self.assertIn("example:", p.stderr)

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
