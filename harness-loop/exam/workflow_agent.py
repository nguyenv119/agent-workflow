"""Harbor agent that runs one exam task through agent-workflow's real `/work`.

    PYTHONPATH=harness-loop/exam harbor run --agent workflow_agent:WorkflowAgent \\
      --path <task dir> --env daytona \\
      --agent-kwarg harness_commit=<sha> --agent-kwarg max_budget_usd=<usd> \\
      [--agent-kwarg title=... test_command=... model_access=api model=... repo_dir=...]

Harbor's own ClaudeCode agent pipes the task text straight to `claude --print`,
which tests plain Claude Code. This subclass keeps its install and stream-json
handling, and around `claude` it:
  - install(): packs the harness at `harness_commit` on the host
    (install_harness.py) and uploads it with setup_task.sh / finish_task.sh;
  - run(): runs setup_task.sh (tools, `exam: base`, harness commit, bare
    origin, one bead from the instruction, EXAM_MODE), then `claude -p
    "/work <bead>"`, then finish_task.sh (checks the bead branch out so the
    hidden tests grade it; writes end.sha/diff.patch);
  - passes only trial_export.sandbox_env() into the sandbox (Bedrock by default);
  - post-run: sets cost_usd from total_cost_usd (includes subagents) and writes
    logs/exam/result.json.
Trial logs: agent/claude-code.txt (stream-json), agent/sessions/projects/**
(parent + subagent transcripts), agent/tries/ (coordinator try folders),
agent/exam/ (start.sha, end.sha, diff.patch, base.sha, bead.txt, branch.txt,
result.json; `trial_export.py finalize <trial>` adds gates.txt after verify).
"""
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

EXAM_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAM_DIR))
import trial_export  # noqa: E402

AW_ROOT = EXAM_DIR.parent.parent
INSTALLER = EXAM_DIR.parent / "install_harness.py"
REMOTE = PurePosixPath("/installed-agent/exam")


class WorkflowAgent(ClaudeCode):
    def __init__(self, logs_dir: Path, *args, harness_commit: str = "HEAD",
                 harness_addon: str | None = None, model_access: str = "bedrock",
                 model: str | None = None, title: str | None = None,
                 test_command: str | None = None, run_name: str | None = None,
                 repo_dir: str | None = None, **kwargs):
        # Popped before ClaudeCode: its option model rejects unknown kwargs.
        self._harness_commit = harness_commit
        self._harness_addon = harness_addon
        self._model_access = model_access
        self._title = title
        self._test_command = test_command or ""
        self._run_name = run_name or Path(logs_dir).parent.name
        self._repo_dir = repo_dir
        self._bead: str | None = None
        self._model = model
        super().__init__(logs_dir, *args, **kwargs)
        if not self._model and self.model_name:  # harbor -m provider/model
            self._model = self.model_name.split("/")[-1]
        self._resolve_auth_env()  # fail on missing keys before any sandbox spend

    @staticmethod
    def name() -> str:
        return "agent-workflow"

    @property
    def _exam_logs(self) -> PurePosixPath:
        return self.environment_logs_dir / "exam"

    def _resolve_auth_env(self) -> dict[str, str]:
        host = {**os.environ, **self._extra_env}
        return trial_export.sandbox_env(host, self._model_access,
                                        tries_dir=self.environment_logs_dir / "tries",
                                        model=self._model)

    def _resolved_model_name(self) -> str | None:
        return self._resolve_auth_env()["ANTHROPIC_MODEL"]

    def _pack_harness(self, out_dir: Path) -> Path:
        target = out_dir / "harness"
        cmd = [sys.executable, str(INSTALLER), "--repo", str(AW_ROOT),
               "--commit", self._harness_commit, "--target", str(target)]
        if self._harness_addon:
            cmd += ["--addon", self._harness_addon]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        tarball = out_dir / "harness.tgz"
        with tarfile.open(tarball, "w:gz") as tar:
            for name in (".claude", "AGENTS.md"):
                tar.add(target / name, arcname=name)
        return tarball

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(environment, f"mkdir -p {REMOTE} && chmod 777 {REMOTE}")
        with tempfile.TemporaryDirectory(prefix="exam-harness-") as tmp:
            tarball = self._pack_harness(Path(tmp))
            await environment.upload_file(tarball, (REMOTE / "harness.tgz").as_posix())
        for script in ("setup_task.sh", "finish_task.sh"):
            await environment.upload_file(EXAM_DIR / script, (REMOTE / script).as_posix())

    async def _repo(self, environment: BaseEnvironment) -> str:
        if not self._repo_dir:  # the agent runs in the task workdir
            self._repo_dir = (await self.exec_as_agent(environment, "pwd")).stdout.strip()
        return self._repo_dir

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        # Bead creation needs the instruction, which only run() receives, so
        # setup_task.sh runs here rather than in install().
        repo = await self._repo(environment)
        with tempfile.TemporaryDirectory(prefix="exam-instruction-") as tmp:
            local = Path(tmp) / "instruction.md"
            local.write_text(instruction)
            await environment.upload_file(local, (REMOTE / "instruction.md").as_posix())
        title = self._title or instruction.strip().splitlines()[0][:80]
        args = [repo, REMOTE / "harness.tgz", title, REMOTE / "instruction.md",
                self._test_command, self._run_name, self._exam_logs]
        result = await self.exec_as_agent(
            environment, "bash " + " ".join(shlex.quote(str(a)) for a in [REMOTE / "setup_task.sh", *args]),
            timeout_sec=600)
        self._bead = result.stdout.strip().splitlines()[-1]
        try:
            await super().run(f"/work {self._bead}", environment, context)
        finally:
            await self.exec_as_agent(
                environment,
                f"bash {REMOTE / 'finish_task.sh'} {shlex.quote(repo)} {self._exam_logs}",
                timeout_sec=300)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        result = trial_export.stream_result(self.logs_dir / "claude-code.txt")
        if result["total_cost_usd"] is not None:
            context.cost_usd = float(result["total_cost_usd"])
        exam = self.logs_dir / "exam"
        exam.mkdir(parents=True, exist_ok=True)
        branch = exam / "branch.txt"
        (exam / "result.json").write_text(json.dumps({
            "bead": self._bead,
            "branch": branch.read_text().strip() if branch.is_file() else None,
            **result,
        }, indent=2) + "\n")
