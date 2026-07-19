---
name: implementer
description: Pure development workflow with test-first development and coverage review. Used by coordinator as a subagent. Commits work but never pushes, never manages beads issues.
---

# Implementer

Follow these phases **in strict order**. Do not skip phases. Do not proceed until the current phase's gate is satisfied.

This skill covers development only — no issue tracking, no pushes. The coordinator handles push and beads lifecycle. The implementer commits its own work.

## Before You Start

Read these files before writing any code:
- `.claude/skills/standards/quality.md` — test structure, mock discipline, refactor audit
- `.claude/skills/standards/correctness-patterns.md` — known bug patterns to avoid while coding

Prevention is better than detection — knowing these patterns avoids creating the bugs reviewers would later catch.

**When invoked with a `WORKTREE` path:** All commands run in that directory. The branch is already checked out — do not create a new one. Verify with `git -C <WORKTREE> branch --show-current` before starting.

## Principles

- Never silently work around problems. Throw errors for missing env vars, invalid state, missing dependencies.
- Mock properly in tests. Do not add production fallbacks to make tests pass.
- No type casts that bypass the type system.
- No optional chaining on required properties.
- **Every production code change requires tests.** No exceptions for migrations, refactors, copy-paste, or "just wiring things up." If you wrote or modified production code, you must write tests for it. Never defer tests to a follow-up issue.
- **Delegate quality-gate runs to a test-runner sub-agent.** Verbose test output consumes your context window — never run the gates directly with Bash. See "Running quality gates" below.

### Mock Discipline

→ See `.claude/skills/standards/quality.md` § C (Mock Discipline) for the full real > in-memory > mock hierarchy and rules.

### Running quality gates

Whenever a phase says to run tests or quality gates, do **not** run them directly — spawn a test-runner sub-agent so the noisy output stays out of your context. Use the Agent tool with `subagent_type: "claude"`, `model: "haiku"`, and `run_in_background: false`:

```
ROLE: Test Runner
SKILL: Read and follow .claude/skills/test-runner/SKILL.md

WORKTREE: <your worktree path>
COMMANDS:
- <the quality-gate commands from the Quality Gates / Verification table in CLAUDE.md, scoped to the changed package(s) and their dependents>
```

The sub-agent replies `RESULT: PASS` or `RESULT: FAIL` with a trimmed error summary. On FAIL, read the summary, fix the issue, and re-delegate. Only run a gate directly in your own context if you need to debug a failure interactively.

**Run-once discipline:** Run gates **once, synchronously** (`run_in_background: false`) and wait for the reply — never launch a gate run in the background and idle/sleep "waiting" for it. Never spawn a second gate run while one is still pending. If you are re-woken or nudged while a gate run is in flight, **report its pending or completed result** — do not restart it; the harness already re-wakes you when a background job finishes, so idling or re-spawning on a nudge only duplicates load.

**Run scope (changed package, not the whole monorepo):** Run only the tests for the **changed package(s) and their dependents** (downstream consumers) — never the whole monorepo locally. The exact scoping command is stack-specific — put it in CLAUDE.md's Quality Gates table and **replace for your stack**; examples: `turbo run test --filter=...[origin/main]` (Turborepo), `nx affected --target=test` (Nx), `pnpm --filter "...[origin/main]" test` (pnpm workspaces), or a Bazel target-scoped query. **Recommended: cap per-run parallelism** (e.g. `--maxWorkers=<~half your cores>`) so one run can't occupy every core. This per-run core cap is the lightest backstop against pile-up — combined with scoping, it keeps concurrent runs from starving the machine *without* needing a machine-global lock, and leaves headroom for sibling worktrees. The **full / timing-sensitive suite is CI's job**, run on push on a clean machine — scoped-local can miss cross-package breaks by design; that's fine because CI is the authoritative gate.

## Phase 0: Announce Approach

Before writing any code or tests, output a brief statement of your plan. This is informational — do **not** wait for approval. Continue immediately to Phase 1.

```
APPROACH: <task-id or "N/A">
Files to touch: <list of files you expect to modify>
Strategy: <1-3 sentences — what you'll do and why>
Key decisions: <any non-obvious choices, or "N/A">
```

Keep it short. The human may read it async and interrupt only if something looks wrong.

## Phase 1: Write Failing Tests

Write tests for the behavior you are about to change or add. Do this **before** touching any production code.

**This phase is NOT optional.** Common excuses that do NOT exempt you from writing tests:
- "It's just a migration" — migrated code has new integration points that need testing
- "It's just wiring up an API client" — API client calls, error handling, and auth headers need tests
- "The old code didn't have tests" — that's a reason to add them, not skip them
- "I'll add tests later" — no, tests ship with the code, always

1. Read the relevant production code to understand current behavior
2. Write new test cases that describe the desired behavior after your change
3. Verify the new tests fail by delegating to a test-runner sub-agent (see "Running quality gates" above)

