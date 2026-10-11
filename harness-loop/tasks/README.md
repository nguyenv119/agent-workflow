# Exam task manifests

A manifest is one JSON file per task set: `{"set", "source", "tasks": [...]}`. Each task row has
`id, harbor_path, repo, pr, merged_at, language, kind (bugfix|feature), workdir, test_command, title, screen`.
`repo`/`pr`/`merged_at` are null for tasks not taken from a PR; `test_command` is the VISIBLE test command
the agent may run (null = "no visible tests"), never the hidden verifier tests.

Validate: `python3 harness-loop/tasks/manifest.py harness-loop/tasks/<set>.json` prints a one-line summary
or fails naming the task id. Sets are named `<source>-<slice>` (e.g. `tb4-software`, `shakedown-2026-05`).
`harbor_path` is relative to this folder, under `cache/` (downloaded task folders, git-ignored).

TODO: `shakedown-2026-05.json` (Monthly-SWEBench 2026-05) is pending approval to download the dataset.
