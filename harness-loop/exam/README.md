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

`--env docker` runs the task on this machine; `--env daytona` rents a cloud sandbox (see "Where tasks
run" below). Model access goes through Bedrock by default; export the Bedrock-only
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and `AWS_REGION`, default `us-east-1`) on the host.
Only those, the region and the `global.anthropic.claude-*-5-5` model ids enter the sandbox
(`trial_export.sandbox_env`). `model_access=api` sends `ANTHROPIC_API_KEY` instead and nothing else.

Harbor redacts secrets from the trial dir only when they are agent env, which is what
`--ae KEY=VALUE` sets. Because the keys come from the host env instead, `WorkflowAgent.__init__`
adds them to its agent env itself, so the scrubber covers them without `--ae`. Passing them with
`--ae` as well is harmless but puts the values on the command line.

```bash
PYTHONPATH=harness-loop/exam harness-loop/.venv/bin/harbor run \
  --agent workflow_agent:WorkflowAgent \
  --path harness-loop/tasks/cache/<task> --env docker --delete \
  --agent-kwarg harness_commit=<sha> --agent-kwarg max_budget_usd=<usd> \
  --agent-kwarg title="<manifest title>" --agent-kwarg test_command="<manifest test_command>"
harness-loop/exam/trial_export.py finalize <trial dir>   # after verify: writes agent/exam/gates.txt
```

Other kwargs: `harness_addon=<dir>` (passed to install_harness.py), `model=<id>` (overrides
`ANTHROPIC_MODEL`), `run_name=<name>` (written to `.claude/EXAM_MODE`; default the trial name).
`workdir=<manifest workdir>` names the task repo; without it the repo is the image's WORKDIR.
They differ for betterleaks (WORKDIR `/app`, repo `/app/betterleaks`), so always pass it. Every
exec, `claude` included, then runs in that directory. Harbor
JSON-parses kwarg values, so the agent turns numbers and booleans back into text; pass
`harness_commit` as a full 40-character sha, since a short all-digit one with an `e` in it would
arrive as a float. The sha is resolved when the agent is built and recorded in `result.json`.

## What happens in the sandbox

| Step | Where | What |
|------|-------|------|
| install | `WorkflowAgent.install` | Claude Code install; harness packed on the host by `install_harness.py --commit`, uploaded with the two scripts |
| tools (install, root) | `setup_task.sh tools` | jq/git/python3/curl/tar and pinned dolt 1.83.4 + bd 0.59.0 if missing |
| repo (install, agent user) | `setup_task.sh repo` | fails on workdir `/` or an unwritable parent dir (sibling worktrees go there); task state committed on `main` as `exam: base`; `bd init`; harness committed as `exam: harness` (a tracked `.claude/` moves to `.claude-repo/`, `AGENTS.md` to `AGENTS.repo.md`, CLAUDE.md gets a Quality Gates row); `main` pushed to the bare `/tmp/origin.git` |
| bead (run) | `setup_task.sh bead` | one bead from the task text; `.claude/EXAM_MODE` |
| run | `claude -p "/work <bead>"` | env adds `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` and `HARNESS_TRIES_DIR=/logs/agent/tries`; `--max-budget-usd` from the kwarg |
| finish (always) | `finish_task.sh` | fetches origin and checks the bead branch out detached, so the hidden tests grade it; with no branch it resets to `exam: harness`; then `git clean -fd`, so stray edits in the main root are never graded |

The harness has to be committed rather than git-excluded: the coordinator makes each worktree off
`origin/main`, and implementers read the skills and the Quality Gates table inside that worktree.
Only the bead step runs in `run()`, because only `run()` receives the task text the bead is made
from; everything else runs during Harbor's setup.

## Trial outputs

- `agent/claude-code.txt`: the stream-json; its final `result` line has `total_cost_usd`, which
  includes subagents (the `usage` field does not). `context.cost_usd` is set from it.
- `agent/sessions/projects/**`: parent and subagent transcripts (Harbor points `CLAUDE_CONFIG_DIR`
  into the logs dir, so nothing is copied).
