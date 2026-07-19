---
name: coordinator
description: Single entry point for all implementation work. Triages tasks, manages beads issues, delegates to implementer skill, runs reviewers, pushes branches for human review.
---

# Coordinator

You are the single entry point for all implementation work. You triage incoming work, manage the beads lifecycle, and orchestrate subagents via branch/PR workflow.

**Model guidance:** The coordinator should run on Opus 4.8. Implementer subagents should run on Sonnet 5 (`model: "sonnet"`).

**IMPORTANT:** The main branch is protected. All changes MUST go through a feature branch and PR. Direct commits to main are not allowed.

## Modes

- **Interactive (default)** — process ready beads and gate on human approval after each (Phase 2 §4e). Current behavior; nothing changes.
- **Autonomous Loop Mode** — when the epic carries a `## Win Condition`, offer to run the loop unattended: the §4e human gate is replaced by the win-condition eval, and you iterate until it passes (or a cap trips). See "Autonomous Loop Mode" below. The per-bead machinery (worktree, branch, implementer, reviewers, gates, PR) is identical in both modes.

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

**With graph tools (when available):** Use `get_impact_radius_tool` on the likely
affected files to get a complete list of files, callers, and tests that may need
changes. This replaces manual Grep exploration and catches indirect dependencies.

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

**With graph tools (when available):** Use `get_impact_radius_tool` on each bead's
target files. If two beads' impact radii overlap (shared files in both results),
they conflict. This replaces the heuristic analysis above with a deterministic check.

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
- Apply the `ponytail` skill during Phase 2: climb its ladder (YAGNI → stdlib → native platform → existing dep → one line → minimum code) and ship the shortest diff that fully does the task, without cutting validation, error handling, security, accessibility, or tests
```

Mark each bead as claimed before spawning its implementer:
```bash
bd update <task-id> --set-labels wip --json
```

### 4. After Each Implementer Completes

The implementer's final output is a structured summary (Phase 5) containing the branch name and commit hash. Only read that summary — ignore intermediate tool output.

#### a. Run Reviews in Parallel

Reviews are **always required**. Even single-file fixes can introduce orphaned code, stale comments, or partial-failure gaps — especially when fixing reviewer feedback (fix-on-fix commits). The only exception is non-code changes (documentation, config-only tweaks with no logic).

**Before spawning reviewers, read both standards files fresh from the worktree** (do NOT rely on memory — they may have been updated):

```bash
cat <worktree_path>/.claude/skills/standards/quality.md
cat <worktree_path>/.claude/skills/standards/correctness-patterns.md
```

Then construct a per-reviewer checklist by extracting the relevant sections per this mapping:

| Reviewer | Sections to extract |
|----------|-------------------|
| Correctness | `correctness-patterns.md` ALL sections (Race/Select, Unbounded Accumulation, Multi-Step Orchestration, Retry Scope, Type Narrowing, Derived Data, Dual Code Paths) + `quality.md` §F (Refactor Cleanup Audit) + `quality.md` §G (Review Discipline) |
| Test Quality | `quality.md` §A (Test Structure) + §B (Docstrings) + §C (Mock Discipline) + §D (Test Naming) + §E (Core Dependency Flagging) + §G (Review Discipline) |
| Architecture | `correctness-patterns.md` Retry Scope + Dual Code Paths + Derived Data sections + `quality.md` §F (Refactor Cleanup Audit) + §G (Review Discipline) |

Format each extracted section as a numbered checklist item, e.g.:

```
## Review Checklist (respond to each item)

1. **Race/Select Orphaned Failures**: When a race or select construct picks a winner,
   check whether the losing branch can fail after the winner settles. [correctness-patterns.md]
2. **Unbounded Input Accumulation**: Do loops collecting input have size caps or
   backpressure? [correctness-patterns.md]
3. **Refactor Cleanup Audit**: In every modified function, check for dead variables,
   stale comments, and unused imports. [quality.md §F]
...
```

Inject the constructed checklist as `<checklist>` in each reviewer prompt below.

Run all 3 reviewers in parallel. Reviews operate on the **worktree** (the branch is not pushed yet at this point):

**Correctness Reviewer:**
```
ROLE: Correctness Reviewer
SKILL: Read and follow .claude/skills/reviewer-correctness/SKILL.md

Standards files (for reference):
- .claude/skills/standards/quality.md
- .claude/skills/standards/correctness-patterns.md

<checklist> (correctness sections: all of correctness-patterns.md + quality.md §F + §G)

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
```

**Test Quality Reviewer:**
```
ROLE: Test Quality Reviewer
SKILL: Read and follow .claude/skills/reviewer-tests/SKILL.md

Standards files (for reference):
- .claude/skills/standards/quality.md

