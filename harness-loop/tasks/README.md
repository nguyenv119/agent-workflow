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
`--override-cpus 4 --override-memory-mb 8192` (oracle passes that way).
