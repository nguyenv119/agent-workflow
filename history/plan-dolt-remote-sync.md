# Plan: Dolt Remote Sync for Multi-Machine Collaboration

## Problem

Today, beads (`bd`) stores issues in a local Dolt database (`.beads/dolt/`). This works perfectly for a single machine — the coordinator claims beads, spawns subagents, and SQLite-level locking prevents conflicts. But when multiple developers (or multiple machines) work on the same project, there's no shared source of truth. Each machine has its own `.db` file, and pushing the JSONL export via git is prone to merge conflicts and race conditions.

## Solution

Dolt natively supports remotes (DoltHub, DoltLab, S3, etc.) with `dolt push`/`dolt pull` semantics identical to git. By configuring a Dolt remote and adding transparent sync hooks, multiple machines can share a single beads database without changing any skill files.

## Architecture

```
Machine A                          DoltHub                        Machine B
bd create → local Dolt             (shared remote)                bd ready → local Dolt
  ↓ (PostToolUse hook)               ↑↓                            ↑ (PreToolUse hook)
  dolt push origin main ──────→  nguyenv119/project  ──────→  dolt pull origin main
```

Skills (`coordinator`, `planner`, `implementer`, etc.) continue calling `bd` exactly as before. Claude Code hooks intercept `bd` commands transparently:
- **PreToolUse**: `dolt pull` before any `bd` command (fresh reads)
- **PostToolUse**: `dolt push` after write `bd` commands (propagate changes)

The hooks no-op if no Dolt remote is configured, so local-only setups are unaffected.

## Beads

### Bead 1: `/setup` slash command (one-time per project)

**What:** A new `.claude/commands/setup.md` that guides the user through configuring a Dolt remote.

**Flow:**
1. Ask: "Do you want to configure a Dolt remote for multi-machine collaboration?"
2. If yes: ask for the DoltHub remote URL (e.g., `nguyenv119/my-project`)
3. Check `dolt login` status — prompt to authenticate if needed
4. Run `dolt remote add origin <url>` inside `.beads/dolt/`
5. Run `dolt push -u origin main` to seed the remote
6. Install the sync hooks (Bead 2) into `.claude/settings.json`

**Files:**
- `NEW: .claude/commands/setup.md`

### Bead 2: Sync hooks (transparent pull/push around `bd` calls)

**What:** Two shell scripts + Claude Code hook configuration that transparently sync the local Dolt database with the remote before reads and after writes.

**Files:**
- `NEW: .claude/hooks/bd-dolt-pull.sh` — PreToolUse hook: reads stdin JSON, checks if command starts with `bd`, runs `dolt pull` if a remote is configured
- `NEW: .claude/hooks/bd-dolt-push.sh` — PostToolUse hook: reads stdin JSON, checks if command is a `bd` write operation (`create`, `update`, `close`, `delete`, `dep`, `reopen`), runs `dolt push` if remote is configured
- `MODIFY: .claude/settings.json` — add PreToolUse and PostToolUse hook entries (matcher: `Bash`)

**Hook logic:**
```bash
# bd-dolt-pull.sh (PreToolUse)
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[[ $COMMAND != bd* ]] && exit 0                    # not a bd command, skip
DOLT_DIR=$(bd where 2>/dev/null)/../dolt || exit 0  # find dolt dir
dolt -C "$DOLT_DIR" remote -v 2>/dev/null | grep -q origin || exit 0  # no remote, skip
dolt -C "$DOLT_DIR" pull origin main 2>/dev/null
exit 0
```

```bash
# bd-dolt-push.sh (PostToolUse)
INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
# Only push after write operations
[[ $COMMAND =~ ^bd\ (create|update|close|delete|dep|reopen|set-state|label) ]] || exit 0
DOLT_DIR=$(bd where 2>/dev/null)/../dolt || exit 0
dolt -C "$DOLT_DIR" remote -v 2>/dev/null | grep -q origin || exit 0
dolt -C "$DOLT_DIR" add .
dolt -C "$DOLT_DIR" commit -m "bd sync: $COMMAND"
dolt -C "$DOLT_DIR" push origin main 2>/dev/null
exit 0
```

**Key design decisions:**
- Hooks match ALL Bash commands (matcher: `Bash`) but exit immediately if not a `bd` command — Claude Code matchers can only filter on tool name, not command string
- No-op when no remote is configured (check `dolt remote -v`)
- Push only after write operations to minimize latency on reads
- 30-second timeout to avoid blocking on network issues

### Bead 3: Documentation

**What:** Update AGENTS.md with remote setup instructions and onboarding guide.

**Files:**
- `MODIFY: AGENTS.md` — add a "Multi-Machine Collaboration" section covering:
  - What Dolt remote sync does
  - How to run `/setup` to configure it
  - Onboarding for new team members: `dolt login` + clone project + hooks auto-sync
  - Limitations: git-level merge conflicts on JSONL are separate from Dolt sync

## Dependencies

```
Bead 1 (/setup command) ─── no dependencies
Bead 2 (sync hooks)     ─── no dependencies (can parallel with 1)
Bead 3 (documentation)  ─── depends on Bead 1 and 2 (needs to document what was built)
```

Beads 1 and 2 are independent and can be parallelized. Bead 3 is a fast follow-up.

## Out of Scope

- Conflict resolution when two machines push competing Dolt commits (Dolt handles this like git — last pusher must pull and merge first)
- Supporting remotes other than DoltHub (S3, GCS, DoltLab) — the hooks are URL-agnostic, but `/setup` only guides through DoltHub for now
- Replacing the JSONL backup mechanism — that continues to work independently as a git-level backup
