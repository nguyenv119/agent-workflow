"""Tests for harness-loop/project_check.py: one model call says whether a change names one specific project.

Each test builds a throwaway repo with a base commit and one change, then runs project_check.py
as a subprocess (as a human or loop.py would) with PROJECT_CHECK_CLAUDE pointing at
tests/fixtures/fake_claude.py, which replays a recorded Sonnet reply. The two recorded replies in
fixtures/project_check/ came from live Bedrock runs of the planted controls below (the invented
project's invoice table rule, and a generic test rule). Nothing here calls a model.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import GIT, make_repo, run

LOOP_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = LOOP_DIR / "project_check.py"
FAKE = f"{sys.executable} {FIXTURES / 'fake_claude.py'}"
sys.path.insert(0, str(LOOP_DIR))
import loop  # noqa: E402

SKILL = ".claude/skills/implementer/SKILL.md"
BASE = "---\nname: implementer\ndescription: Builds one bead.\n---\n\n# Implementer\n\n## Phase 3: Verify\n\nRun the tests.\n"
GENERIC = ("Before you push, rerun the test you wrote first and confirm it now fails without your change "
           "and passes with it.")
# A rule that only one (invented) project needs, using no word from EXAMPLE_LEAK.
PROJECT = ("When a change touches the invoices_v2 table, rerun the nightly ledger reconciliation and post "
           "the totals in #billing-ops before you merge.")


class ProjectCheckCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="project-check-test-")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.base = make_repo(self.tmp / "repo", {SKILL: BASE})
        self.log = self.tmp / "claude.jsonl"
        self.env = {**os.environ, "PROJECT_CHECK_CLAUDE": FAKE, "FAKE_CLAUDE_LOG": str(self.log)}
        for k in ("FAKE_CLAUDE_REPLY", "FAKE_CLAUDE_EXIT"):
            self.env.pop(k, None)

    def change(self, line, message="implementer: one more rule"):
        """Commit `line` appended to the skill; return the new head sha."""
        (self.repo / SKILL).write_text(BASE + line + "\n")
        run(GIT + ["commit", "-qam", message], self.repo)
        return run(GIT + ["rev-parse", "HEAD"], self.repo).stdout.strip()

    def check(self, *args, reply=None, env=None):
        """Run the script in the repo; return (exit code, parsed JSON verdict or None, stderr)."""
        e = {**self.env, **(env or {})}
        if reply:
            e["FAKE_CLAUDE_REPLY"] = str(FIXTURES / "project_check" / reply)
        p = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo, env=e, capture_output=True, text=True)
        verdict = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else None
        return p.returncode, verdict, p.stderr

    def prompts(self):
        return [json.loads(ln)["prompt"] for ln in self.log.read_text().splitlines()] if self.log.is_file() else []

    def write_reply(self, result, is_error=False, cost=0.01):
        f = self.tmp / "reply.json"
        f.write_text(json.dumps({"type": "result", "is_error": is_error, "result": result, "total_cost_usd": cost}))
        return str(f)


class PlantedControls(ProjectCheckCase):
    def test_generic_change_passes(self):
        """The planted generic control (a rule any repo could use) passes with its recorded reply."""
        # GIVEN a generic rule
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check(self.base, head, reply="clean.json")
        # THEN it is clean, exit 0, with the call's cost
        self.assertEqual(code, 0)
        self.assertEqual((v["clean"], v["hits"], v["error"]), (True, [], None))
        self.assertAlmostEqual(v["cost_usd"], 0.0074815)

    def test_project_only_rule_without_leak_words_is_blocked(self):
        """The planted project-only rule slips past the word-list scan but the recorded model reply blocks it."""
        # GIVEN the project-only rule, which the word-list scan does not catch
        head = self.change(PROJECT, "implementer: reconcile ledgers after invoice changes")
        self.assertEqual(loop.leak_hits([PROJECT], None), [])
        # WHEN checked
        code, v, _ = self.check(self.base, head, reply="project.json")
        # THEN it is blocked, exit 1, with the model's hits
        self.assertEqual(code, 1)
        self.assertFalse(v["clean"])
        self.assertEqual(v["hits"][0]["line"], "L1")
        self.assertIn("invoices_v2", v["hits"][0]["why"])


class WhatTheModelSees(ProjectCheckCase):
    def test_prompt_has_added_lines_commit_message_and_body_numbered(self):
        """Added lines (with file), the commit message and the body each reach the model as numbered lines."""
        # GIVEN a change that also removes a line, and a body file
        (self.repo / SKILL).write_text(BASE.replace("Run the tests.\n", "") + GENERIC + "\n")
        run(GIT + ["commit", "-qam", "implementer: prove the first test"], self.repo)
        head = run(GIT + ["rev-parse", "HEAD"], self.repo).stdout.strip()
        body = self.tmp / "body.md"
        body.write_text("Idea: prove the first test fails.\n\nEvidence:\n  tries/1/try-1\n")
        # WHEN checked
        self.check(self.base, head, str(body))
        # THEN the model saw each line once, numbered, and not the removed line
        prompt = self.prompts()[0]
        self.assertIn(f"L1 [{SKILL}] {GENERIC}", prompt)
        self.assertIn("L2 [commit message] implementer: prove the first test", prompt)
        self.assertIn("L3 [PR body] Idea: prove the first test fails.", prompt)
        self.assertIn("L5 [PR body]   tries/1/try-1", prompt)
        self.assertNotIn("Run the tests.", prompt)

    def test_model_gets_bedrock_keys_and_no_other_credentials(self):
        """The call's env keeps the Bedrock keys and drops API keys, OAuth tokens and admin keys."""
        # GIVEN an env with Bedrock keys and other credentials
        head = self.change(GENERIC)
        extra = {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "CLAUDE_CODE_USE_BEDROCK": "1",
                 "ANTHROPIC_API_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x", "AWS_ADMIN_ACCESS_KEY_ID": "x", "GH_TOKEN": "x"}
        # WHEN checked
        self.check(self.base, head, env=extra)
        # THEN only the Bedrock ones reach the call, with a fresh config dir
        seen = set(json.loads(self.log.read_text().splitlines()[0])["env"])
        self.assertTrue({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CONFIG_DIR"} <= seen)
        self.assertFalse({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_ADMIN_ACCESS_KEY_ID", "GH_TOKEN"} & seen)


class FailsClosed(ProjectCheckCase):
    def assertBlocked(self, code, v, error_part):
        self.assertEqual(code, 1)
        self.assertFalse(v["clean"])
        self.assertIn(error_part, v["error"] or "")

    def test_call_error_blocks(self):
        """A claude call that exits non-zero blocks."""
        # GIVEN a model call that fails
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check(self.base, head, env={"FAKE_CLAUDE_EXIT": "1"})
        # THEN blocked with the exit named
        self.assertBlocked(code, v, "claude exited 1")

    def test_reply_that_is_not_json_blocks(self):
        """A reply in prose, with no JSON object, blocks."""
        # GIVEN a prose reply
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check(self.base, head, env={"FAKE_CLAUDE_REPLY": self.write_reply("Looks fine to me.")})
        # THEN blocked
        self.assertBlocked(code, v, "no JSON object")

    def test_reply_with_wrong_shape_blocks(self):
        """A JSON reply without a boolean clean and a hits list blocks."""
        # GIVEN {"clean": "yes"}
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check(self.base, head, env={"FAKE_CLAUDE_REPLY": self.write_reply('{"clean": "yes"}')})
        # THEN blocked
        self.assertBlocked(code, v, "not {clean: bool")

    def test_clean_true_with_a_hit_blocks(self):
        """A reply that says clean but lists a hit is treated as blocked: a hit always wins."""
        # GIVEN a self-contradicting reply
        head = self.change(GENERIC)
        reply = self.write_reply('{"clean": true, "hits": [{"line": "L1", "why": "names a table"}]}')
        # WHEN checked
        code, v, _ = self.check(self.base, head, env={"FAKE_CLAUDE_REPLY": reply})
        # THEN blocked, no error, the hit kept
        self.assertEqual(code, 1)
        self.assertEqual((v["clean"], v["error"]), (False, None))
        self.assertEqual(v["hits"], [{"line": "L1", "why": "names a table"}])

    def test_error_envelope_blocks(self):
        """An envelope with is_error true (e.g. a budget cap hit) blocks."""
        # GIVEN an error envelope
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check(self.base, head, env={"FAKE_CLAUDE_REPLY": self.write_reply("cap", is_error=True)})
        # THEN blocked
        self.assertBlocked(code, v, "claude returned an error")

    def test_default_command_without_bedrock_blocks_before_any_call(self):
        """With no fake command and no Bedrock env, the check refuses to call claude at all."""
        # GIVEN the default command and an env without CLAUDE_CODE_USE_BEDROCK
        head = self.change(GENERIC)
        env = {k: v for k, v in self.env.items() if k not in ("PROJECT_CHECK_CLAUDE", "CLAUDE_CODE_USE_BEDROCK")}
        p = subprocess.run([sys.executable, str(SCRIPT), self.base, head], cwd=self.repo, env=env,
                           capture_output=True, text=True)
        v = json.loads(p.stdout)
        # THEN blocked, naming Bedrock
        self.assertBlocked(p.returncode, v, "Bedrock only")

    def test_bad_ref_blocks(self):
        """A base that is not a commit blocks (git error), never passes as an empty diff."""
        # GIVEN a ref that does not exist
        head = self.change(GENERIC)
        # WHEN checked
        code, v, _ = self.check("no-such-ref", head)
        # THEN blocked, and no model call was made
        self.assertBlocked(code, v, "git diff")
        self.assertEqual(self.prompts(), [])

    def test_oversized_input_blocks(self):
        """Input over the size cap blocks rather than being cut short."""
        # GIVEN a change far over the cap
        head = self.change("x" * 250_000)
        # WHEN checked
        code, v, _ = self.check(self.base, head)
        # THEN blocked, no model call
        self.assertBlocked(code, v, "over the")
        self.assertEqual(self.prompts(), [])

    def test_wrong_argument_count_is_a_usage_error(self):
        """One argument prints the usage and exits 2."""
        # GIVEN one argument
        # WHEN run
        code, v, err = self.check(self.base)
        # THEN usage, exit 2
        self.assertEqual((code, v), (2, None))
        self.assertIn("project_check.py <base> <head>", err)


if __name__ == "__main__":
    unittest.main()
