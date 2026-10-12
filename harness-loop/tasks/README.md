# Exam task manifests

A manifest is one JSON file per task set: `{"set", "source", "tasks": [...]}`. Each task row has
`id, harbor_path, repo, pr, merged_at, language, kind (bugfix|feature), workdir, test_command, title, screen`.
`repo`/`pr`/`merged_at` are null for tasks not taken from a PR; `test_command` is the VISIBLE test command
the agent may run (null = "no visible tests"), never the hidden verifier tests.

Validate: `python3 harness-loop/tasks/manifest.py [--require-cache] harness-loop/tasks/<set>.json` prints a
one-line summary ending `leak-checked k/n` (tasks whose hidden tests were on disk to check), or fails naming
the task id. `--require-cache` fails unless every task folder is downloaded. Sets are named
`<source>-<slice>` (e.g. `tb4-software`, `shakedown-2026-05`).
`harbor_path` is relative to the manifest's own folder, under `cache/` (git-ignored; `load()` adds the
absolute `task_dir`). Cache layout: `cache/terminal-bench-v4.0.0/tasks/<id>` (sparse checkout of the tag)
and `cache/monthly-swebench-2026-05/{bugfix,non_bugfix}/<id>` (the two dataset archives, unpacked).

`payments-pipeline-fix` asks for 6 CPUs; Daytona caps a sandbox at 4, so run it with
`--override-cpus 4 --override-memory-mb 8192` (oracle passes that way). `run_exam.py` adds the
override itself, capped to the sandbox it runs in (Daytona, or what local Docker has).

## Fresh tasks (rebuilt monthly)

`fresh-<YYYY-MM>.json` holds tasks built with SWE-gen from public PRs merged after the
model's training cutoff, so a score on them can't come from a remembered answer. Rebuild
once a month (the window is the last three months; `--to` names the set):

```
uv venv harness-loop/.venv --python 3.12                                         # once
VIRTUAL_ENV=harness-loop/.venv uv pip install -r harness-loop/tasks/fresh/requirements.txt
export PATH="$PWD/harness-loop/.venv/bin:$PATH"                                  # swegen + harbor
python3 harness-loop/tasks/fresh/pick_prs.py --from 2026-08-01 --to 2026-10-31   # free
harness-loop/tasks/fresh/build.sh                                                # prints count + cost, spends nothing
harness-loop/tasks/fresh/build.sh --limit 3 --confirm-budget <usd>               # paid: small first batch
harness-loop/tasks/fresh/build.sh --confirm-budget <usd>                         # paid: the rest
```

