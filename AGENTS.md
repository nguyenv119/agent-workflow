# Agent Instructions

You are an experienced software engineer, building well-structured, well-maintained
software. You should not create or tolerate significant duplication, architectural
mess, or poor code organization. Clean small messes up immediately, and file tickets
for resolving larger issues in follow-on work.

## Workflows

| Scenario | Command |
|----------|---------|
| New epic or feature design | `/plan <description-or-epic-id>` |
| All implementation work | `/work <id-or-description>` |
| Regenerate/update a PR summary | `/pr [branch-name]` |
| Close bead and clean up after merge | `/merged [branch-name]` |
| End a session before `/clear` | `/handoff` |

Before writing a script for a chore (worktree, PR checks, waiting, locks, eval verdicts), run `agent-cli --help`; project CLIs are listed at its end.

`/plan` explores the codebase, discusses tradeoffs with you, files beads issues, and runs an architectural plan review. Use it before `/work` for new epics.

`/work` triages the work, creates per-bead worktrees and branches, runs automated reviews with injected standards checklists, pushes branches, and auto-creates a PR for each bead. Dependent beads are blocked until their blockers are merged and closed. A PreToolUse hook enforces that reviewers receive their standards content — the coordinator cannot skip this step.

`/pr` regenerates or updates the PR summary for a branch. Since the coordinator auto-creates PRs, use this when you want to refresh the summary after additional commits. It is idempotent — safe to run multiple times.

`/merged` runs after you merge a PR on GitHub. It verifies the merge, closes the associated bead(s), removes the worktree, and deletes the branch. This is the gate that unblocks dependent beads.

`/handoff` writes an end-of-session briefing to `~/.claude/handoffs/YYYY-MM-DD-<slug>.md` and posts only the path plus a two-line gist in chat. It is written so the next agent starts where this session ended: facts verified and how, approaches that failed and why, exact paths/IDs/commands, and every open item with its single next action. Run it before `/clear` — the file survives compaction, a chat summary does not.

## Rule -> enforcer

A PreToolUse hook (`agent-cli hook pretooluse`) checks each Bash command against these rules. Deny blocks the command and names the replacement; warn adds context and lets it run. `# agent-cli: allow <rule id>` in the command skips one rule; `AGENT_CLI_HOOKS=off` skips all. `agent-cli hook rules` lists them, project rules included. `agent-cli hook scan <dir>` runs the same rules over script files (or `--stdin` over a command list) and reports a hit even when it carries the allow comment. Rows marked `skill text only` have no hook yet: only the named skill asks for them, so nothing blocks a skip.

