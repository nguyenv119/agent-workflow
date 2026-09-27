---
name: copy-harness
description: Copy the up-to-date agent harness into another repo that is already cloned, so /plan, /work, /merged, /handoff and the other skills and commands work there. Copies every file in .claude/skills, commands, hooks and agents, plus settings.json and AGENTS.md when the target has none. Merges Long's three live harness copies (agent-workflow, conspectus outer, conspectus inner), keeps the newest version of each file, and lists every file where the copies disagree. Use it whenever someone wants to move, copy, port, install, set up or refresh the harness, "our skills", "the workflow" or "the slash commands" in another repo or folder, even if they never say "harness". Invoked via /copy-harness <path-to-repo>.
---

# Copy the harness into another repo

The harness has three live copies on this machine, and they have drifted apart:

- `~/long-harness/agent-workflow/.claude` is the generic harness, with conspectus-specific lines stripped out.
- `~/conspectus/.claude` is the copy used every day. It carries project-specific lines, plus conspectus-only skills such as race-runner and services.
- `~/conspectus/conspectus/.claude` is the inner repo. Some of its skills are symlinks to the outer copy, and a few (build-profile, room-json-drift) live only here.

Changes have landed on both sides without being ported back. As of 2026-09-25, the planner's plain-language overview exists only in conspectus, while the file-based handoff and teach's takeaway-capture step exist only in agent-workflow. Because no single copy is complete, `copy_harness.py` merges all three, keeps the newest version of each file, and prints every file where the copies disagree, so a wrong pick is visible and can be swapped by hand.

## Run it

```bash
python3 <this skill's base directory>/copy_harness.py <target-repo>
```

- Just run it. The script never deletes anything and backs up whatever it replaces, so there is no need to preview first.
- `--dry-run` prints the same report and writes nothing, for when the user asks what a copy would change.
- `--self-test` runs the script's own checks in a temp folder.
- Pass the target's repo root. If the user didn't name one, ask for the path.
- Running it again refreshes a target. From a session inside a target that already has the harness, `/copy-harness .` works too.

## What it copies

- Every file under `.claude/skills`, `commands`, `hooks` and `agents`, conspectus-only skills included, because the ask was all of them. Symlinks are followed, so the target gets real files instead of links back into conspectus.
- `.claude/smoke-test.sh`, which `/smoke-test` runs.
- `.claude/settings.json` (wires the hooks) and `AGENTS.md` (the workflow rules printed at session start). Each one is copied only if the target lacks that file and is never replaced afterwards, because the target's version may be the project's own or carry local edits.

It never copies `.env` files, `CLAUDE.md`, `settings.local.json`, or the logs and data folders (`discover-kb`, `loop-evals`, runbooks and the rest) that sit beside the harness in `~/conspectus/.claude`. Inside skill folders it skips `.DS_Store`, `*.bak*`, `__pycache__`, `*.pyc` and `*.log`. Skills in `~/.claude/skills` already load in every repo, so they are not copied.

## What happens to files already in the target

- Same content: left alone.
- Different and older: replaced. The old version is saved first under `~/.claude/harness-backups/<target>-<timestamp>/`.
- Different and newer than every source: kept, because it is probably a local edit. Git decides for the files it tracks, since a fresh clone stamps every file with the clone time: a tracked file with uncommitted changes is kept, and one that matches its last commit is replaced like any older copy.
- Nothing in the target is deleted.
- It never writes through a symlink that leads outside the target, and it refuses a target that is one of the three sources. The conspectus copies symlink into each other, so writing through a link would quietly edit a source.
- It adds `.claude/` (and `AGENTS.md`, when it copied one) to the target's `.git/info/exclude`, which keeps the harness out of the target's commits the same way `~/conspectus/conspectus` does. Files git already tracks stay tracked.

## After it runs

1. Relay the report: counts, the backup folder if anything was replaced, and the disagreement list as one short line per file naming the copy that won.
2. A `kept` line for `settings.json` or `AGENTS.md` means the target already has its own version: the project's own file, or an earlier harness copy that has since fallen behind. Show the user the diff against the source the line names, and change the file only with their OK, since the target's team may track it. A settings.json missing the harness `hooks` block leaves the bd sync hooks, the reviewer-standards guard that `/work` depends on, and the session-start print of AGENTS.md unwired. An AGENTS.md without the harness text means the workflow rules aren't loaded at session start.
3. The implementer and reviewers read test, lint and typecheck commands from a Quality Gates table in the target's `CLAUDE.md`. If it has none, offer to draft one from the target's package.json, Makefile or CI config.
4. If the target has no `.beads/`, `/plan` and `/work` need `bd init`. Offer it rather than running it: it creates a database and may install git hooks.
5. If the target isn't a conspectus repo, the conspectus-only skills came along too. They are the skill folders missing from `~/long-harness/agent-workflow/.claude/skills`; name them and offer to delete them from the target.
6. Some repos commit their own copies of harness files (claudespectus commits coordinator, planner and the reviewers). The harness versions replace them, and the report lists them as tracked files that changed. Name them and warn that a later `git add -A` would commit them; `git checkout -- <file>` restores the repo's version of any the user wants to keep.
7. Tell the user to open a new Claude Code session in the target and accept the trust prompt. Until the folder is trusted, Claude Code ignores the harness's permission rules; skills, commands and hooks load either way.

Commit nothing, in the target or in any source.

## Taking the other copy of a file

Each disagreement line names the winning copy and the older ones. To use an older copy instead, copy it over by hand: `cp <source root>/<path> <target>/<path>`. The source roots live in `SOURCES` at the top of `copy_harness.py`; edit that list when a harness copy moves or a new one appears.