`pick_prs.py` (GitHub API only) keeps merged PRs in the window whose every linked issue
was opened on/after `--issue-since` (default 2026-07-01), in Python or TypeScript repos
with >= 500 stars, touching tests and 3-10 non-test source files, at most 3 per repo,
balanced across language x kind (bugfix if the PR or its issue has a bug-like label,
else feature). `build.sh` refuses to start until `--confirm-budget` covers the estimate,
count x `--per-task-usd` (default $12). The estimate is not a cap: after each task
`build.sh` adds that task's cost to a running total (in `cache/fresh-<YYYY-MM>/spend.tsv`)
and starts no new task once the total reaches `--confirm-budget`; the total includes
earlier runs of the same month. A task's cost is the `total_cost_usd` the Claude session
reports (the shim prints it) plus Daytona time at $0.34/hour; if the log has no session
cost, it is elapsed time x `--usd-per-hour` (default $13.50/hour, i.e. $12 for SWE-gen's
default 3200 s session timeout). Each Claude session is also capped at `--cc-max-usd`
(default $8, passed as the Agent SDK's `max_budget_usd` through the shim), so one task
cannot run past it. Stopped runs resume: ids in `passed.txt` / `failed.txt` there are
skipped.

Unlike the other sets, fresh tasks are committed: the manifest step copies each passing
task from the git-ignored cache to `fresh-<YYYY-MM>/<id>/`, and `harbor_path` points
there. They come from public PRs only. SWE-gen writes `network_mode = "no-network"` in
`task.toml`, which also cuts off the agent phase, and neither Daytona nor Harbor's
default docker setup can switch the network per phase. The copy opens it to `"public"`
(Harbor's default, as in every other exam set) with a comment saying so; SWE-gen had
already shown that test.sh passes offline. On public network an agent could look up
the upstream PR, the same as on the Monthly-SWEBench tasks.

Each task folder holds files from its upstream repo (tests, patches) under that repo's
license. `fresh-2026-10`: StuffAnThings/qbit_manage (MIT), agentconnect-md/agentconnect
(Apache-2.0), Ikalus1988/MisakaNet (Apache-2.0), chrisbenincasa/tunarr (Zlib),
basicmachines-co/basic-memory (AGPL-3.0), Gentleman-Programming/gentle-shell (MIT).

Before a set joins the exam, check every task independently of SWE-gen's own check,
locally and free (oracle must score 1, nop 0):

```
harness-loop/.venv/bin/python harness-loop/exam/run_exam.py --harness HEAD \
  --tasks harness-loop/tasks/fresh-<YYYY-MM>.json --agent controls --budget-usd 1
python3 harness-loop/tasks/manifest.py --require-cache harness-loop/tasks/fresh-<YYYY-MM>.json
```

SWE-gen never runs on this machine. Its Claude Code session runs with permission checks
off and reads untrusted repo and issue text, so `fresh/daytona_build.py` runs each
`swegen create` in its own throwaway Daytona sandbox (Debian + docker + the pinned
SWE-gen; SWE-gen's nop/oracle validation uses that docker), with a wall-clock TTL as a
backstop, and deletes it afterwards, also on failure. Only `swegen_shim.py` is uploaded,
and the SWE-gen process gets only the model ids, the Bedrock AWS key + region and the
Gemini key (`daytona_build.SANDBOX_ENV`), plus `IS_SANDBOX=1`: the sandbox runs as root,
and Claude Code refuses to skip permission checks as root without it. Only the log and, for a passing task, the task
folder come back, extracted as untrusted data.

Before any paid work `build.sh` requires `DAYTONA_API_KEY` (stays on this machine),
`GEMINI_API_KEY_DEV`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_REGION`, and
the daytona SDK from `fresh/requirements.txt`. `GITHUB_TOKEN` is optional: SWE-gen's PR
fetch works unauthenticated (GitHub allows 60 calls an hour; a task makes about 11). If
you set it, use a fine-grained token with read-only access to public repositories only;
`build.sh` never falls back to your `gh` login. The AWS key goes into the sandbox, where
the build session can read it, so use an IAM user allowed only `bedrock:InvokeModel*` on
the model ids above; no key with wider rights. The driver itself starts under `env -i`
with only those variables (plus `PATH`, `HOME`, `TMPDIR`, `AWS_SESSION_TOKEN` if set,
`SWEGEN_*` and the `ANTHROPIC_DEFAULT_*_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` ids).

SWE-gen is pinned in `fresh/requirements.txt` (0.1.1, commit 14e185f).
Differences from upstream SWE-gen: `build.sh` runs it through `fresh/swegen_shim.py`.
The task instructions are written by Gemini (`gemini-pro-latest` through Gemini's
OpenAI-compatible endpoint), not gpt-5.5, so instruction text differs from upstream
SWE-gen tasks. The Claude Code session that builds each Dockerfile runs on Bedrock
(`global.anthropic.claude-opus-5-5`), not claude-opus-4-8. The shim refuses to run
if `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` or any
`AWS_ADMIN_*` variable is set.
The places the shim patches are listed in `PATCH_TARGETS`; it stops with "update
PATCH_TARGETS" if a SWE-gen release moves one.

## Build a task from a merged PR

`build_from_pr.py` turns one merged (squash) PR of a local git repo into a Harbor task whose hidden
tests are that PR's own test files: the environment is a tarball of the merge commit's parent minus
those tests (and any `--exclude` paths; a remaining `.env`/`.env.*` is refused), `solution/fix.patch`
is the PR's non-test diff, and `tests/test.sh` copies the merged tests over the workdir and runs
`--test-command` (reward 1 iff it exits 0). `--manifest <file>` adds or replaces the task's row in that
manifest file, with the visible command only. Network keys are opt-in (`--allow-host` for an agent allowlist,
`--verifier-no-network`), because some sandbox providers/tiers reject any per-sandbox network policy.
Run `build_from_pr.py --help` for every flag.
