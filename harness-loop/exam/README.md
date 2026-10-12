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

Harbor redacts secrets from the trial dir only when they are agent env, which is what
`--ae KEY=VALUE` sets. Because the keys come from the host env instead, `WorkflowAgent.__init__`
adds them to its agent env itself, so the scrubber covers them without `--ae`. Passing them with
`--ae` as well is harmless but puts the values on the command line.

```bash
PYTHONPATH=harness-loop/exam harness-loop/.venv/bin/harbor run \
  --agent workflow_agent:WorkflowAgent \
  --path harness-loop/tasks/cache/<task> --env daytona \
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
main clone. A task asking for more than 4 CPUs or 8 GiB gets `--override-cpus 4
--override-memory-mb 8192`, Daytona's cap.
