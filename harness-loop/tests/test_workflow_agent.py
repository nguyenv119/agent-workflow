"""Tests for harness-loop/exam/trial_export.py and the WorkflowAgent wiring.

trial_export is stdlib-only and runs everywhere. The WorkflowAgent cases need
Harbor, so they live in agent_cases.py, run in harness-loop/.venv, and skip
when that venv is absent.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
EXAM = HERE.parent / "exam"
sys.path.insert(0, str(EXAM))
import trial_export  # noqa: E402

VENV_PY = HERE.parent / ".venv" / "bin" / "python"
SAVE_TRY = HERE.parent.parent / ".claude/skills/coordinator/save_try.py"

def tmpdir(case):
    d = tempfile.TemporaryDirectory()
    case.addCleanup(d.cleanup)
    return Path(d.name)


HOST = {
    "AWS_ACCESS_KEY_ID": "AKIA-bedrock", "AWS_SECRET_ACCESS_KEY": "bedrock-secret",
    "AWS_ADMIN_ACCESS_KEY_ID": "AKIA-admin", "AWS_ADMIN_SECRET_ACCESS_KEY": "admin-secret",
    "ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-x",
    "AWS_SESSION_TOKEN": "session-x", "AWS_PROFILE": "admin", "GITHUB_TOKEN": "ghp-x",
}


class SandboxEnvTest(unittest.TestCase):
    def test_bedrock_passes_exactly_the_listed_vars(self):
        """
        Only the Bedrock-only key pair, region and model ids may enter the
        sandbox (D41). A leaked admin key, API key or OAuth token would let an
        exam run spend on, or act as, the wrong identity.
        """
        # GIVEN a host env holding admin keys, an API key and an OAuth token too
        # WHEN the bedrock sandbox env is built
        env = trial_export.sandbox_env(HOST, "bedrock", tries_dir="/logs/agent/tries")
        # THEN it holds exactly the Bedrock route plus the exam switches
        self.assertEqual(set(env), {
            "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "IS_SANDBOX", "HARNESS_TRIES_DIR"})
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AKIA-bedrock")
        self.assertEqual(env["AWS_REGION"], "us-east-1")
        self.assertEqual(env["ANTHROPIC_MODEL"], "global.anthropic.claude-opus-5-5")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "global.anthropic.claude-haiku-5-5")
        self.assertEqual(env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")
        self.assertEqual(env["HARNESS_TRIES_DIR"], "/logs/agent/tries")
        self.assertNotIn("admin", " ".join(env.values()))

    def test_bedrock_without_keys_fails_loudly(self):
        """
        With no Bedrock keys the run would fall back to nothing and fail deep
        inside the sandbox after setup; fail before any spend instead.
        """
        # GIVEN a host env with only an API key
        # WHEN / THEN building the bedrock env raises naming the missing var
        with self.assertRaisesRegex(RuntimeError, "AWS_ACCESS_KEY_ID"):
            trial_export.sandbox_env({"ANTHROPIC_API_KEY": "sk"}, "bedrock", tries_dir="/t")

    def test_api_mode_passes_only_the_api_key(self):
        """
        `--model-access api` is the one route that uses ANTHROPIC_API_KEY; it
        must not also carry AWS keys or the OAuth token.
        """
        # GIVEN the same host env
        # WHEN the api sandbox env is built
        env = trial_export.sandbox_env(HOST, "api", tries_dir="/t", model="claude-sonnet-5-5")
        # THEN only the API key, model and exam switches travel
        self.assertEqual(set(env), {"ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                                    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "IS_SANDBOX", "HARNESS_TRIES_DIR"})
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-x")
        self.assertEqual(env["ANTHROPIC_MODEL"], "claude-sonnet-5-5")

    def test_unknown_access_mode_is_rejected(self):
        """A typo in model_access must not silently pick a route."""
        # GIVEN / WHEN / THEN an unknown mode raises
        with self.assertRaises(ValueError):
            trial_export.sandbox_env(HOST, "oauth", tries_dir="/t")


class StreamResultTest(unittest.TestCase):
    def test_reads_cost_and_subtype_from_the_result_line(self):
        """
        `total_cost_usd` on the final result line includes subagents (usage
        does not); the probe's cost figure must come from there. The subtype
        tells a budget cut from a normal finish.
        """
        # GIVEN a stream with assistant noise and a result line
        stream = tmpdir(self) / "claude-code.txt"
        stream.write_text("\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": "parent"}),
            json.dumps({"type": "system", "subtype": "init", "session_id": "later"}),
            json.dumps({"type": "assistant", "total_cost_usd": 99}),
            "not json",
            json.dumps({"type": "result", "subtype": "error_max_budget_usd", "total_cost_usd": 11.84}),
        ]) + "\n")
        # WHEN it is parsed
        result = trial_export.stream_result(stream)
        # THEN cost and subtype come from the result line, the session from the first init
        self.assertEqual(result, {"total_cost_usd": 11.84, "subtype": "error_max_budget_usd",
                                  "session_id": "parent", "exam_no_pr_line": False})

    def test_missing_result_gives_nones(self):
        """A run killed before its result line has no cost; say so, never 0."""
        # GIVEN a stream without a result line, and no file at all
        d = tmpdir(self)
        (d / "s.txt").write_text(json.dumps({"type": "assistant"}) + "\n")
        # WHEN parsed
        # THEN every field is empty
        none = {"total_cost_usd": None, "subtype": None, "session_id": None, "exam_no_pr_line": False}
        self.assertEqual(trial_export.stream_result(d / "s.txt"), none)
        self.assertEqual(trial_export.stream_result(d / "missing.txt"), none)


class FinalizeTest(unittest.TestCase):
    def make_trial(self):
        trial = tmpdir(self)
        exam = trial / "agent" / "exam"
        exam.mkdir(parents=True)
        (exam / "start.sha").write_text("a" * 40 + "\n")
        (exam / "end.sha").write_text("b" * 40 + "\n")
        (exam / "diff.patch").write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n")
        (trial / "verifier").mkdir()
        (trial / "verifier" / "test-stdout.txt").write_text("2 passed\n")
        (trial / "verifier" / "reward.txt").write_text("1\n")
        return trial, exam

    def test_finalize_writes_gates_from_the_verifier(self):
        """
        The try folder's gates.txt is the hidden tests' verdict, which only
        exists after Harbor's verifier ran, so it is written host-side then.
        """
        # GIVEN a trial dir after verify
        trial, exam = self.make_trial()
        # WHEN finalize runs
        trial_export.finalize(trial)
        # THEN gates.txt holds the verifier output and the reward
        gates = (exam / "gates.txt").read_text()
        self.assertIn("2 passed", gates)
        self.assertIn("reward: 1", gates)

    def test_finalized_dir_is_accepted_by_save_try_from_files(self):
        """
        B13 hands the exported dir to `save_try.py --from-files`. If the file
        names drift from what save_try reads, every exam try is lost.
        """
        # GIVEN a finalized trial
        trial, exam = self.make_trial()
        trial_export.finalize(trial)
        tries = tmpdir(self)
        # WHEN save_try reads it
        out = subprocess.run(
            [sys.executable, str(SAVE_TRY), "--bead", "exam-1", "--from-files", str(exam),
             "--repo-name", "app", "--result", "pass"],
            env={**os.environ, "HARNESS_TRIES_DIR": str(tries)}, text=True, capture_output=True)
        # THEN a try folder exists with the same diff
        self.assertEqual(out.returncode, 0, out.stderr)
        folder = Path(out.stdout.strip().splitlines()[-1])
        self.assertEqual((folder / "diff.patch").read_text(), (exam / "diff.patch").read_text())
        self.assertIn("2 passed", (folder / "gates.txt").read_text())

    def test_finalize_without_verifier_output_says_so(self):
        """A trial whose verifier never ran must not look like an empty pass."""
        # GIVEN a trial without verifier output
        trial, exam = self.make_trial()
        for f in (trial / "verifier").iterdir():
            f.unlink()
        # WHEN finalize runs
        trial_export.finalize(trial)
        # THEN gates.txt says no verifier output
        self.assertIn("no verifier output", (exam / "gates.txt").read_text())


class WriteResultTest(unittest.TestCase):
    def agent_logs(self, events):
        logs = tmpdir(self)
        (logs / "claude-code.txt").write_text("\n".join(json.dumps(e) for e in events) + "\n")
        (logs / "exam").mkdir()
        return logs

    def test_status_follows_the_result_subtype(self):
        """
        Bead .7 counts rewards only for `ok` tries and costs `budget_cut` ones
        at the cap; a crashed run (no result line) must be `error`, not ok.
        """
        for subtype, status in (("success", "ok"), ("error_max_budget_usd", "budget_cut"),
                                ("error_during_execution", "error"), (None, "error")):
            with self.subTest(subtype=subtype):
                # GIVEN a stream ending with that subtype (or no result line)
                events = [{"type": "result", "subtype": subtype, "total_cost_usd": 1}] if subtype else []
                # WHEN the result is written
                result = trial_export.write_result(self.agent_logs(events), "b", "a" * 40)
                # THEN the status matches
                self.assertEqual(result["status"], status)

    def test_no_pr_line_counts_only_in_the_final_message(self):
        """
        The coordinator skill text contains `EXAM_MODE: no PR`, and it enters
        the stream when the skill is read. Only the parent's final message
        proves the exam-mode finish happened.
        """
        # GIVEN the line inside a tool result but a final message without it
        logs = self.agent_logs([
            {"type": "user", "message": {"content": "...\nEXAM_MODE: no PR\n..."}},
            {"type": "result", "subtype": "success", "result": "BEAD COMPLETE\nPR: none"}])
        # WHEN / THEN it is not counted
        self.assertFalse(trial_export.write_result(logs, "b", "c")["exam_no_pr_line"])

    def test_missing_session_file_leaves_transcript_null(self):
        """A trial whose session file was not downloaded must say so, not crash."""
        # GIVEN an init line naming a session with no file
        logs = self.agent_logs([{"type": "system", "subtype": "init", "session_id": "gone"}])
        # WHEN written / THEN transcript is null and no file was made
        self.assertIsNone(trial_export.write_result(logs, "b", "c")["transcript"])
        self.assertFalse((logs / "exam" / "transcript.jsonl").exists())


@unittest.skipUnless(VENV_PY.exists(), "needs harness-loop/.venv with harbor installed")
class HarborCasesTest(unittest.TestCase):
    def test_agent_cases_pass_in_the_harbor_venv(self):
        """
        agent_cases.py exercises WorkflowAgent against real Harbor classes
        (construction, install/run with a fake sandbox, post-run export). It
        runs in the venv with a host env holding only fake keys, so a real
        developer key can never reach a test and a failure there fails here.
        """
        # GIVEN a host env with fake Bedrock, admin, API and OAuth credentials
        env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **HOST}
        # WHEN the cases run under the venv's python
        r = subprocess.run([str(VENV_PY), "-m", "unittest", "agent_cases"], cwd=HERE, env=env,
                           text=True, capture_output=True)
        # THEN all pass
        self.assertEqual(r.returncode, 0, r.stderr[-4000:])
        self.assertIn("OK", r.stderr)


if __name__ == "__main__":
    unittest.main()