| Rule id | What it stops | Replacement command | Mode |
|---------|---------------|---------------------|------|
| `agent-cli-hand-verdict` | A worker printing the eval's WIN sentinel by hand | `agent-cli eval run <dir>` (exit 0 = WIN); inside the eval, `source "$(agent-cli eval lib --sh)"`, then `win` | deny |
| `hand-worktree-add` | A hand-typed `git worktree add ... -b` (skips the project's setup hook) | `agent-cli wt new <bead-id> <slug> --base origin/main` | deny |
| `hand-worktree-add-other` | A hand-typed `git worktree add` on an existing branch (`--detach` is exempt) | `agent-cli wt new <id> <slug> --existing <branch>` | warn |
| `hand-sleep-poll` | A foreground `while`/`until` loop that sleeps 3s or more | `agent-cli wait --until '<status cmd>' --every 60 --cap 30m` in the background | warn |
| `hand-pr-poll` | A hand-written `while`/`until` loop that sleeps and polls `gh pr checks`/`gh pr view`/`gh api .../pulls`, or `gh pr checks --watch` | `agent-cli pr wait <n> --cap 40m` (run_in_background); one-shot `agent-cli pr status <n>` | deny |
| `hand-loop-log` | `echo`/`printf >>` into a hand-rolled `loop-*.log` or `.txt` | `agent-cli log append .claude/loop-<epic-id>.tsv --phase ... --action ... --result ...` | warn |
| `blast-radius-proof` | A risky bead (schema, shared infra, cross-service contract, prod-affecting) shipped on an asserted safety claim | `.claude/skills/blast-radius/SKILL.md`: one "Safe because / Proved by" paragraph with a command that ran, in the BEAD COMPLETE block | skill text only |
| `measured-number-checklist` | A cost ratio, speedup or pass rate reported without vetting | `.claude/skills/win-condition/SKILL.md` § Before reporting a measured number: answer all 7, `not checked` where skipped | skill text only |
| `page-card-format` | A document page or to-do card that breaks the format rules (layer parts, one legend line, closed For agents, needs-you fields, no ids or tokens in prose) | `agent-cli page check <page>`, `agent-cli card check <dir>`; edits by `agent-cli page mark-update\|new-layer\|new` and `agent-cli card write`; per-project rules in `page-rules.json` / `card-rules.json` seams | skill text only |

A background hook (`agent-cli hook posttooluse` and `posttoolusefailure`, async, never blocks) records each Bash command's shape: command and subcommand names and flag names, with every value masked (`<str>`, `<path>`, `<n>`, ...). It never stores the command or its output. A project lists its own multi-command tools (whose second word is a subcommand) in the `subcommand-tools` seam, and wrapper words in `command-prefixes`. Rows go to `~/.claude/agent-cli/shapes/<day>.jsonl` (`AGENT_CLI_SHAPES_DIR` moves it; files older than `AGENT_CLI_SHAPES_KEEP_DAYS`, default 45, are deleted). `agent-cli repeats` reports the shapes run in 3+ sessions that no hook rule and no command covers yet (`--json`, `--since 14d` or `--from/--until`; `--commands <file>` replays raw commands in memory); a project names its own CLIs on a `commands:` line under `## repeats` in `.claude/project-rules.md`.

Each session start rewrites the list of those shapes (`agent-cli repeats --write-cache`, into the store). When you type a command whose shape is on it, the PreToolUse hook adds a warning once per session (and per subagent): the shape was hand-written N times across M sessions, so propose a command for it (the `cli-for-agents` skill). It never blocks, it says nothing when a rule denies the command, and `# agent-cli: allow repeated-shape` silences it. Shapes made only of generic reads (`cat`, `sed -n`, `grep`, `head`, `ls`, `git status/diff/log/show`, inline `python3 - <<EOF` or `-c` code) never warn: they top the list because agents read files all day, and there is no chore in them to wrap. `agent-cli repeats` still lists them. When the user's prompt says something like "should be a CLI", "make this a command" or "we keep typing this", the UserPromptSubmit hook tells you to file a bead for the command whose acceptance cites the skill and its house rules; a project adds its own phrasing as one regex on a `cli-phrases:` line under `## repeats`.

## Issue Tracking with bd (beads)

**IMPORTANT**: This project uses **bd (beads)** for ALL issue tracking. Do NOT use markdown TODOs, task lists, or other tracking methods.

### Why bd?

- Dependency-aware: Track blockers and relationships between issues
- Git-friendly: Dolt-powered version control with native sync (see Multi-Machine Collaboration)
- Agent-optimized: JSON output, ready work detection, discovered-from links
- Prevents duplicate tracking systems and confusion

### Quick Start

**Check for ready work:**
```bash
bd ready --json
bd ready --json | jq '[.[] | select(.issue_type == "epic")]'
bd list --json | jq '[.[] | select(.status == "open" and .priority <= 1)]'
```

**Create new issues:**
```bash
bd create "Issue title" -t bug|feature|task -p 0-4 --json
bd create "Issue title" -d "Short description" -p 1 --json
bd create "Issue title" -p 1 --deps discovered-from:bd-123 --json
bd create "Subtask" --parent <epic-id> --json  # Hierarchical subtask (gets ID like epic-id.1)
# Multi-line descriptions: pipe heredoc into --body-file -
cat <<'EOF' | bd update bd-42 --body-file - --json
Multi-line description here.
EOF
```

**Claim and update:**
```bash
bd update <id> --claim --json      # atomic claim (sets in_progress)
bd update bd-42 --priority 1 --json
```

**Complete work:**
```bash
bd close bd-42 --reason "Completed" --json
```

### Issue Types

- `bug` - Something broken
- `feature` - New functionality
- `task` - Work item (tests, docs, refactoring)
- `epic` - Large feature with subtasks
- `chore` - Maintenance (dependencies, tooling)

### Priorities

- `0` - Critical (security, data loss, broken builds)
- `1` - High (major features, important bugs)
- `2` - Medium (default, nice-to-have)
- `3` - Low (polish, optimization)
- `4` - Backlog (future ideas)

### Workflow for AI Agents

1. **Check ready work**: `bd ready` shows unblocked issues (open + no blocking deps)
2. **Claim your task atomically**: `bd update <id> --claim`
3. **Work on it**: Implement, test, document
4. **Discover new work?** Create linked issue:
   - `bd create "Found bug" -p 1 --deps discovered-from:<parent-id>`
5. **Complete**: `bd close <id> --reason "Done"`

### Writing Self-Contained Issues

Issues must be fully self-contained - readable without any external context (plans, chat history, etc.). A future session should understand the issue completely from its description alone.

**Required elements:**
- **Summary**: What and why in 1-2 sentences
- **Files to modify**: Exact paths (with line numbers if relevant)
- **Implementation steps**: Numbered, specific actions
- **Example**: Show before -> after transformation when applicable

### Dependencies: Think "Needs", Not "Before"

`bd dep add X Y` = "X needs Y" = Y blocks X

**TRAP**: Temporal words ("Phase 1", "before", "first") invert your thinking!
```
WRONG: "Phase 1 before Phase 2" -> bd dep add phase1 phase2
RIGHT: "Phase 2 needs Phase 1" -> bd dep add phase2 phase1
```
**Verify**: `bd blocked` - tasks blocked by prerequisites, not dependents.

### Auto-Sync

bd writes auto-commit to Dolt history — there is no JSONL export step. Remote
sync is automatic via the `.claude/hooks/` pull/push hooks (see Multi-Machine
Collaboration). Manual sync when needed: `bd dolt push` / `bd dolt pull`.

### Managing AI-Generated Planning Documents

AI assistants often create planning and design documents during development:
- PLAN.md, IMPLEMENTATION.md, ARCHITECTURE.md
- DESIGN.md, CODEBASE_SUMMARY.md, INTEGRATION_PLAN.md
- TESTING_GUIDE.md, TECHNICAL_DESIGN.md, and similar files

**Best Practice: Use a dedicated directory for these ephemeral files**

**Recommended approach:**
- Create a `history/` directory in the project root
- Store ALL AI-generated planning/design docs in `history/`
- Keep the repository root clean and focused on permanent project files
- Only access `history/` when explicitly asked to review past planning

### CLI Help

Run `bd <command> --help` to see all available flags for any command.
For example: `bd create --help` shows `--parent`, `--deps`, `--assignee`, etc.

### Important Rules

- Use bd for ALL task tracking
- Always use `--json` flag for programmatic use; pipe through `jq` for filtering
- Link discovered work with `discovered-from` dependencies
- Check `bd ready` before asking "what should I work on?"
- Store AI planning docs in `history/` directory
- Run `bd <cmd> --help` to discover available flags
- Do NOT create markdown TODO lists
- Do NOT use external issue trackers
- Do NOT duplicate tracking systems
- Do NOT clutter repo root with planning documents

## Multi-Machine Collaboration

Beads issues can be shared across machines via DoltHub, the hosted Dolt database service. When configured, every `bd` command automatically syncs: hooks pull the latest data before each read and push changes after each write.

### One-Time Setup (per project)

Run `/setup-remote` once to connect the project's beads database to DoltHub:

```
/setup-remote
```

This command will ask for your DoltHub remote path (e.g., `owner/database-name`), configure `origin`, and push the current beads data. Prerequisites:

1. **Install Dolt**: https://docs.dolthub.com/introduction/installation
2. **Authenticate**: Run `dolt login` and follow the prompts

### How Sync Works

Two Claude Code hooks in `.claude/hooks/` handle sync automatically:

- **`bd-dolt-pull.sh`** (PreToolUse): Runs `dolt pull origin main` before every `bd` command, so the agent always reads the latest issues.
- **`bd-dolt-push.sh`** (PostToolUse): Runs `dolt push origin main` after any `bd` write command (`create`, `update`, `close`, etc.), so changes propagate immediately.

Both hooks **no-op when no remote is configured** — local-only projects are unaffected. Network errors are non-fatal; the hooks always exit 0 so a connectivity issue never blocks agent work.

### Adding a New Machine

On each additional machine:

1. `dolt login` — authenticate with DoltHub
2. Clone the project repo — the `.beads/` directory and hooks are already checked in
3. Run `/setup-remote` — configure the same DoltHub remote URL

After that, `bd` commands on the new machine auto-pull and auto-push just like the original machine.

### Merge Conflicts

If two machines push simultaneously, you may encounter a Dolt merge conflict. Resolve it the same way as a git conflict:

```bash
cd .beads/dolt
dolt pull origin main   # triggers the conflict
dolt conflicts resolve --ours .   # or --theirs, or edit manually
dolt commit -m "resolve merge conflict"
dolt push origin main
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
- In exam mode (`.claude/EXAM_MODE` exists), pushing to the local bare origin completes the work; there is no PR.
