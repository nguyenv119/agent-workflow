---
name: win-condition
description: Define a measurable win-condition (the eval) for an epic — the runnable check plus stop conditions an autonomous loop gates on. Included by the planner during /plan; consumed by the coordinator in Autonomous Loop Mode.
---

# Win Condition

An autonomous loop is only as good as the win-condition it stops on. This skill turns a user's **high-level** goal ("both sources ingest end-to-end in dev without me doing anything") into a **measurable eval** the loop can run every iteration to decide continue vs. stop.

You discuss the high-level outcome with the user, then **derive the technical eval yourself** — that derivation is the hard part and the whole point of this skill.

## Core principle

Define a verifiable **end state**, not a process. "Fix the ingestion" is a process. "Both feeds land valid rows in dev, unattended, with zero unhandled errors" is an end state you can check.

## Hard rules

**R1 — Positive signal required. Absence of errors is never sufficient on its own.**
A pipeline can exit 0 while ingesting nothing: empty feed, a silent auth failure that returns 200, an over-eager filter. So every win-condition must assert a *positive, observable* outcome (rows landed, endpoint returns the expected shape, file exists with content). "No errors" may appear only as one clause *inside* a positive assertion — never as the whole gate.

**R2 — The eval is local loop scaffolding, never a repo artifact.** It lives out-of-tree under the outer (un-versioned) `.claude/loop-evals/<epic-id>/`, authored by the agent at loop setup — never committed, never a bead, never a PR. It runs on your laptop *as Claude Code*, so it may reach the dev system directly (Neon Dev, Vercel, dev Trigger) to fetch / parse / seed / insert / update / delete; that direct access is *why* it never needs to ride into the repo. Do not fall back to the unit-test suite (unit ≠ end-to-end) or to a blind LLM judge. If no runnable end-to-end check exists yet, the loop's first action is to author one under `.claude/loop-evals/`.

**R3 — Runnable check first; LLM judge only as a backstop.** A command that exits 0/1 is the primary signal. Claude Code's `/goal` (a Stop hook that asks a fast model yes/no after each turn) is a backstop only, and is trustworthy *only when handed evidence* — e.g. the result of a `SELECT count(*)…`, not "does it look done?" Prefer to assert the evidence in a script.

**R4 — Bound it.** The win-condition is a bounded *build-completion* state, then the loop stops. It is NOT "run forever." Long-running production behavior (continuous polling) belongs to prod scheduling (Trigger.dev), not the agent loop.

**R5 — The eval emits the success sentinel, never the implementer.** The verification script prints `<promise>WIN</promise>` as its final line only when all positive assertions pass. The worker that made the change never declares its own success — independence comes from the eval testing reality (it queries the live system), not from the worker's word. The sentinel + exit-0 together guard a script that exits 0 without finishing its assertions.

## The `## Win Condition` block (write this onto the epic bead)

Every loopable epic carries this block in its body. Five fields, all required:

```
## Win Condition

**Outcome**: <one sentence, positive + observable end state>

**Verification**: <exact command(s) that prove the outcome, exit 0 = pass>
<!-- the eval lives out-of-tree at .claude/loop-evals/<epic-id>/ — never committed; author it there if it doesn't exist yet -->

**Done signal (dual-condition)**: verification exits 0 AND its output ends with the
success sentinel `<promise>WIN</promise>`, which the verification script prints only
after every positive assertion passes. Both required.

**Does NOT count**:
- absence of errors with no positive output (R1)
- any manual step during the run ("unattended" means zero human input)
- <epic-specific false-positives>

**Stop conditions**:
- max-iterations: <N>            # hard cap, always set
- stagnation: no progress in 3 iterations, or the same error in 5 → BLOCKED
```

## How to derive it (the process you run)

