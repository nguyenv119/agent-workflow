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
run" below). Model access follows `HARNESS_MODEL_ACCESS`. Unset, it is the Claude Code
subscription: a sandbox cannot read this machine's keychain login, so run `claude setup-token` once
and export the token it prints as `CLAUDE_CODE_OAUTH_TOKEN`; only that token and a first-party model
id enter the sandbox, and run_exam refuses to start without it. `HARNESS_MODEL_ACCESS=bedrock` is
the paid opt-in: export the Bedrock-only `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and
`AWS_REGION`, default `us-east-1`); only those, the region and the `global.anthropic.claude-*-5-5`
model ids enter the sandbox (`trial_export.sandbox_env`). run_exam never switches route on a failure. A try
that hits a usage limit is `rate_limited`: the run stops starting tries and is marked incomplete with
run.json `rate_limited`, so the limit never scores as an unsolved task. `HARNESS_MODEL_ACCESS=api`
(run_exam only, never the loop's own route) sends `ANTHROPIC_API_KEY` instead and nothing else.
`loop.py` picks the exam's route itself when it runs on the subscription: the token, then Bedrock,
then the API key, whichever first answers a one-call preflight. A missing or failing token therefore
moves the exam to a paid route; only a token at its usage limit pauses the run instead
(`loop.py exam-route` shows the choice).

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
the model host (`api.anthropic.com` on the subscription, `bedrock-runtime.<region>.amazonaws.com`
on Bedrock), github.com (bd/dolt downloads) and its package registries.

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
The tools `setup_task.sh` installs into the container (dolt, bd) are pinned per architecture:
a linux amd64 and a linux arm64 release tarball, each with its upstream sha256, chosen by `uname -m`.
`setup_task.sh pin <dolt|bd>` prints the url, sha256 and tarball member it would use; on any other
machine it stops naming the tool and the machine.

Concurrency: each task asks for 2 CPUs and 4 GiB (one TB4 task asks for more and is capped to what
Docker has). `--concurrency` defaults to `local_concurrency`: as many tasks as fit in the CPUs and
memory Docker reports, keeping 1 GiB for the engine, at least 1. On a 10-CPU / 16 GiB Mac with
Docker Desktop's default memory (7.65 GiB) that is 1. Giving Docker 10 GiB (Settings > Resources,
which leaves 6 GiB to macOS) makes it 2; 14 GiB would make it 3 but leaves too little for anything
else. On daytona the default stays 1.

Env for Harbor: an allowlist (`HARBOR_ENV`), plus the route's credential (`CLAUDE_CODE_OAUTH_TOKEN`,
or the Bedrock keys) only for WorkflowAgent runs
(oracle and nop never call a model), plus `DAYTONA_*` on daytona or `DOCKER_HOST` /
`DOCKER_CONTEXT` / `DOCKER_CONFIG` on docker. Nothing else from the host reaches Harbor.

## Canary (a short exam for harness PRs)

`agent-cli harness canary --pr N` screens one harness PR on three cheap tasks
(`tasks/canary-2026-10.json`) against main on the plan token only, reruns the candidate once on a screen
fail, and posts commit status `harness-canary` on the PR head: success "not needed (no loaded file)" when
the path gate finds nothing /work loads, success "pass solved B/n vs main A/n", failure "fail (rerun)
solved B/n vs main A/n", or error "could not run: <reason>". The canary passes only when the candidate
solves at least as many tasks as main (`compare.py --solved-slack 0`, and `run_exam.py --early-stop-slack 0`
for the early stop; a round or plain exam allows one fewer); the cost, cut, error and complete rules are the
round's. On three tasks one lost task is a third of the signal, so it fails, and the one rerun absorbs a
flake. In agent-workflow it runs
`loop.py exam --pr N --tasks tasks/canary-2026-10.json --subscription-only --rerun-on-fail 1
--post-status`; in a repo that installs the harness it builds the candidate as the PR would land on main
(`git merge-tree`; a conflict is failure "stale, rebase the PR onto main", exit 1), clones agent-workflow
at that tree's pinned commit (`HARNESS_CANARY_SOURCE` / `HARNESS_CANARY_REF` override where and which),
and runs `loop.py exam --candidate-installed <merged tree> --baseline-installed <main tree>`
(`run_exam.py --installed`) with `HARNESS_LOOP_HOME` defaulting to `~/.harness-loop-private/<repo>` (an
inherited value wins). `--candidate DIR --baseline DIR` compares two installed trees and posts nothing.
Identical trees are "not needed (installed trees identical)"; a candidate tree that fails the free checks
is failure "dry run fail (...)"; a tree holding a `.env`, `.env.*`, `*.bak` or `settings.local.json` under
`.claude` is refused (exit 2), and in a git work tree only tracked files are packed. loop.py runs
`tasks/fetch_cache.py` (task folders and `.venv`) only after the gate, the token check and Docker, so a
"not needed" PR downloads nothing.

Before it starts:

1. Docker running (`docker info`; down is exit 5 with status error).
2. The plan token: export `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, a human step), or put
   it in a file such as `~/.config/harness/exam.env` and name that file in `HARNESS_EXAM_ENV_FILE` or in
   an `exam-env-file: <path>` line under `## harness` in the installing repo's project-rules.md. Only that
   one line is read, and the value goes to the preflight and run_exam subprocesses, never to a log.
   Without a working token the canary exits 4; it never falls back to Bedrock or an API key.

