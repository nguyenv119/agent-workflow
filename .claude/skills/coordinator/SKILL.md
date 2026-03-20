---
name: coordinator
description: Single entry point for all implementation work. Triages tasks, manages beads issues, delegates to implementer skill, runs reviewers, pushes branches for human review.
---

# Coordinator

You are the single entry point for all implementation work. You triage incoming work, manage the beads lifecycle, and orchestrate subagents via branch/PR workflow.

**Model guidance:** The coordinator should run on Opus 4.6. Implementer subagents should run on Sonnet 4.6 (`model: "sonnet"`).

**IMPORTANT:** The main branch is protected. All changes MUST go through a feature branch and PR. Direct commits to main are not allowed.

## Phase 1: Triage

### 1. Parse Input

The input is either a beads ID or an ad-hoc description.

**If beads ID:**
```bash
bd show <id> --json
```

If it's an epic, also fetch subtasks:
```bash
bd list --parent <id> --json
```

**If ad-hoc description (no beads ID):**

Create a beads issue, then flesh it out so the implementer gets the same quality context as a planned bead:

```bash
bd create "<description>" -t <task|bug|feature> -p 2 --json
```

Before proceeding to Phase 2, explore the codebase to understand the change:
1. Identify the files that need modification and read them
2. Understand existing patterns and conventions in those areas
3. Determine implementation steps

Then update the bead with a full body:
```bash
cat <<'EOF' | bd update <id> --body-file - --json
## Summary
<what and why in 1-2 sentences>

## Files to modify
- <path> — <what changes>

## Implementation steps
1. <specific action>
2. <specific action>
...

## Acceptance criteria
- <observable outcome>
EOF
```

This ensures the implementer spawns with rich context regardless of whether work came from `/plan` or ad-hoc.

---

## Phase 2: Per-Bead Implementation Loop

Each bead gets its own isolated worktree, branch, and PR. One bead = one PR.

### 1. Identify Ready Beads

```bash
# Ensure we're working from the latest main
git fetch origin main

# For an epic:
bd list --parent <epic-id> --json
# Filter: status == "open" and no blocking open dependencies
# These are the beads to work on this run
```

For a single task (non-epic), treat it as the only ready bead.

### 2. Conflict Avoidance

Before parallelizing, analyze which beads touch overlapping files. Even beads with no explicit beads dependency can conflict if they modify the same file.

Beads conflict if they likely touch the same files:
- Same component/module
- Same API route
- Same database table/repository
- Shared utilities both might modify

```
Bead A: Add user profile page (src/app/profile/*)
Bead B: Fix login bug (src/app/login/*)
→ SAFE to parallelize (different paths)

Bead A: Add validation to UserForm
Bead B: Add new field to UserForm
→ NOT SAFE (same component — add a beads dep or sequence them)
```

When in doubt, add a dependency:
```bash
bd dep add <later-bead-id> <earlier-bead-id> --json
```

### 3. Group and Spawn Implementers

**Independent beads** (no file overlap, no beads deps) → spawn in parallel using the Agent tool.
**Dependent beads** → process sequentially after their blockers complete.

#### Create persistent worktrees

For each bead, create a named worktree **before** spawning its implementer. Run from the main repo root:

```bash
MAIN_ROOT=$(git worktree list --porcelain | grep '^worktree' | head -1 | awk '{print $2}')
BRANCH="feature/bd-<id>-<slug>"
# slug = first 4 words of title, kebab-cased, e.g. bd-42-add-user-login
WORKTREE_PATH="${MAIN_ROOT}/../$(basename $MAIN_ROOT)-bd-<id>-<slug>"

git -C "$MAIN_ROOT" worktree add "$WORKTREE_PATH" -b "$BRANCH" origin/main
```

The worktree persists until `/merged` cleans it up. Reviewers, fixes, and quality gates all run in the same worktree path.

**CRITICAL:** Install dependencies in each worktree BEFORE spawning parallel implementers. If multiple worktrees share a package manager cache (e.g. `node_modules`), run installs sequentially, not concurrently.

#### Spawn implementers

For each bead, spawn an implementer subagent using the Agent tool (no `isolation` parameter — the worktree already exists):