1. **Restate the goal as a positive end state.** Push back on negative phrasings ("no errors") until you have something observable.
2. **Locate the verification.** Is there a script/command that runs the real thing end-to-end and inspects the result? If yes, that's your check. If no → R2: author it under `.claude/loop-evals/<epic-id>/` (local scaffolding, never a bead).
3. **Write the runnable assert.** Run the actual flow in dev, then check state. Make failure loud (non-zero exit) and success specific (counts/shape/idempotency), per R1.
4. **Set the caps** (max-iterations + stagnation) per R4 and the dual-condition done signal per R3/R5 (eval exits 0 AND prints the success sentinel on its last line).
5. **Write the block onto the epic** and confirm the outcome wording with the user.

## Worked example — source ingestion

High-level goal from the user: *"Sources X and Y ingest end-to-end in dev without me doing anything."*

Derived win-condition:

```
## Win Condition

**Outcome**: A dev run of the pipeline ingests feeds X and Y end-to-end,
landing valid `pipeline.content` rows for each, with no human intervention.

**Verification**: .claude/loop-evals/<epic-id>/ingest-e2e.sh X Y
  # runs the dev pipeline against both feed-slugs for one poll window, then asserts:
  #   - pipeline.content rows for each feed-slug >= 1 (Neon query)
  #   - required fields non-null; no duplicate rows on a second run (idempotent)
  #   - unhandled-error count == 0 over the run
  # exits 0 only if all hold; on full success, prints <promise>WIN</promise> as its last line

**Done signal (dual-condition)**: ingest-e2e.sh exits 0 AND its last line is
<promise>WIN</promise> (printed only after all asserts pass).

**Does NOT count**:
- the run completes but lands 0 rows (R1 — empty ingestion is not success)
- rows land only after a manual reseed or manual token paste
- errors are swallowed/logged but the row count is still 0

**Stop conditions**:
- max-iterations: 25
- stagnation: no new passing assertion in 3 iterations, or same error in 5 → BLOCKED
```

If the eval doesn't exist yet, the loop authors it under `.claude/loop-evals/<epic-id>/` as its first action — it is **not** a filed bead and never enters the repo.

## Bead-level acceptance (risk-tiered)

The epic win-condition gates the whole loop. Each **bead** also needs an acceptance check, but the strength depends on risk:

- **Standard beads** (pure code, UI, refactors) — the existing gate is enough: the 3 reviewers + quality gates (lint/types/unit) + the bead's observable `## Acceptance criteria`.
- **Risky beads** (DB schema/migrations, shared infra, cross-service contracts, anything that can affect prod) — unit tests are **not** enough. These get a **local integration check** under `.claude/loop-evals/<epic-id>/` (same out-of-tree model as the epic eval, R2), run by Claude Code with direct dev access. It follows R1 (positive signal) and prints the sentinel on success.

**Why units fail here:** a unit test mocks the boundary most likely to break. A migration can pass every unit and still fail to apply, lock a table, drop data, or break the running prod app during the deploy window. Only a real run catches that.

**Schema-change acceptance — worked example** (the migration workflow lives in `packages/db/CLAUDE.md`; Neon branching makes this cheap and prod-safe):
1. Branch from `production`.
2. Apply the migration on the branch.
3. Run the app's real queries / boot against the branch; assert they pass.
4. Verify **backward compatibility** (expand-contract: the *old* code still works against the *new* schema) and that rollback is clean.
5. Exit 0 + print the sentinel only if all hold.

Backward compatibility is the load-bearing assertion — it's what proves the change won't break prod mid-rollout, which no unit test can. This local check is the loop's *gate*; if you also want a durable migration regression test in CI, that's a normal bead with normal tests, separate from the loop.

## What you do NOT do

- ❌ Accept "no errors" / "it runs" / "tests pass (unit)" as the win-condition
- ❌ Leave the verification as prose when it could be a command
- ❌ Write an unbounded win-condition ("keeps ingesting") — that's prod, not the loop
- ❌ Decide the high-level outcome for the user — discuss it, then derive the technical eval
- ❌ Have the implementer (or any worker) emit the success sentinel — the eval prints it
- ❌ Commit the eval into the repo, or file it as a bead/PR — it is local-only scaffolding under the outer `.claude/loop-evals/`