### Test Structure Requirements

→ See `.claude/skills/standards/quality.md` for all test structure rules (GIVEN/WHEN/THEN, docstrings, naming, mock flagging).

Every test **must** follow those standards — they are not optional. The goal is to minimize review surface: a reviewer reads GIVEN/WHEN/THEN and the docstring, never the implementation.

**Gate:** Your new tests **fail** (or, for pure deletions/removals, you can write tests asserting the old behavior is gone — these will pass after implementation). If your new tests already pass, they are not testing anything new. Rewrite them.

## Phase 2: Implement

**Frontend work:** If the task involves building or modifying frontend UI (components, pages, layouts, styles), invoke the `/frontend-design` skill. It produces distinctive, production-grade interfaces — use it instead of writing frontend UI code from scratch.

**Minimal-code discipline (ponytail):** Before writing production code, apply the `ponytail` skill. Climb its ladder and stop at the first rung that holds: (1) does this need to exist at all? (YAGNI) (2) stdlib does it? (3) native platform feature covers it? (4) already-installed dependency solves it? (5) can it be one line? (6) only then, the minimum code that works. Ship the shortest diff that fully does the task. Mark deliberate simplifications with a `ponytail:` comment naming the upgrade path. This does **not** relax anything else in this skill: input validation at trust boundaries, error handling that prevents data loss, security, accessibility basics, and the test requirements below are never simplified away — lazy means writing less code, not cutting the checks.

Make the production code changes. Keep changes minimal and focused on the task.

## Phase 3: Verify

Run quality gates — scoped to the changed package(s) and their dependents, not the whole monorepo (see "Running quality gates" above) — by delegating to a test-runner sub-agent. Pull the command list from the **Quality Gates** / Verification table in CLAUDE.md.

**Gate:** The sub-agent reports `RESULT: PASS`. If it reports FAIL, read the error summary, fix the issues, and re-delegate before proceeding.

### Real-acceptance artifact (not just mocks)

The unit/quality gates above are the **CI gate** — they prove the code is shaped right. They are **not** the acceptance bar (see `standards/quality.md` §H). The bead's `## Real acceptance` names a check **against reality**: a live API/model call, real corpus data, or a real dev-DB integration. If that runnable artifact doesn't exist yet, **build it** — a small `*.live-check.ts` script or a real integration test — so the coordinator can run it for real. Do **not** run a live check needing credentials you weren't given; leave it runnable and record its path + exact command in your Phase 5 summary.

## Phase 4: Test Coverage Review

This is an audit, not a formality. Evaluate whether your tests actually cover the changes you made.

### Step 1: List what changed

```bash
git diff --name-only
```

Separate the output into production files and test files.

### Step 2: For each changed production file, evaluate

- **What behavior changed?** (new feature, bug fix, removed feature, refactored logic)
- **What existing tests cover this file?** Read the corresponding test file if one exists.
- **Are there gaps?** Specifically:
  - Happy path for new/changed behavior
  - Error paths and edge cases
  - Regression test if this is a bug fix (a test that would have caught the original bug)
  - Boundary conditions

### Step 3: Refactor cleanup audit

→ See `.claude/skills/standards/quality.md` § F (Refactor Cleanup Audit). Run this check on every modified function.

### Step 4: Evaluate integration test needs

Integration tests are needed when changes affect:
- Repository/persistence layer (database queries, data mapping)
- API routes that combine multiple services
- Auth flows or permission checks
- Data flowing across multiple layers

If integration tests are needed, write them.

### Step 5: Fill gaps

Write any missing tests identified above. Then re-run quality gates via the test-runner sub-agent (see "Running quality gates" above).

**Gate:** All tests pass, including your new coverage additions. If you identified no gaps in Steps 2-3, document your reasoning (e.g., "Changes were purely deletions; added regression tests in Phase 1 confirming removed elements no longer render").

## Phase 5: Commit and Summary

First, commit all changes:
```bash
git add -A
git commit -m "<type>(<scope>): <short description>

Bead: <task-id>"
```

**This must be the very last thing you output.** The coordinator reads your result — keep it concise to avoid polluting its context.

Produce exactly this and nothing else after it:

```
IMPLEMENTATION RESULT: SUCCESS | FAILURE

Task: <task-id or "N/A" if not provided>
Commit: <full commit hash, or "N/A" on failure>

## What changed
- <1 bullet per logical change, max 5>

## Files modified
- <path> — <what changed in 1 phrase>

## Test coverage
- <1 bullet per test file added/modified, what it covers>

## Real acceptance
- <the runnable real-acceptance check (path + exact command), and its output if you ran it; else "left for coordinator to run — needs <creds/data>">

## Concerns
- <anything the coordinator should know, or "None">
```

If implementation failed, replace "What changed" with:

```
## Error
<what went wrong — 1-3 sentences>

## Attempted
- <what you tried>
```