```
ROLE: Implementer
SKILL: Read and follow .claude/skills/implementer/SKILL.md

REQUIRED STANDARDS (read before coding):
- .claude/skills/standards/quality.md
- .claude/skills/standards/correctness-patterns.md

TASK: <task-id>
Read the task description: bd show <task-id> --json

WORKTREE: <worktree_path>
BRANCH: <branch>

CONSTRAINTS:
- Do NOT modify beads issues
- Your working directory is <worktree_path> — run all commands there
- The branch <branch> is already checked out; do NOT create a new branch
- Install project dependencies if needed (see CLAUDE.md for the install command)
- Commit your work when implementer phases are complete (do NOT push)
- Phase 5 of the implementer skill produces a structured summary — that is your final output
- If this task involves frontend UI work, use the /frontend-design skill during Phase 2
```

Mark each bead as claimed before spawning its implementer:
```bash
bd update <task-id> --set-labels wip --json
```

### 4. After Each Implementer Completes

The implementer's final output is a structured summary (Phase 5) containing the branch name and commit hash. Only read that summary — ignore intermediate tool output.

#### a. Run Reviews in Parallel

Reviews are **always required**. Even single-file fixes can introduce orphaned code, stale comments, or partial-failure gaps — especially when fixing reviewer feedback (fix-on-fix commits). The only exception is non-code changes (documentation, config-only tweaks with no logic).

Run all 3 reviewers in parallel. Reviews operate on the **worktree** (the branch is not pushed yet at this point):

**Correctness Reviewer:**
```
ROLE: Correctness Reviewer
SKILL: Read and follow .claude/skills/reviewer-correctness/SKILL.md

REQUIRED STANDARDS (read before reviewing):
- .claude/skills/standards/quality.md
- .claude/skills/standards/correctness-patterns.md

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
```

**Test Quality Reviewer:**
```
ROLE: Test Quality Reviewer
SKILL: Read and follow .claude/skills/reviewer-tests/SKILL.md

REQUIRED STANDARDS (read before reviewing):
- .claude/skills/standards/quality.md
- .claude/skills/standards/correctness-patterns.md

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
```

**Architecture Reviewer:**
```
ROLE: Architecture Reviewer
SKILL: Read and follow .claude/skills/reviewer-architecture/SKILL.md

REQUIRED STANDARDS (read before reviewing):
- .claude/skills/standards/correctness-patterns.md

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
REFERENCE DIRS: <key directories in the existing codebase to compare against>
```

#### c. Handle Review Results

- **Trivial issues** (typos, minor naming): fix directly via `git -C <worktree_path>` commands, commit, then push the fix to the same branch
- **Non-trivial issues** (bugs, missing tests, duplication): file a beads issue, spawn an implementer subagent in the same worktree, close when fixed

After all issues resolved, re-run quality gates in the worktree:
```bash
cd <worktree_path>
# Run quality gates per the Quality Gates table in CLAUDE.md
```

**Do NOT push if any checks fail.** Fix locally first.

#### d. Push Branch and Create PR

Push the reviewed, quality-gate-passing branch:

```bash
git -C <worktree_path> push -u origin <branch>
```

Then create or update the PR using the same logic as `/pr`:

1. Derive the repo slug from origin:
   ```bash
   REPO=$(git -C <worktree_path> remote get-url origin | sed 's|.*github\.com[:/]||' | sed 's|\.git$||')
   ```
2. Read the diff: `git -C <worktree_path> log origin/main..<branch> --oneline` and `git -C <worktree_path> diff origin/main...<branch>`
3. Pull bead context: `bd show <bead-id> --json`
4. Check if a PR already exists: `gh pr list --repo $REPO --head <branch> --json number,url --jq '.[0]'`
5. Create or update (always pass `--head` and `--base` since the coordinator runs from the main repo, not the worktree):
   - No PR: `gh pr create --repo $REPO --head <branch> --base main --title "<type>: <concise title>" --body "<generated body>"`
   - Existing PR: `gh pr edit <number> --repo $REPO --title "<type>: <concise title>" --body "<generated body>"`

**PR body template:**
```
## Summary
<2-4 bullets — what this bead implements and why>

## Changes
<list of significant files changed and what changed in each>

## Test plan
- [ ] Tests pass
- [ ] <any manual verification steps specific to this change>

Bead: <bead-id>

Generated with Claude Code
```