It refuses two cases outright, posting no status and starting nothing (exit 5): a cloud session
(`CLAUDE_CODE_REMOTE` set: the canary is laptop-only, so a laptop picks the PR up later) and a fork PR
(the sandbox holds the plan token and runs the PR's harness, so a fork's head could read it).

| exit | meaning |
|------|---------|
| 0 | pass, or not needed |
| 1 | fail after the one rerun, stale, or a free check failed |
| 2 | usage error or refused config (e.g. an installed tree with an add-on and the default `~/.harness-loop`) |
| 3 | `agent-cli harness canary` only: gh could not read the PR |
| 4 | paused: usage limit, or no working plan token |
| 5 | could not run: laptop-only, fork PR, gh or git failed, the PR moved, Docker down, clone or task fetch failed, both runs (or the rerun) errored, the exam crashed, the PR body edit failed (the status is still posted) |

Callers treat 5 like 4: leave the PR open and retry later.

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

`--screen --early-stop-vs <sealed baseline run>` runs the tasks cheapest first by the baseline's
per-task cost, runs the first (cheapest) try alone as a paid dry run, and stops starting new ones
once the screen rule can no longer pass (more cut or errored tries than the baseline, too few solves
left, or cost past 1.25x, `compare.SCREEN_COST_RATIO`). The run is then incomplete, with the
reason in run.json `early_stop`. `loop.py` passes it for every candidate screen.
`--early-stop-slack N` is the solved slack it uses, matching `compare.py --solved-slack N` (default
1, `compare.SCREEN_SOLVED_SLACK`; the canary passes 0).

Scoring: in the baseline, `budget_cut` and `error` tries are left out; in the candidate they count
as unsolved at the full cap. The screen also fails on more candidate errors or an incomplete
candidate run (details in `controls.md`). Harbor gets only an allowlisted host env (`HARBOR_ENV` in
`run_exam.py`), so whoever launches a run cannot change what it measures.

A private task set (built with `tasks/build_from_pr.py`) runs with its add-on:
`HARNESS_LOOP_HOME=<private dir> run_exam.py --tasks <private manifest> --addon <add-on dir>`. An add-on run
refuses the default `~/.harness-loop`, whose `runs/` the improver reads. The add-on reaches the
preflight install and WorkflowAgent (`harness_addon`); list the add-on's task folders in its
`.installignore` so they never enter the sandbox. The installed `HARNESS_VERSION` (commit and
`addon_sha256`) is recorded as `harness_version` in run.json; each workflow result.json
records the version that try's own install wrote, and a try whose version differs from the
preflight's is status `error`.

Task folders come from the manifest's `cache/` (git-ignored); in a worktree, symlink it from the
main clone. A task asking for more CPUs or memory than one sandbox has is capped to it: on daytona
4 CPUs and 8 GiB, on docker the engine's CPUs and its memory less 1 GiB.
