"""Harbor agent that runs one exam task through agent-workflow's real `/work`.

    PYTHONPATH=harness-loop/exam harbor run --agent workflow_agent:WorkflowAgent \\
      --path <task dir> --env docker \\
      --agent-kwarg harness_commit=<sha> --agent-kwarg max_budget_usd=<usd> \\
      [--agent-kwarg title=... test_command=... workdir=... model_access=api model=...]

Harbor's own ClaudeCode agent pipes the task text straight to `claude --print`,
which tests plain Claude Code. This subclass keeps its install and stream-json
handling, and around `claude` it:
  - install(): packs the harness at `harness_commit` on the host
    (install_harness.py), uploads it with setup_task.sh / finish_task.sh, then
    runs `setup_task.sh tools` as root and `setup_task.sh repo` as the agent
    user (`exam: base`, `exam: harness`, bare origin) in the task workdir;
  - run(): `setup_task.sh bead` (one bead from the instruction, EXAM_MODE),
    marks the workdir trusted in claude's config (else project settings are
    ignored), then `claude -p "/work <bead>"` in the workdir, then finish_task.sh
    (checks the bead branch out so the hidden tests grade it; writes
    end.sha/diff.patch), even when claude fails;
  - passes only trial_export.sandbox_env() into the sandbox (Bedrock by default)
    and registers its secrets as agent env so Harbor's trial-dir scrubber
    redacts them;
  - post-run: sets cost_usd from total_cost_usd (includes subagents) and writes
    logs/exam/result.json (with the HARNESS_VERSION install() packed) +
    transcript.jsonl (trial_export.write_result).
Trial logs: agent/claude-code.txt (stream-json), agent/sessions/projects/**
(parent + subagent transcripts), agent/tries/ (coordinator try folders),
agent/exam/ (start.sha, end.sha, diff.patch, base.sha, bead.txt, branch.txt,
origin_url.txt, transcript.jsonl, result.json; `trial_export.py finalize
<trial>` adds gates.txt after verify).
"""
import json
import os
import re
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
# Kwargs this agent owns; ClaudeCode's option model rejects unknown ones.
OWN_KWARGS = ("harness_commit", "harness_addon", "model_access", "model", "title",
              "test_command", "run_name", "workdir")
# sandbox_env values Harbor's scrubber must know about (it only reads agent env).
SECRET_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY")


def trust_cmd(config_dir, repo) -> str:
    """Mark `repo` trusted in claude's config. Untrusted, `claude -p` ignores
    the project's .claude/settings.json permissions (seen on Daytona)."""
    f = shlex.quote(f"{config_dir}/.claude.json")
    return (f"mkdir -p {shlex.quote(str(config_dir))} && {{ [ -s {f} ] || echo '{{}}' > {f}; }} && "
            f"jq --arg p {shlex.quote(repo)} '.projects[$p].hasTrustDialogAccepted = true' {f} > {f}.tmp "
            f"&& mv {f}.tmp {f}")


def _text(value):
    """--agent-kwarg values arrive JSON-parsed (`1234567` -> int, `true` -> bool)."""
    if value is None or isinstance(value, str):
        return value
    return str(value).lower() if isinstance(value, bool) else str(value)


