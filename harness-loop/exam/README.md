# Exam runner: one task through `/work` in a sandbox

Harbor's built-in `claude_code` agent pipes the task text to `claude --print`, so it measures plain
Claude Code. `WorkflowAgent` runs the same task through agent-workflow's own `/work` flow instead:
it installs the harness at a chosen commit into the task repo, turns the task text into a bead, and
runs `claude -p "/work <bead>"` in exam mode. Harbor's verifier then runs the task's hidden tests
on the branch `/work` built.

## Setup (once)

Harbor needs Python 3.12 or newer.

```bash
python3 -m venv harness-loop/.venv
harness-loop/.venv/bin/pip install -r harness-loop/exam/requirements.txt
```

## Run one task

Task images are amd64, so run them in a cloud sandbox (Daytona, Modal, E2B), not emulated Docker on
an Apple-silicon Mac. Model access goes through Bedrock by default; export the Bedrock-only
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `AWS_REGION`, default `us-east-1`) on the host.
Only those, the region and the `global.anthropic.claude-*-5-5` model ids enter the sandbox
(`trial_export.sandbox_env`). `model_access=api` sends `ANTHROPIC_API_KEY` instead and nothing else.

```bash
PYTHONPATH=harness-loop/exam harness-loop/.venv/bin/harbor run \
  --agent workflow_agent:WorkflowAgent \
  --path harness-loop/tasks/cache/<task> --env daytona \
  --agent-kwarg harness_commit=<sha> --agent-kwarg max_budget_usd=<usd> \
  --agent-kwarg title="<manifest title>" --agent-kwarg test_command="<manifest test_command>"
harness-loop/exam/trial_export.py finalize <trial dir>   # after verify: writes agent/exam/gates.txt
```

Other kwargs: `harness_addon=<dir>` (passed to install_harness.py), `model=<id>` (overrides
`ANTHROPIC_MODEL`), `run_name=<name>` (written to `.claude/EXAM_MODE`; default the trial name),
`repo_dir=<path>` (default: the container's workdir).

## What happens in the sandbox

| Step | Where | What |
|------|-------|------|
| install | `WorkflowAgent.install` | Claude Code install; harness packed on the host by `install_harness.py --commit`, uploaded with the two scripts |
| setup | `setup_task.sh` | jq/git/python3 and pinned dolt 1.83.4 + bd 0.59.0 if missing; task state committed on `main` as `exam: base`; `bd init`; harness committed as `exam: harness` (a tracked `.claude/` moves to `.claude-repo/`, `AGENTS.md` to `AGENTS.repo.md`, CLAUDE.md gets a Quality Gates row); `main` pushed to the bare `/tmp/origin.git`; one bead; `.claude/EXAM_MODE` |
| run | `claude -p "/work <bead>"` | env adds `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` and `HARNESS_TRIES_DIR=/logs/agent/tries`; `--max-budget-usd` from the kwarg |
| finish | `finish_task.sh` | fetches origin and checks the bead branch out detached, so the hidden tests grade it; with no branch the workdir stays at `exam: harness` |

The harness has to be committed rather than git-excluded: the coordinator makes each worktree off
`origin/main`, and implementers read the skills and the Quality Gates table inside that worktree.
`setup_task.sh` runs in `run()`, not `install()`, because only `run()` receives the task text the
bead is made from.

## Trial outputs

- `agent/claude-code.txt`: the stream-json; its final `result` line has `total_cost_usd`, which
  includes subagents (the `usage` field does not). `context.cost_usd` is set from it.
- `agent/sessions/projects/**`: parent and subagent transcripts (Harbor points `CLAUDE_CONFIG_DIR`
  into the logs dir, so nothing is copied).
- `agent/tries/`: the coordinator's own try folders.
- `agent/exam/`: `base.sha`, `start.sha` (`exam: harness`), `end.sha`, `diff.patch`, `bead.txt`,
  `branch.txt`, `result.json` (bead, branch, `total_cost_usd`, result `subtype`). After
  `finalize` it also holds `gates.txt`, so the folder is a `save_try.py --from-files` input.

## Network

Harbor's default network mode is public. None of the 105 Monthly-SWEBench 2026-05 tasks or the 6
Terminal-Bench 4.0 Software picks set `network_mode` in `task.toml`, so none needs an override. A
task that restricts the agent phase would need an allowlist with
`bedrock-runtime.<region>.amazonaws.com`, github.com (bd/dolt downloads) and its package registries.
