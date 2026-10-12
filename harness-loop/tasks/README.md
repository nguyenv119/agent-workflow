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
and starts no new task once the total reaches `--confirm-budget`. A task's cost is the
`total_cost_usd` the Claude session reports in its log; if the log has none, it is elapsed
time x `--usd-per-hour` (default $13.50/hour, i.e. $12 for SWE-gen's default 3200 s
session timeout). It needs `swegen` and docker installed, and resumes a stopped run (ids
in `passed.txt` / `failed.txt` there are skipped).

Before any paid work `build.sh` requires `GITHUB_TOKEN`, `GEMINI_API_KEY_DEV`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_REGION`. Set `GITHUB_TOKEN` to a
fine-grained token with read-only access to public repositories only; `build.sh` never
falls back to your `gh` login. SWE-gen's Claude session runs with permission checks off,
so it is started under `env -i` with only those variables (plus `PATH`, `HOME`, `TMPDIR`,
`AWS_SESSION_TOKEN` if set, and any `SWEGEN_*`), from a scratch folder under the cache.

Differences from upstream SWE-gen: `build.sh` runs it through `fresh/swegen_shim.py`.
The task instructions are written by Gemini (`gemini-pro-latest` through Gemini's
OpenAI-compatible endpoint), not gpt-5.5, so instruction text differs from upstream
SWE-gen tasks. The Claude Code session that builds each Dockerfile runs on Bedrock
(`global.anthropic.claude-opus-5-5`), not claude-opus-4-8. The shim refuses to run
if `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` or any
`AWS_ADMIN_*` variable is set.
The places the shim patches are listed in `PATCH_TARGETS`; it stops with "update
PATCH_TARGETS" if a SWE-gen release moves one.
