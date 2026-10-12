"""WorkflowAgent cases that need Harbor; run under harness-loop/.venv.

test_workflow_agent.HarborCasesTest runs this module in the venv with a
scrubbed host env (fake keys only), so the main suite fails when any case here
fails. It is not named test_* so system-python discovery never imports it.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "exam"))

from harbor.agents.installed.claude_code import ClaudeCode  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402
from harbor.utils.env import is_sensitive_env_key  # noqa: E402

import workflow_agent  # noqa: E402
from workflow_agent import WorkflowAgent, trust_cmd  # noqa: E402

BEDROCK = {"AWS_ACCESS_KEY_ID": "AKIA-bedrock", "AWS_SECRET_ACCESS_KEY": "bedrock-secret"}


class FakeEnvironment:
    """Records exec/upload calls: Harbor's real sandboxes (Daytona, Docker amd64) can't run locally."""

    default_user = None

    def __init__(self, workdir="/app", bead="exam-abc"):
        self.workdir, self.bead = workdir, bead
        self.execs, self.uploads = [], []

    async def exec(self, command, user=None, env=None, cwd=None, timeout_sec=None):
        self.execs.append(SimpleNamespace(command=command, user=user, cwd=cwd))
        out = ""
        if command.endswith("; pwd"):
            out = self.workdir + "\n"
        elif "setup_task.sh bead" in command:
            out = f"noise\n{self.bead}\n"
        return SimpleNamespace(return_code=0, stdout=out, stderr="")

    async def upload_file(self, source, target):
        self.uploads.append(target)

    def commands(self, needle):
        return [e for e in self.execs if needle in e.command]


class AgentCase(unittest.TestCase):
    def make_agent(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logs = Path(tmp.name) / "trial-1" / "agent"
        logs.mkdir(parents=True)
        return WorkflowAgent(logs_dir=logs, **kwargs)


class ConstructionTest(AgentCase):
    def test_sandbox_env_is_exactly_the_bedrock_allowlist(self):
        """
        Harbor's ClaudeCode would pick credentials from the whole host env;
        the override must hand the sandbox sandbox_env's vars and nothing
        else, even with admin keys, an API key and an OAuth token on the host.
        """
        # GIVEN the scrubbed host env this module runs under (admin + api + oauth set)
        # WHEN the agent resolves its sandbox env
        env = self.make_agent()._resolve_auth_env()
        # THEN it is the Bedrock allowlist with the Bedrock-only keys
        self.assertEqual(set(env), {
            "CLAUDE_CODE_USE_BEDROCK", "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "IS_SANDBOX", "HARNESS_TRIES_DIR"})
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "AKIA-bedrock")

    def test_budget_kwarg_reaches_claude_as_max_budget_usd(self):
        """
        The cost probe's per-task cap is the run-config budget; if it does not
        reach `claude` as --max-budget-usd, one runaway task can spend the cap.
        """
        # GIVEN max_budget_usd as Harbor passes it (JSON-parsed to a number)
        # WHEN the agent builds its CLI flags
        flags = self.make_agent(max_budget_usd=12).build_cli_flags()
        # THEN the flag is there
        self.assertIn("--max-budget-usd 12", flags)

    def test_default_model_is_bedrock_opus(self):
        """`claude` must run the Bedrock global Opus id, not a first-party model name."""
        # GIVEN no model kwarg / WHEN resolved / THEN the global Opus id
        self.assertEqual(self.make_agent()._resolved_model_name(), "global.anthropic.claude-opus-5-5")

    def test_secrets_are_registered_for_the_trial_scrubber(self):
        """
        Harbor redacts trial output only for sensitive keys in the agent's env
        (what `--ae` sets). Keys taken from the host env are invisible to it,
        so a transcript that printed env would keep them in clear text.
        """
        # GIVEN agents in bedrock and api mode
        for mode, keys in (("bedrock", ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"]),
                           ("api", ["ANTHROPIC_API_KEY"])):
            with self.subTest(mode=mode):
                agent = self.make_agent(model_access=mode)
                # WHEN the scrubber's secret set is built the way Trial._scrub_jobs_dir does
                secrets = {v for k, v in agent.extra_env.items() if is_sensitive_env_key(k)}
                # THEN every key the sandbox gets is in it, and nothing from the other route
                self.assertEqual(secrets, {os.environ[k] for k in keys})

    def test_harness_commit_is_resolved_to_a_sha(self):
        """
        result.json must name the sha that ran; "HEAD" would point at a
        different commit as soon as main moves.
        """
        # GIVEN the default harness_commit (HEAD) / WHEN built
        agent = self.make_agent()
        # THEN it holds the full sha of HEAD
        head = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        self.assertEqual(agent._harness_commit, head)

    def test_kwargs_parsed_to_numbers_or_bools_become_text(self):
        """
        Harbor JSON-parses --agent-kwarg values: `test_command=true` arrives
        as True and an all-digit value as an int. Used as-is they would land
        in CLAUDE.md as "True" or crash the shell quoting.
        """
        # GIVEN JSON-parsed kwarg values
        agent = self.make_agent(test_command=True, title=1234567, run_name=False)
        # WHEN / THEN they are the text the user typed
        self.assertEqual(agent._test_command, "true")
        self.assertEqual(agent._title, "1234567")
        self.assertEqual(agent._run_name, "false")

    def test_preflight_accepts_the_agents_own_kwargs(self):
        """
        Harbor calls preflight(kwargs) before queueing trials and ClaudeCode's
        option model rejects unknown kwargs, so without the override every
        real `harbor run` with harness_commit=... would fail at once.
        """
        kwargs = {"harness_commit": "HEAD", "title": "t", "test_command": "make", "max_budget_usd": 12}
        # GIVEN ClaudeCode's own preflight rejects these kwargs (the control)
        with self.assertRaisesRegex(ValueError, "harness_commit"):
            ClaudeCode.preflight(kwargs, {})
        # WHEN / THEN WorkflowAgent's passes them and still validates ClaudeCode's
        WorkflowAgent.preflight(kwargs, {})
        with self.assertRaisesRegex(ValueError, "no_such_option"):
            WorkflowAgent.preflight({**kwargs, "no_such_option": 1}, {})


class LifecycleTest(AgentCase):
    def install(self, agent, env):
        with mock.patch.object(ClaudeCode, "install", new=mock.AsyncMock()), \
             mock.patch.object(WorkflowAgent, "_pack_harness", return_value=Path(__file__)):
            asyncio.run(agent.install(env))

    def run_agent(self, agent, env, claude=None):
        claude = claude or mock.AsyncMock()
        with mock.patch.object(ClaudeCode, "run", new=claude):
            asyncio.run(agent.run("Fix the counter.\nMore detail.", env, AgentContext()))
        return claude

    def test_install_prepares_the_repo_in_the_workdir(self):
        """
        Tool installs need root; the repo commits must be made as the agent
        user (root-owned .git files would block /work), in the workdir where
        Harbor starts claude.
        """
        # GIVEN an agent and a sandbox whose workdir is /app/repo
        agent, env = self.make_agent(test_command="pytest -q"), FakeEnvironment("/app/repo")
        # WHEN install runs
        self.install(agent, env)
        # THEN tools ran as root and the repo phase as the agent user on /app/repo
        tools, = env.commands("setup_task.sh tools")
        repo, = env.commands("setup_task.sh repo")
        self.assertEqual(tools.user, "root")
        self.assertIsNone(repo.user)
        self.assertIn("/app/repo /installed-agent/exam/harness.tgz 'pytest -q' /logs/agent/exam", repo.command)

    def test_run_starts_work_on_the_bead_and_finishes_in_the_workdir(self):
        """
        claude must get `/work <bead>` (not the task text), and the finish
        step must check the bead branch out in the same workdir and write the
        export into the logs dir the trial downloads.
        """
        # GIVEN an agent and a sandbox that prints bead exam-abc
        agent, env = self.make_agent(), FakeEnvironment("/app/repo", bead="exam-abc")
        # WHEN run executes
        claude = self.run_agent(agent, env)
        # THEN claude got /work exam-abc and finish ran on the workdir + logs dir
        self.assertEqual(claude.await_args.args[0], "/work exam-abc")
        bead, = env.commands("setup_task.sh bead")
        self.assertIn("/app/repo 'Fix the counter.'", bead.command)
        finish, = env.commands("finish_task.sh")
        self.assertTrue(finish.command.endswith("finish_task.sh /app/repo /logs/agent/exam"))
        # AND the workdir was marked trusted in the session config claude uses
        trust, = env.commands("hasTrustDialogAccepted")
        self.assertIn("/logs/agent/sessions/.claude.json", trust.command)
        self.assertIn("--arg p /app/repo", trust.command)

    def test_trust_cmd_marks_the_repo_and_keeps_existing_config(self):
        """
        Untrusted, `claude -p` ignores the project's .claude/settings.json;
        the trust step must set the flag without clobbering other config.
        """
        with tempfile.TemporaryDirectory() as tmp:
            # GIVEN a config dir whose .claude.json already holds a key
            cfg = Path(tmp) / "sessions"
            cfg.mkdir()
            (cfg / ".claude.json").write_text('{"keep": 1}')
            # WHEN the trust command runs for /app/my repo
            subprocess.run(["bash", "-c", trust_cmd(cfg, "/app/my repo")], check=True)
            # THEN the repo is trusted and the old key survives
            data = json.loads((cfg / ".claude.json").read_text())
            self.assertEqual(data, {"keep": 1, "projects": {"/app/my repo": {"hasTrustDialogAccepted": True}}})
            # AND it also works when no config exists yet
            (cfg / ".claude.json").unlink()
            subprocess.run(["bash", "-c", trust_cmd(cfg / "new", "/r")], check=True)
            self.assertTrue(json.loads((cfg / "new" / ".claude.json").read_text())["projects"]["/r"]["hasTrustDialogAccepted"])

    def test_workdir_kwarg_overrides_the_image_workdir_for_every_exec(self):
        """
        betterleaks' image WORKDIR is /app but its repo is /app/betterleaks;
        setup, claude and finish must all use the repo, not the WORKDIR.
        """
        # GIVEN workdir=/app/betterleaks in a sandbox whose WORKDIR is /app
        agent, env = self.make_agent(workdir="/app/betterleaks"), FakeEnvironment("/app")
        # WHEN install and run execute
        self.install(agent, env)
        claude = mock.AsyncMock(side_effect=lambda i, e, c: agent.exec_as_agent(e, "claude"))
        self.run_agent(agent, env, claude)
        # THEN the repo phase used it and every exec (claude included) ran there
        repo, = env.commands("setup_task.sh repo")
        self.assertIn("repo /app/betterleaks ", repo.command)
        self.assertEqual(env.commands("claude")[0].cwd, "/app/betterleaks")
        self.assertEqual({e.cwd for e in env.execs}, {"/app/betterleaks"})

    def test_finish_runs_even_when_claude_fails(self):
        """
        A budget cut or crash raises out of claude's run; the finish step
        must still run so whatever /work pushed is graded and exported.
        """
        # GIVEN claude's run raising
        agent, env = self.make_agent(), FakeEnvironment()
        # WHEN run executes / THEN the error propagates and finish still ran
        with self.assertRaises(RuntimeError):
            self.run_agent(agent, env, mock.AsyncMock(side_effect=RuntimeError("budget")))
        self.assertEqual(len(env.commands("finish_task.sh")), 1)


class PostRunTest(AgentCase):
    def test_post_run_sets_cost_and_writes_the_export(self):
        """
        Bead .7 reads cost, status, branch, the sha that ran and the parent
        transcript from agent/exam/. Cost must be total_cost_usd (subagents
        included), not Harbor's own token estimate.
        """
        # GIVEN downloaded agent logs: stream with init + result, the session, finish output
        agent = self.make_agent()
        agent._bead = "exam-abc"
        logs = agent.logs_dir
        (logs / "claude-code.txt").write_text("\n".join(json.dumps(e) for e in [
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "result", "subtype": "success", "total_cost_usd": 7.25,
             "result": "BEAD COMPLETE\nEXAM_MODE: no PR\n"}]) + "\n")
        session = logs / "sessions" / "projects" / "-app"
        session.mkdir(parents=True)
        (session / "s-1.jsonl").write_text('{"type":"user","sessionId":"s-1"}\n')
        (logs / "exam").mkdir()
        (logs / "exam" / "branch.txt").write_text("feature/bd-exam-abc-fix\n")
        (logs / "exam" / "origin_url.txt").write_text("file:///tmp/origin.git\n")
        context = AgentContext()
        # WHEN post-run runs
        agent.populate_context_post_run(context)
        # THEN cost and the export carry the run's facts
        self.assertEqual(context.cost_usd, 7.25)
        result = json.loads((logs / "exam" / "result.json").read_text())
        self.assertEqual({k: result[k] for k in ("bead", "branch", "origin_url", "harness_commit",
                                                  "status", "total_cost_usd", "exam_no_pr_line")},
                         {"bead": "exam-abc", "branch": "feature/bd-exam-abc-fix",
                          "origin_url": "file:///tmp/origin.git", "harness_commit": agent._harness_commit,
                          "status": "ok", "total_cost_usd": 7.25, "exam_no_pr_line": True})
        self.assertEqual((logs / "exam" / "transcript.jsonl").read_text(),
                         (session / "s-1.jsonl").read_text())

    def test_post_run_without_result_line_leaves_cost_unset(self):
        """A run killed before its result line has no cost: None, never 0."""
        # GIVEN logs with no stream at all
        agent = self.make_agent()
        context = AgentContext()
        # WHEN post-run runs
        agent.populate_context_post_run(context)
        # THEN cost stays None and the status is error
        self.assertIsNone(context.cost_usd)
        self.assertEqual(json.loads((agent.logs_dir / "exam" / "result.json").read_text())["status"], "error")


if __name__ == "__main__":
    unittest.main()