- `agent/tries/`: the coordinator's own try folders.
- `agent/exam/`: `base.sha`, `start.sha` (`exam: harness`), `end.sha`, `diff.patch` (`--binary`),
  `bead.txt`, `branch.txt`, `origin_url.txt`, `transcript.jsonl` (the parent session, found by the
  stream's init `session_id`) and `result.json`: bead, branch, `origin_url`, the resolved
  `harness_commit` sha, `status` (`ok`, `budget_cut` when the result subtype is
  `error_max_budget_usd`, else `error`), `subtype`, `total_cost_usd` (null with no result line),
  `exam_no_pr_line` (the final message has `EXAM_MODE: no PR` on its own line), `session_id`,
  `transcript`. After `finalize` it also holds `gates.txt`, so the folder is a
  `save_try.py --from-files` input.

## Network

Harbor's default network mode is public. None of the 105 Monthly-SWEBench 2026-05 tasks or the 6
Terminal-Bench 4.0 Software picks set `network_mode` in `task.toml`, so none needs an override. A
task that restricts the agent phase would need an allowlist with
`bedrock-runtime.<region>.amazonaws.com`, github.com (bd/dolt downloads) and its package registries.

## Where tasks run

`run_exam.py --env docker` is the default: Harbor builds each task's container with the local
Docker engine, so a run costs no sandbox rent. `--env daytona` sends it to a rented Daytona sandbox
instead (needs `DAYTONA_API_KEY`); use it when the laptop is busy or a task cannot build locally.
Either way every task x try gets a fresh container that Harbor deletes afterwards (`--delete`, which
also removes the built image). With docker, `run_exam.py` then removes any container or network of
that trial Harbor left behind (it matches the trial's compose project name), so a crashed Harbor
does not leave containers running. `docker ps -a` is empty after a run.

Local setup: Docker Desktop running. `run_exam.py` asks `docker info` first and stops with a
message, before any run, if the engine is down or does not answer within 60 s. Docker Desktop's
Resource Saver stops its VM after 5 idle minutes; the first call wakes it in about a second.
Task containers are built for the host's CPU: the base images are multi-arch, so on an
Apple-silicon Mac they build natively as arm64. A task whose Dockerfile downloads x86-64 binaries
cannot build that way (none in the shakedown, hard or TB4 sets does; six other Monthly-SWEBench
tasks do); run the controls on a new task first and send it to `--env daytona` if oracle is not 1.

Concurrency: each task asks for 2 CPUs and 4 GiB (one TB4 task asks for more and is capped to what
Docker has). `--concurrency` defaults to `local_concurrency`: as many tasks as fit in the CPUs and
memory Docker reports, keeping 1 GiB for the engine, at least 1. On a 10-CPU / 16 GiB Mac with
Docker Desktop's default memory (7.65 GiB) that is 1. Giving Docker 10 GiB (Settings > Resources,
which leaves 6 GiB to macOS) makes it 2; 14 GiB would make it 3 but leaves too little for anything
else. On daytona the default stays 1.

Env for Harbor: an allowlist (`HARBOR_ENV`), plus the Bedrock keys only for WorkflowAgent runs
(oracle and nop never call a model), plus `DAYTONA_*` on daytona or `DOCKER_HOST` /
`DOCKER_CONTEXT` / `DOCKER_CONFIG` on docker. Nothing else from the host reaches Harbor.

## Run the exam

`run_exam.py` runs a manifest's tasks through Harbor at one harness sha and seals the results under
`~/.harness-loop/runs/<run-id>/`; `compare.py` compares two runs with a luck margin. Controls and
the planted-harm branch are in `controls.md`.

```bash
harness-loop/exam/run_exam.py --harness main --tasks harness-loop/tasks/shakedown-2026-05.json \
  --screen --kind baseline --budget-usd 60          # per-run cap: --max-budget-usd (default 12.05)
harness-loop/exam/run_exam.py --harness main --tasks <same> --screen --agent controls --budget-usd 1
harness-loop/exam/compare.py <runA> <runB> --margin-from <runA2> --screen-rule
```

Scoring: in the baseline, `budget_cut` and `error` tries are left out; in the candidate they count
as unsolved at the full cap. The screen also fails on more candidate errors or an incomplete
candidate run (details in `controls.md`). Harbor gets only an allowlisted host env (`HARBOR_ENV` in
`run_exam.py`), so whoever launches a run cannot change what it measures.

Task folders come from the manifest's `cache/` (git-ignored); in a worktree, symlink it from the
main clone. A task asking for more CPUs or memory than one sandbox has is capped to it: on daytona
4 CPUs and 8 GiB, on docker the engine's CPUs and its memory less 1 GiB.