<checklist> (test quality sections: quality.md §A + §B + §C + §D + §E + §G)

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
```

**Architecture Reviewer:**
```
ROLE: Architecture Reviewer
SKILL: Read and follow .claude/skills/reviewer-architecture/SKILL.md

Standards files (for reference):
- .claude/skills/standards/correctness-patterns.md

<checklist> (architecture sections: correctness-patterns.md Retry Scope + Dual Code Paths + Derived Data + quality.md §F + §G)

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this bead implements>
REFERENCE DIRS: <key directories in the existing codebase to compare against>
```

#### c. Handle Review Results

- **Trivial issues** (typos, minor naming): fix directly via `git -C <worktree_path>` commands, commit, then push the fix to the same branch
- **Non-trivial issues** (bugs, missing tests, duplication): file a beads issue, spawn an implementer subagent in the same worktree, close when fixed

After all issues resolved, re-run quality gates. **Delegate to a test-runner sub-agent** so verbose output doesn't pollute the coordinator's context — do NOT run the gates directly with Bash. Use the Agent tool with `subagent_type: "claude"`, `model: "haiku"`, and `run_in_background: false` — run the gate **once, synchronously**; never spawn a second gate run while one is pending, and if re-woken or nudged mid-run, report the pending/complete result instead of restarting it. Pull the command list from the **Quality Gates** / Verification table in CLAUDE.md, scoped to the **changed package(s) and their dependents** — never the whole monorepo (e.g. `turbo run test --filter=...[origin/main]`, replace for your stack; the full suite is CI's job on push):

```
ROLE: Test Runner
SKILL: Read and follow .claude/skills/test-runner/SKILL.md

WORKTREE: <worktree_path>
COMMANDS:
- <quality-gate commands matching the changed code, scoped to the changed package(s) and dependents>
```

**Do NOT push if the sub-agent reports FAIL.** Fix locally first (spawn an implementer if the fix is non-trivial), then re-delegate.

#### c2. Run the Real Acceptance — against reality, NOT mocks

Reviewers + unit tests prove the code is *shaped* right; they do **not** prove it *works against reality*. A green mock suite passes even when the real model id, API auth, data shape, or DB behavior is wrong (lived example: a local `db:verify` reported "no drift" and all unit tests were green, while CI's from-zero migrate — the real check — failed on the same change). So **before pushing**, run the bead's **real-acceptance check** — the runnable check in its `## Real acceptance` section — and capture its actual output.

