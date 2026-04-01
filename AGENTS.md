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

`/plan` explores the codebase, discusses tradeoffs with you, files beads issues, and runs an architectural plan review. Use it before `/work` for new epics.

`/work` triages the work, creates per-bead worktrees and branches, runs automated reviews with injected standards checklists, pushes branches, and auto-creates a PR for each bead. Dependent beads are blocked until their blockers are merged and closed. A PreToolUse hook enforces that reviewers receive their standards content — the coordinator cannot skip this step.

`/pr` regenerates or updates the PR summary for a branch. Since the coordinator auto-creates PRs, use this when you want to refresh the summary after additional commits. It is idempotent — safe to run multiple times.

`/merged` runs after you merge a PR on GitHub. It verifies the merge, closes the associated bead(s), removes the worktree, and deletes the branch. This is the gate that unblocks dependent beads.

## Issue Tracking with bd (beads)

**IMPORTANT**: This project uses **bd (beads)** for ALL issue tracking. Do NOT use markdown TODOs, task lists, or other tracking methods.

### Why bd?

- Dependency-aware: Track blockers and relationships between issues
- Git-friendly: Auto-syncs to JSONL for version control
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
bd update bd-42 --status in_progress --json
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
2. **Claim your task**: `bd update <id> --status in_progress`
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

bd automatically syncs with git:
- Exports to `.beads/issues.jsonl` after changes (5s debounce)
- Imports from JSONL when newer (e.g., after `git pull`)
- No manual export/import needed!

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

## Codebase Graph (Auto-Installed)

This project integrates **code-review-graph**, a tree-sitter-based structural codebase analyzer that provides 23 MCP tools for pre-computed graph queries. It delivers an average **8.2x token reduction** for codebase exploration compared to reading files directly.

### Auto-Install Behavior

On your **first Claude Code session** in this project, a SessionStart hook automatically:
1. Checks for Python 3 (exits gracefully if not found)
2. Installs `code-review-graph` via `pip install --user` (~10s)
3. Runs `code-review-graph install` to write `.mcp.json` (~1s)
4. Copies `.code-review-graphignore` from the template if it doesn't exist
5. Backgrounds the initial graph build, then starts the `watch` daemon for auto-rebuild on file changes

On your **second session** and all subsequent sessions, the hook ensures the `watch` daemon is running (restarts it if the machine rebooted), then exits in under 100ms. The graph tools appear in Claude's toolbox immediately.

### Key Tools

| Tool | Purpose |
|------|---------|
| `get_impact_radius_tool` | Given a file or function, return all callers, transitive dependencies, and affected tests |
| `get_review_context_tool` | Get focused context for reviewing a specific file or set of changes |
| `semantic_search_nodes_tool` | Find structurally similar types, functions, or patterns across the codebase |
| `get_architecture_overview_tool` | Get high-level architecture summary: packages, layers, and key entry points |
| `detect_changes_tool` | Detect what changed between two commits and return affected nodes |

See the full tool list at [code-review-graph documentation](https://github.com/kodu-ai/code-review-graph).

### Graceful Degradation

If Python is not installed on your machine, the hook exits instantly without errors. Claude Code continues to work normally, and all skills fall back to their existing Grep/Glob/Read workflows. The graph tools are an optimization, not a requirement.

### Manual Setup Fallback

If the auto-install hook fails for any reason, you can manually set up the graph:

```bash
pip install --user code-review-graph
code-review-graph install
code-review-graph build
code-review-graph watch &   # starts the auto-rebuild daemon
```

Then restart Claude Code. The graph tools will appear in the next session.

### Customizing Exclusions

The hook copies `.code-review-graphignore.template` to `.code-review-graphignore` on first setup. Edit `.code-review-graphignore` to exclude additional files or directories from the graph (e.g., large generated files, third-party code, test fixtures).

Common exclusions are already included: `node_modules/`, `vendor/`, `.venv/`, `dist/`, `build/`, `*.generated.*`, `*.min.js`, and binary/media files.

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
