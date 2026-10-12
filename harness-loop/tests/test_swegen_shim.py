"""Tests for harness-loop/tasks/fresh/swegen_shim.py, against a fake SWE-gen package.

The real SWE-gen is not installed (installing it needs approval), so each test writes a
throwaway package with the same shapes the shim patches: a module constant used as a
function default, a keyword-only default, a class method default, a typer-style Option
holder, and a literal ClaudeAgentOptions(model=...) call.
"""
import io
import sys
import tempfile
import textwrap
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks" / "fresh"))
import swegen_shim  # noqa: E402

FILES = {
    "__init__.py": "",
    "create/__init__.py": "",
    "create/task_instruction.py": '''
        MODEL_NAME = "gpt-5.5"

        def evaluate_and_generate_task(metadata, client, model: str = MODEL_NAME, api_key=None):
            return client.beta.chat.completions.parse(model=model, messages=[], response_format=dict)

        def judge(client, *, model=MODEL_NAME):
            return client.beta.chat.completions.parse(model=model, messages=[])
        ''',
    "analyze/__init__.py": "",
    "analyze/classifier.py": '''
        VERDICT_MODEL = "gpt-5.5"

        class Verdicts:
            def synthesize(self, client, model: str = VERDICT_MODEL):
                return client.beta.chat.completions.parse(model=model, messages=[])
        ''',
    "cli.py": '''
        from PKG.analyze.classifier import VERDICT_MODEL

        class OptionInfo:  # what typer.Option(...) returns
            def __init__(self, default):
                self.default = default

        def analyze(verdict_model: str = OptionInfo(VERDICT_MODEL)):
            return verdict_model
        ''',
    "create/claude_code_runner.py": '''
        class ClaudeAgentOptions:
            def __init__(self, model=None, cwd=None):
                self.model, self.cwd = model, cwd

        def build_options(cwd):
            return ClaudeAgentOptions(cwd=cwd, model="claude-opus-4-8")

        def other_options():
            return ClaudeAgentOptions(model="claude-haiku-4-5")

        class ResultMessage:
            total_cost_usd = 2.75

        async def query(prompt, options):
            yield "assistant text"
            yield ResultMessage()

        async def run():
            return [m async for m in query(prompt="p", options=None)]
        ''',
}

ENV = {"SWEGEN_LLM_MODEL": "gemini-pro-latest", "SWEGEN_CLAUDE_MODEL": "global.anthropic.claude-opus-5-5",
       "OPENAI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
       "OPENAI_API_KEY": "test-key", "CLAUDE_CODE_USE_BEDROCK": "1"}


class FakeClient:
    """Records the model each parse() call receives (mocked: the OpenAI SDK client)."""

    def __init__(self):
        self.models = []
        self.beta = self
        self.chat = self
        self.completions = self

    def parse(self, **kwargs):
        self.models.append(kwargs["model"])


class ShimTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pkg = "fakeswegen_" + uuid.uuid4().hex[:8]
        root = Path(tmp.name) / self.pkg
        for rel, body in FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(textwrap.dedent(body).replace("PKG", self.pkg))
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        self.addCleanup(self.unload)

    def unload(self):
        for name in [m for m in sys.modules if m.startswith(self.pkg)]:
            del sys.modules[name]

    def targets(self):
        """PATCH_TARGETS with the real package name swapped for the fake one."""
        return tuple((m.replace("swegen", self.pkg, 1), old, var, wrap)
                     for m, old, var, wrap in swegen_shim.PATCH_TARGETS)

    def mod(self, rel):
        return sys.modules[f"{self.pkg}.{rel}"]

    def test_parse_call_receives_env_model_not_gpt(self):
        """
        The point of the shim: the instruction writer's parse() call must get
        SWEGEN_LLM_MODEL. Changing only MODEL_NAME would not do it, because the
        default argument was bound at import; the control call before apply()
        shows the fake really starts on gpt-5.5.
        """
        # GIVEN the fake instruction writer, called once before patching
        import importlib
        ti = importlib.import_module(f"{self.pkg}.create.task_instruction")
        before = FakeClient()
        ti.evaluate_and_generate_task({}, before)
        # WHEN the shim is applied and both default styles are called
        swegen_shim.apply(self.targets(), ENV)
        after = FakeClient()
        ti.evaluate_and_generate_task({}, after)
        ti.judge(after)
        # THEN the control saw gpt-5.5 and both patched calls see the Gemini model
        self.assertEqual(before.models, ["gpt-5.5"])
        self.assertEqual(after.models, ["gemini-pro-latest", "gemini-pro-latest"])
        self.assertEqual(ti.MODEL_NAME, "gemini-pro-latest")

    def test_classifier_method_and_cli_option_defaults_rebound(self):
        """
        The verdict synthesis default lives on a class method, and the CLI copies
        VERDICT_MODEL into a typer Option at import; both must move.
        """
        # GIVEN the shim applied to the fake package
        swegen_shim.apply(self.targets(), ENV)
        client = FakeClient()
        # WHEN the verdict method runs and the CLI default is read
        self.mod("analyze.classifier").Verdicts().synthesize(client)
        option = self.mod("cli").analyze()
        # THEN both use the Gemini model
        self.assertEqual(client.models, ["gemini-pro-latest"])
        self.assertEqual(option.default, "gemini-pro-latest")
        self.assertEqual(self.mod("cli").VERDICT_MODEL, "gemini-pro-latest")

    def test_build_session_literal_model_rebound_others_untouched(self):
        """
        The Claude Code build session passes model="claude-opus-4-8" as a
        literal, so only wrapping ClaudeAgentOptions can change it; a call that
        names a different model must be left alone.
        """
        # GIVEN the shim applied
        swegen_shim.apply(self.targets(), ENV)
        runner = self.mod("create.claude_code_runner")
        # WHEN the runner builds its options, and another call names a different model
        opts, other = runner.build_options("/w"), runner.other_options()
        # THEN the build session gets the Bedrock Opus id, the cwd is kept, the other is untouched
        self.assertEqual((opts.model, opts.cwd), ("global.anthropic.claude-opus-5-5", "/w"))
        self.assertEqual(other.model, "claude-haiku-4-5")

    def test_session_cost_is_printed(self):
        """
        SWE-gen never reports what its Claude session cost; the shim prints each
        result's total_cost_usd so build.sh can add it to the running total.
        """
        import asyncio
        # GIVEN the shim applied
        swegen_shim.apply(self.targets(), ENV)
        runner = self.mod("create.claude_code_runner")
        # WHEN the runner iterates its session
        err = io.StringIO()
        with redirect_stderr(err):
            messages = asyncio.run(runner.run())
        # THEN every message still arrives and the cost line is printed
        self.assertEqual(len(messages), 2)
        self.assertIn("swegen_shim: total_cost_usd: 2.75", err.getvalue())

    def test_settings_raised_and_drift_refused(self):
        """
        Gemini's thinking tokens count toward max_completion_tokens, so the
        shim raises it; if SWE-gen changes the shipped value it must stop.
        """
        import importlib
        name = f"{self.pkg}.create.task_instruction"
        mod = importlib.import_module(name)
        mod.LIMIT = 4096
        # WHEN the setting matches what SWE-gen ships, THEN it is raised
        swegen_shim.apply_settings(((name, "LIMIT", 4096, 32768),))
        self.assertEqual(mod.LIMIT, 32768)
        # WHEN it no longer matches, THEN the shim refuses
        with self.assertRaisesRegex(swegen_shim.ShimError, "SETTING_TARGETS"):
            swegen_shim.apply_settings(((name, "LIMIT", 4096, 32768),))

    def test_missing_model_env_fails(self):
        """No silent fallback to gpt-5.5 when the replacement model is unset."""
        # GIVEN an env without SWEGEN_LLM_MODEL
        env = {k: v for k, v in ENV.items() if k != "SWEGEN_LLM_MODEL"}
        # WHEN the shim is applied
        # THEN it refuses, naming the variable
        with self.assertRaisesRegex(swegen_shim.ShimError, "SWEGEN_LLM_MODEL"):
            swegen_shim.apply(self.targets(), env)

    def test_target_that_no_longer_has_the_model_fails(self):
        """
        If an upgrade renames or moves the model, the shim must stop rather
        than run SWE-gen against OpenAI; this is the drift alarm.
        """
        # GIVEN the instruction writer rewritten to use a different model string
        path = Path(sys.path[0]) / self.pkg / "create" / "task_instruction.py"
        path.write_text(path.read_text().replace("gpt-5.5", "gpt-6"))
        # WHEN the shim is applied
        # THEN it fails naming the module
        with self.assertRaisesRegex(swegen_shim.ShimError, "task_instruction no longer mentions"):
            swegen_shim.apply(self.targets(), ENV)


class EnvTest(unittest.TestCase):
    def test_good_env_passes(self):
        """The env build.sh sets is accepted."""
        # GIVEN the full env
        # WHEN checked
        # THEN no error
        swegen_shim.check_env(ENV)

    def test_env_problems_are_refused(self):
        """
        Calls must go to Gemini and Bedrock: no OPENAI_BASE_URL means OpenAI,
        a direct Anthropic key or OAuth token means not Bedrock.
        """
        # GIVEN three broken envs
        no_base = {k: v for k, v in ENV.items() if k != "OPENAI_BASE_URL"}
        direct = dict(ENV, ANTHROPIC_API_KEY="x")
        not_bedrock = dict(ENV, CLAUDE_CODE_USE_BEDROCK="0")
        auth_token = dict(ENV, ANTHROPIC_AUTH_TOKEN="x")
        admin = dict(ENV, AWS_ADMIN_ACCESS_KEY_ID="x")
        # WHEN each is checked
        # THEN each is refused with the reason
        for env, why in ((no_base, "OPENAI_BASE_URL"), (direct, "ANTHROPIC_API_KEY"),
                         (not_bedrock, "CLAUDE_CODE_USE_BEDROCK"),
                         (auth_token, "ANTHROPIC_AUTH_TOKEN"), (admin, "AWS_ADMIN_ACCESS_KEY_ID")):
            with self.assertRaisesRegex(swegen_shim.ShimError, why):
                swegen_shim.check_env(env)

    def test_main_exits_clearly_when_swegen_missing(self):
        """Without the swegen console script, main() exits saying it is not installed."""
        # GIVEN a good env and an entry point name nothing provides
        # WHEN main runs
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            swegen_shim.main(["create"], env=ENV, entry="swegen-not-installed-" + uuid.uuid4().hex)
        # THEN the exit message says so
        self.assertIn("is not installed", str(cm.exception.code))


if __name__ == "__main__":
    unittest.main()