- **Real** = against the real dependency: a live API/model call, real corpus data, a real dev-DB integration. Never a mock. (See `standards/quality.md` §H — mocks are the fast CI gate, never the acceptance bar.)
- Run it from the worktree with the real creds/data it needs (e.g. `set -a; source <env>; set +a; tsx <worktree>/<live-check>.ts`).
- **If the real-acceptance check fails, do NOT push.** Fix (or spawn an implementer), re-run. A green unit suite with a failing real-acceptance is a FAIL.
- Capture the check's key output **verbatim** — it goes into the `BEAD COMPLETE` block so the human approves on *evidence* ("dim 1024; cosine 0.83 vs 0.11"), not on "tests passed."
- **Migration/schema beads** (reality can't be fully exercised pre-merge): the real acceptance is the **from-zero apply on a real Postgres** — CI's `migration-check` plus the Vercel preview migrate — NOT a local `db:verify` on an already-migrated branch.

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

## Background (from first principles)
<Explain from first principles, assuming no prior knowledge. Motivate WHY
before HOW. Build understanding layer by layer. Be concise at each step.
Write for a reader with no background on this work — or who has forgotten
what they were working on: what problem exists, why it matters, and what
this change does about it, before any implementation detail.>

## Changes
<list of significant files changed and what changed in each>

## Test plan
- [ ] Tests pass
- [ ] <any manual verification steps specific to this change>

Bead: <bead-id>

Generated with Claude Code
```

#### e. Approval Gate

> **Autonomous Loop Mode:** skip this human gate. Instead run the win-condition eval and let the triage step decide continue/stop (see "Autonomous Loop Mode"). The rest of §4 (push, PR, beads status) is unchanged.

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
Real acceptance: <the real check that ran> → <its actual output / verdict — the evidence, not "passed">
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

## Autonomous Loop Mode

Entered when the epic carries a `## Win Condition` **and** the user confirms an autonomous run at the start of `/work` (ask once — this is the manual "start the loop" step; never start unattended without it). Everything in Phase 2 still applies per bead; the only change is that the §4e human approval gate is replaced by the win-condition eval inside an outer loop.

**Read first:**
```bash
bd show <epic-id> --json        # read the ## Win Condition block
```
Then load `.claude/skills/triage/SKILL.md` and `.claude/skills/win-condition/SKILL.md`.

**The eval lives out-of-tree** at the outer (un-versioned) `.claude/loop-evals/<epic-id>/` — never in the repo. If it doesn't exist yet, authoring it is the loop's first action (per win-condition R2). It runs locally as you (Claude Code), so it may reach the dev system directly — Neon Dev, Vercel, dev Trigger — to fetch / parse / seed / insert / update / delete.

**Confirm guardrails once** before starting: max-iterations N, **per-bead max attempts (default 3)**, the stagnation thresholds (from the win-condition), and that merges stay manual. Initialize a loop log (e.g. `.claude/loop-<epic-id>.log`), one line per iteration.

**Each iteration:**

1. **Triage** — follow the triage skill. Run the win-condition's verification; if it passes **and** its output ends with the success sentinel → exit **SUCCESS** (triage must cite the evidence). Otherwise get exactly one action: `RUN_BEAD <id>` / `NEW_BEAD <desc>` / `QUICK_FIX <desc>` / `STOP <reason>`.
2. **Execute (bounded per-bead fix-loop)** — carry out the action via the existing Phase 2 machinery: its own worktree + branch, implementer subagent, all three reviewers, quality gates, **and the bead's acceptance check** (the integration check for risky beads, per the win-condition skill — not just unit gates). One action = one PR (drift containment). **No human approval gate** in this mode.
   - If reviewers / gates / acceptance fail: have **triage diagnose** the failure (see triage's "Per-bead failure diagnosis"), then re-spawn the implementer with that diagnosis as context. Repeat up to **per-bead max attempts (default 3)**.
   - **Carry state across attempts**: append each attempt's failure + diagnosis to the loop log (and the bead notes) so attempt N+1 sees what N tried — this is what stops it repeating the same mistake or drifting on stale context.
   - Still failing after the cap → mark the bead **BLOCKED**, log it, and let the outer triage decide: try a different ready bead, or stop the run.
3. **Verify** — re-run the win-condition's runnable check. Dual-condition: it must exit 0 **and** its output must end with the success sentinel `<promise>WIN</promise>` (printed by the eval, not the implementer). Absence of errors alone is never success.
4. **Record** — append to the loop log: action, result, verification output (exit code + key line), files touched, and approximate token cost (track cost-per-accepted-change; if the accept rate craters, stop and re-tune rather than burning tokens).
5. **Stop checks** — exit the loop if:
   - win-condition met → **SUCCESS**
   - max-iterations reached → **BLOCKED**
   - stagnation: no progress in 3 iterations, or the same error 5 times → **BLOCKED**

**On SUCCESS:** output the run summary and the open PRs. Merging is still the human's job.

**On BLOCKED:** produce a handoff-style report (in chat) — what the win-condition still needs, what each iteration attempted, the blocking error, and the single most likely next action. Do **not** keep looping.

**Irreversible actions stay OUTSIDE the loop body** — merging PRs, deploying, deleting data, rotating secrets. The loop only ever produces reviewed PRs; a human (or a separate gated step) merges.

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
- Running quality gates directly in coordinator context — always delegate to a test-runner sub-agent (`model: "haiku"`) so verbose output doesn't pollute context
- Idling on a background gate run, or re-spawning a gate run when nudged instead of reporting the pending result
- Treating unit tests / mocks as the acceptance bar — the **real-acceptance check** (§4c2: live dependency / real data / real from-zero DB, run before the gate) is what makes a bead "done"; mocks are only the CI gate
- Approving a bead on "tests passed" instead of on the real-acceptance **evidence** (the actual output) in the `BEAD COMPLETE` block
- Merging PRs (that's the human's job)
- Watching CI (that's the human's job)
- Cleaning up worktrees before merge (that's the human's job — `/merged` handles it)
- Sharing a worktree across multiple beads — each bead gets its own named worktree
- Running dependency installs concurrently across multiple worktrees
- Fixing non-trivial review issues inline — file issues and spawn implementers instead
- Using `isolation: "worktree"` for implementers — worktrees are ephemeral and disappear before reviews run; always create worktrees manually with `git worktree add`
- Omitting `--repo` from `gh` commands — always derive it from `git remote get-url origin`
- (Autonomous Loop Mode) Performing irreversible actions inside the loop — merging, deploying, deleting data, and rotating secrets all stay outside the loop body
- (Autonomous Loop Mode) Committing the loop eval into the repo or filing it as a bead/PR — it is local-only scaffolding under the outer `.claude/loop-evals/<epic-id>/`
- (Autonomous Loop Mode) Treating "no errors" / exit 0 as success — gate on the positive win-condition assertion (rows landed, expected output), never on absence of errors alone
- (Autonomous Loop Mode) Looping past the max-iteration or stagnation caps instead of stopping with a handoff
- (Autonomous Loop Mode) Starting an unattended run without the user's go-ahead, or on an epic with no `## Win Condition`
