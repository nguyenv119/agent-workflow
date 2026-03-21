# A Concrete Workflow

A full walkthrough of building **JWT authentication with a login endpoint** — from planning to merged PR.

---

## Why This Exists

An agent's context window is finite. A large feature doesn't fit. If you try, the agent loses track of what it read earlier by the time it finishes.

The fix: break big work into small tasks, give each task to a fresh agent with a clean context window, and track state externally — in [beads](https://github.com/jdelfino/beads) and git, not in any agent's memory.

---

## Two Files Every Agent Reads

**`CLAUDE.md`** — your project's permanent briefing. Build commands, test commands, conventions. Loaded at the start of every session.

**`AGENTS.md`** — workflow rules and beads usage. Loaded automatically via a `SessionStart` hook, so every agent knows the system without being told.

---

## Path 1: Full Flow (big or uncertain features)

### `/plan "add JWT authentication with a login endpoint"`

The planner explores your codebase first — existing patterns, shared types, files that will be affected. Then it discusses tradeoffs with you: refresh tokens? Cookie or header? What does "done" look like?

You agree on an approach. The planner files tasks:

```
epic: bd-a3f8   "JWT Authentication"
  └── bd-a3f8.1  "Create users table and Prisma schema"
  └── bd-a3f8.2  "POST /login endpoint"                  ← depends on .1
  └── bd-a3f8.3  "JWT middleware for protected routes"    ← depends on .2
```

Each task is self-contained — a future agent with no memory of this conversation can implement it from the description alone.

A plan reviewer checks for gaps, dependency errors, and pattern violations. Once approved, planning is done.

### `/work bd-a3f8` (run 1)

The coordinator checks: which tasks are unblocked? Only `bd-a3f8.1` — the other two are waiting on it.

For `bd-a3f8.1`, the coordinator:

1. Creates a worktree and branch: `feature/bd-a3f8.1-users-table`
2. Spawns an **implementer** (Sonnet) in that worktree — writes failing tests first, implements, verifies, audits coverage, commits
3. Reads both standards files (`quality.md`, `correctness-patterns.md`) and constructs a **per-reviewer checklist** — each reviewer gets only its relevant sections, injected directly into the prompt
4. Spawns three **reviewers** in parallel — each responds to every checklist item with N/A/PASS/FAIL, then gives a verdict. A hook verifies the standards content was actually injected (not just referenced)
5. Fixes trivial findings, files issues for non-trivial ones
6. Pushes and opens a PR

The handoff includes a **review guide** — which tests to read first, ordered by dependency:

```
BEAD [1] COMPLETE
Tests passing: npm test
Branch: feature/bd-a3f8.1-users-table
PR: https://github.com/org/<REPO>/pull/<ID>

Review guide (read tests in this order):
1. tests/models/user.test.ts — schema validations; foundational
2. tests/db/migrations.test.ts — migration correctness; builds on #1
(Implementation files are backup if tests leave questions.)
```

Why tests first? Tests are the specification. If the tests are right and passing, the implementation is right by definition.

The coordinator stops here. `.2` and `.3` are still blocked.

### You review and merge

Review the PR on GitHub. If changes are needed, `/work bd-a3f8.1` picks up the existing branch and pushes fixes. When satisfied, squash merge.

### `/merged feature/bd-a3f8.1-users-table`

Verifies the merge, closes the bead, removes the worktree, deletes the branch. Now `bd-a3f8.2` is unblocked.

### `/work bd-a3f8` (run 2)

Same cycle for `bd-a3f8.2`. Its worktree branches from the updated `origin/main`, which now contains bead 1's merged code. The implementer inherits exactly the state it depends on.

`bd-a3f8.3` follows after `.2` is merged.

---

## Path 2: Lightweight (small, clear tasks)

```
/work "add rate limiting to the login endpoint"
```

The coordinator creates a bead inline and runs the full cycle — worktree, implementer, reviewers, PR. Same output, no planning ceremony.

---

## The Full Picture

```
/plan "JWT authentication"
  planner explores codebase → discusses with you → files tasks with dependencies
  plan reviewer validates → APPROVED

/work bd-a3f8  (run 1 — only .1 is ready)
  coordinator creates worktree from origin/main
  implementer: failing tests → implement → verify → coverage review → commit
  coordinator reads standards files → builds per-reviewer checklists
  3 reviewers in parallel (standards injected as checklist items):
    correctness → N/A/PASS/FAIL per item → verdict
    tests       → N/A/PASS/FAIL per item → verdict
    architecture → N/A/PASS/FAIL per item → verdict
  fix findings → push → PR created with review guide → STOP

  you review, merge on GitHub

/merged feature/bd-a3f8.1-users-table
  verify merge → close bead → remove worktree → delete branch

/work bd-a3f8  (run 2 — .2 now unblocked, branches from updated main)
  same cycle → STOP

  you review, merge

/merged → /work bd-a3f8 (run 3 — .3) → ...
```

The human gate between tasks is intentional. A broken foundation must be caught before dependent work builds on it.

---

## Standards Enforcement

Shared standards (`quality.md`, `correctness-patterns.md`) encode quality rules learned from production incidents — mock discipline, refactor cleanup audit, race condition patterns, etc. The challenge: how do you guarantee a spawned reviewer agent actually uses them?

**The approach:** Don't tell reviewers to "go read this file" — inject the relevant content directly into their prompt as a numbered checklist. A `PreToolUse` hook on the Agent tool verifies the coordinator actually did the injection by checking for content signatures (section headers from the standards files). If the coordinator tries to spawn a reviewer with ad-hoc instructions instead of the canonical checklists, the hook blocks the call.

```
Coordinator reads standards files
  → extracts relevant sections per reviewer type
  → injects as numbered checklist in reviewer prompt
  → hook verifies content signatures before allowing spawn

Reviewer responds to each checklist item
  → N/A (doesn't apply), PASS (checked, clean), or FAIL (issue found)
  → verdict: APPROVED or CHANGES NEEDED
```

This moves standards compliance from probabilistic (instruction-following) to deterministic (mechanical enforcement).

---

## Multi-Machine Collaboration

Everything above works on one machine. The beads database is local — a Dolt `.db` file in `.beads/dolt/`.

**The problem:** Machine A creates a task, Machine B doesn't see it. The database doesn't leave your disk.

**The fix:** Dolt speaks git. It has `dolt push` and `dolt pull` — same semantics, but for database rows instead of files. DoltHub is the GitHub equivalent.

**How it's wired:** Two Claude Code hooks make sync transparent:

- Before any `bd` command → `dolt pull` (get latest)
- After any `bd` write command → `dolt push` (share changes)

Both no-op when no remote is configured. Network failures never block the agent.

**Setup:** `/setup-remote` (one-time per project) — authenticates with DoltHub and connects the database.

**What this doesn't solve:** Two machines can still grab the same task simultaneously. On one machine, the coordinator prevents this. Across machines, coordination is an open problem. For small teams, communication works. For large teams, you'd need a locking layer on top.