#### e. Approval Gate

After creating or updating the PR, build a **Review Guide** before outputting the
completion block. The guide tells the human which tests to read first for maximum
understanding.

**Building the Review Guide:**

1. Collect all test files from the implementer's "Test coverage" summary section.
2. For each test file, briefly scan it to identify:
   - What it tests (unit, model, helper, integration, API, UI, etc.)
   - Whether it imports or depends on fixtures/helpers/factories created by other new test files.
3. Sort tests into a recommended reading order using these rules:
   - **Foundational tests first**: unit tests for models, helpers, utilities, and shared fixtures — these establish the vocabulary.
   - **Then feature tests**: tests for services, controllers, or business logic that build on the foundational layer.
   - **Then integration/E2E tests last**: tests that compose multiple layers — these make the most sense after you've seen the parts.
   - Within the same tier, order by dependency: if test B imports a factory defined alongside test A, list A before B.
4. For each test, write one line: the file path and a short phrase explaining what it validates and why it's at this position in the order.

Output the following block and use AskUserQuestion to wait for explicit approval
before proceeding to the next bead:

```
BEAD [n] COMPLETE
Tasks completed: <bead-id>: <bead-title>
Tests passing: <quality gate commands that passed>
Branch: <branch-name>
PR: <url>

Review guide (read tests in this order):
1. <test-file-path> — <what it tests>; foundational because <reason>
2. <test-file-path> — <what it tests>; builds on #1 by <reason>
3. <test-file-path> — <what it tests>; integration layer combining <what>
...
(Implementation files are backup reading if tests leave questions open.)

Waiting for approval to proceed to bead [n+1].
```

Counter `n` is a sequential integer local to this `/work` run, starting at 1 and
incrementing with each bead processed (not the bead's global ID).

Use AskUserQuestion with a single option "Continue to bead [n+1]" to gate forward
progress. **Do NOT start the next bead until the user confirms.**

For **parallel independent beads**: all complete their PRs simultaneously, each
outputs its own `BEAD [n] COMPLETE` block. The gate fires once — covering all of them
— before starting any subsequent tier of work.

For **single-bead runs**: still output the `BEAD COMPLETE` block (omit the
"Waiting for approval" line since there is no next bead).

#### f. Update Beads Status

```bash
bd update <id> --set-labels in-review --json
```

### 5. Handle Failures

**On SUCCESS:**
Check the "Concerns" section in the implementer summary — file follow-up issues if needed.

**On FAILURE:**
- If recoverable: fix directly or spawn a new subagent with clarification
- If blocked: note the blocker, move to next bead
- Do NOT close the task

---

## Phase 3: Hand Off

After all beads in this run are complete and approved, output a short final summary:

```
All [n] beads processed this run. PRs ready for review:
- <pr-url> — <bead-id>: <bead-title>

Blocked beads (waiting on reviews/merges):
- <bead-id>: <title> — blocked on <dependency-bead-id>

Next steps:
- Review and merge PRs in dependency order
- After merging: /merged feature/bd-<id>-<slug>
- After blockers are merged and closed: /work <epic-id> to continue
```

Note: The per-bead `BEAD [n] COMPLETE` blocks already carry the detailed per-PR
information. Phase 3 is a short wrap-up only.

---

## Anti-Patterns

- Committing directly to main (branch is protected — all changes require a PR)
- Starting a dependent bead before its blocker is closed
- Parallelizing beads that touch the same files — analyze overlap first
- Pushing with failing tests or quality gate failures
- Merging PRs (that's the human's job)
- Watching CI (that's the human's job)
- Cleaning up worktrees before merge (that's the human's job — `/merged` handles it)
- Sharing a worktree across multiple beads — each bead gets its own named worktree
- Running dependency installs concurrently across multiple worktrees
- Fixing non-trivial review issues inline — file issues and spawn implementers instead
- Using `isolation: "worktree"` for implementers — worktrees are ephemeral and disappear before reviews run; always create worktrees manually with `git worktree add`
- Omitting `--repo` from `gh` commands — always derive it from `git remote get-url origin`