def resolve_commit(ref):
    return subprocess.run(["git", "-C", str(AW_ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                          check=True, capture_output=True, text=True).stdout.strip()


class WorkflowAgent(ClaudeCode):
    def __init__(self, logs_dir: Path, *args, harness_commit="HEAD", harness_addon=None,
                 model_access="bedrock", model=None, title=None, test_command=None,
                 run_name=None, workdir=None, **kwargs):
        # Resolved now: the run records the sha that ran, and HEAD may move.
        self._harness_commit = resolve_commit(_text(harness_commit))
        self._harness_addon = _text(harness_addon)
        self._model_access = _text(model_access)
        self._title = _text(title)
        self._test_command = _text(test_command) or ""
        self._run_name = _text(run_name) or Path(logs_dir).parent.name
        self._model = _text(model)
        # The repo dir; None = the image's WORKDIR (betterleaks' is /app, its repo /app/betterleaks).
        self._repo: str | None = _text(workdir)
        # Harbor's Daytona backend puts the exec cwd into `cd {cwd}` unquoted.
        if self._repo is not None and not re.fullmatch(r"/[A-Za-z0-9._/-]+", self._repo):
            raise ValueError(f"workdir must be an absolute path of [A-Za-z0-9._/-]: {self._repo!r}")
        self._bead: str | None = None
        self._harness_version: dict | None = None  # the installed .claude/HARNESS_VERSION
        # In -p mode a pending ScheduleWakeup keeps claude alive after /work is
        # done and re-sends its prompt when it fires (first Daytona run: ~29 min
        # idle, then a second `/work` turn). Background-agent completions
        # already wake the coordinator, so the tool is not needed.
        kwargs["disallowed_tools"] = ",".join(
            filter(None, [_text(kwargs.get("disallowed_tools")), "ScheduleWakeup"]))
        super().__init__(logs_dir, *args, **kwargs)
        if not self._model and self.model_name:  # harbor -m provider/model
            self._model = self.model_name.split("/")[-1]
        env = self._resolve_auth_env()  # fails on missing keys before any sandbox spend
        # Like `--ae`: Trial._scrub_jobs_dir redacts only agent extra_env values.
        self._extra_env.update({k: env[k] for k in SECRET_KEYS if k in env})

    @classmethod
    def preflight(cls, kwargs=None, env=None) -> None:
        super().preflight({k: v for k, v in (kwargs or {}).items() if k not in OWN_KWARGS}, env)

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
        self._harness_version = json.loads((target / ".claude/HARNESS_VERSION").read_text())
        tarball = out_dir / "harness.tgz"
        with tarfile.open(tarball, "w:gz") as tar:
            for name in (".claude", "AGENTS.md"):
                tar.add(target / name, arcname=name)
        return tarball

    @staticmethod
    def _setup_cmd(*args) -> str:
        return "bash " + " ".join(shlex.quote(str(a)) for a in (REMOTE / "setup_task.sh", *args))

    async def _exec(self, environment, command, user=None, env=None, cwd=None, timeout_sec=None):
        # ClaudeCode launches claude without a cwd; default every exec to the
        # repo so /work runs there even when it is not the image's WORKDIR.
        return await super()._exec(environment, command, user=user, env=env,
                                   cwd=cwd or self._repo, timeout_sec=timeout_sec)

    async def _workdir(self, environment: BaseEnvironment) -> str:
        if not self._repo:
            self._repo = (await self.exec_as_agent(environment, "pwd")).stdout.strip()
        return self._repo

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(environment, f"mkdir -p {REMOTE} && chmod 777 {REMOTE}")
        with tempfile.TemporaryDirectory(prefix="exam-harness-") as tmp:
            tarball = self._pack_harness(Path(tmp))
            await environment.upload_file(tarball, (REMOTE / "harness.tgz").as_posix())
        for script in ("setup_task.sh", "finish_task.sh"):
            await environment.upload_file(EXAM_DIR / script, (REMOTE / script).as_posix())
        await self.exec_as_root(environment, self._setup_cmd("tools"), timeout_sec=300)
        repo = await self._workdir(environment)
        await self.exec_as_agent(environment, self._setup_cmd(
            "repo", repo, REMOTE / "harness.tgz", self._test_command, self._exam_logs), timeout_sec=300)

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        # Bead creation needs the instruction, which only run() receives.
        repo = await self._workdir(environment)
        with tempfile.TemporaryDirectory(prefix="exam-instruction-") as tmp:
            local = Path(tmp) / "instruction.md"
            local.write_text(instruction)
            await environment.upload_file(local, (REMOTE / "instruction.md").as_posix())
        title = self._title or instruction.strip().splitlines()[0][:80]
        result = await self.exec_as_agent(environment, self._setup_cmd(
            "bead", repo, title, REMOTE / "instruction.md", self._run_name, self._exam_logs),
            timeout_sec=120)
        self._bead = result.stdout.strip().splitlines()[-1]
        try:
            await self.exec_as_agent(environment, trust_cmd(self.environment_logs_dir / "sessions", repo))
            await super().run(f"/work {self._bead}", environment, context)
        finally:
            await self.exec_as_agent(
                environment,
                f"bash {REMOTE / 'finish_task.sh'} {shlex.quote(repo)} {self._exam_logs}",
                timeout_sec=300)

    def populate_context_post_run(self, context: AgentContext) -> None:
        super().populate_context_post_run(context)
        result = trial_export.write_result(self.logs_dir, self._bead, self._harness_commit,
                                           self._harness_version)
        if result["total_cost_usd"] is not None:
            context.cost_usd = float(result["total_cost_usd"])
