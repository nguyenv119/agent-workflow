---
name: quick
description: Lightweight PR flow for small, low-risk fixes — one reviewer instead of coordinator's three, still a real worktree/branch/PR, still gated by tests and (when the bead defines one) a real-acceptance check. Refuses and hands off to full coordinator/work when a change is too large or touches anything risky.
---

# Quick

A smaller sibling of `coordinator`, for changes that don't need the full
machinery: single-file or few-line fixes, typo/copy corrections, small
config tweaks, one-off bug fixes with an obvious cause. The point isn't to
skip rigor — it's to match rigor to actual risk, per how the industry
already tiers AI-authored PR review: *"a config change earns a linter and a
glance; a revision to core business logic earns the full stack."* Quick is
the "linter and a glance" tier — never zero review, always less ceremony.

**Model guidance:** the implementer runs at the parent's normal
model/effort — a small diff still deserves correct code. Only the *review*
layer is lightened: the single reviewer spawns as the `quick-reviewer`
custom agent type (`.claude/agents/quick-reviewer.md`), which carries its
own lower effort level in its own definition file, not something set at
call time.

## Stay quiet during execution

Same rule as `coordinator`: nothing between the gate and the final report
gets narrated to chat — no "implementer's done, spawning the reviewer now,"
no "reviewer found a trivial issue, fixing it." Do the work, then surface
exactly one thing: `QUICK FIX COMPLETE` (Step 7) once it's actually true. The
only thing that interrupts this is Step 1 refusing and handing off — that's
a decision the user needs to see, not progress noise.

## Step 1 — The gate (refuse before you start, don't half-review something risky)

Qualifies for Quick only if **both** hold:

1. **Small, OR mechanically uniform.** Either the diff is small (roughly one
   file, or a handful of lines across a couple of files), **or** it's a bulk
   change where the *mechanism* is uniform and low-risk even though the line
   count is large — near-100% deletions (`git rm -r` across many files), a
   scripted rename, a generated-file regen. Line count is a proxy for
   complexity, and bulk mechanical deletion is exactly the case where that
   proxy breaks: a 12,000-line `git rm -r` + two greps is lower actual risk
   than a 15-line change to tricky logic. Judge the mechanism, not just the
   number. If you're arguing "it's medium-sized but simple" for something
   that ISN'T mechanically uniform (real logic, just not much of it), that's
   still a signal to hand off — this exception is narrow on purpose.
2. **Not on the risk deny-list.** Does not touch:
   - DB schema / migrations (`packages/db/drizzle/`, schema files)
   - Auth / permissions code
   - Payment / billing code
   - CI/CD config (`.github/workflows/`, deploy configs)
   - Widely-imported shared/core infra (a change here has blast radius no
     matter how small the diff looks)
   - Secrets, env handling, anything prod-facing in a way that isn't purely
     additive

**If either fails, stop and hand off to `coordinator`/`work` instead** — say
so plainly ("this touches migrations, routing to the full flow") rather than
attempting a lighter review on something that needed the full stack. Quick
existing at all depends on it refusing the cases it isn't built for.

## Step 2 — Worktree, same as always

Create the worktree and branch exactly like `coordinator` does — no
shortcut here, this was a deliberate call, not an oversight:

```bash
MAIN_ROOT=$(git worktree list --porcelain | grep '^worktree' | head -1 | awk '{print $2}')
BRANCH="feature/bd-<id>-<slug>"
WORKTREE_PATH="${MAIN_ROOT}/../$(basename $MAIN_ROOT)-bd-<id>-<slug>"
git -C "$MAIN_ROOT" worktree add "$WORKTREE_PATH" -b "$BRANCH" origin/main
```

Install dependencies in the worktree before doing anything else in it.

## Step 3 — Implement

Spawn a single implementer subagent (same `implementer` skill coordinator
uses), at the parent's normal model — do not downgrade the effort/model that
writes the code, only the review that checks it:

```
ROLE: Implementer
SKILL: Read and follow .claude/skills/implementer/SKILL.md

TASK: <task-id or the ad-hoc description>
WORKTREE: <worktree_path>
BRANCH: <branch>

CONSTRAINTS:
- Do NOT modify beads issues
- Your working directory is <worktree_path>
- The branch <branch> is already checked out; do NOT create a new branch
- Commit your work when done (do NOT push)
```

## Step 4 — One reviewer, not three

Read `quality.md` and `correctness-patterns.md` fresh from the worktree
(same rule coordinator follows — don't rely on memory, they may have
changed). Build the same correctness checklist coordinator's correctness
reviewer uses: `correctness-patterns.md` ALL sections + `quality.md` §F
(Refactor Cleanup Audit) + §G (Review Discipline).

Spawn exactly one reviewer, using the `quick-reviewer` agent type (its
effort level lives in its own definition, not set here):

```
ROLE: Quick Reviewer
SKILL: Read and follow .claude/skills/reviewer-correctness/SKILL.md

<checklist> (same correctness sections coordinator would use)

WORKTREE: <worktree_path>
BASE: origin/main
SUMMARY: <what this fix does>
```

Use the Agent tool with `subagent_type: "quick-reviewer"`.

**Handle findings:** trivial issues (typo, minor naming) — fix directly and
commit. Anything non-trivial (a real bug, a missing edge case) — this is a
signal the change wasn't as simple as the gate assumed; consider handing off
to `coordinator`/`work` for a fuller pass rather than patching around it here.

## Step 5 — Tests (Haiku, same as coordinator)

Delegate quality gates to a test-runner sub-agent exactly like coordinator
does — never run them directly in this context:

```
ROLE: Test Runner
SKILL: Read and follow .claude/skills/test-runner/SKILL.md

WORKTREE: <worktree_path>
COMMANDS:
- <quality-gate commands matching the changed code>
```

Use the Agent tool with `subagent_type: "claude"`, `model: "haiku"`. Do not
push if it reports FAIL — fix, then re-delegate.

## Step 6 — Real acceptance, only if the bead defines one

If the bead's description carries a `## Real acceptance` section, run it and
capture the actual output before pushing — same rule as coordinator (mocks
are the CI gate, never the acceptance bar). Most genuinely small, low-risk
fixes won't have one; don't invent a requirement the bead doesn't carry.

## Step 7 — Push, PR, report

Push and open the PR exactly like coordinator does (same branch/PR
mechanics — derive repo from `git remote get-url origin`, `gh pr create` /
`gh pr edit`). Never merge, never push to main.

Final verdict only, plain language, a few sentences — no review-guide
construction, no field-by-field dump:

```
QUICK FIX COMPLETE
<bead-id>: <one plain sentence — what changed and why>. Tests pass<, real
acceptance: one plain-language line if it ran>. PR: <url>.
```

## Constraints

- **NEVER** attempt a change that fails the Step 1 gate — hand off instead.
- **NEVER** skip the worktree, the test-runner gate, or the PR/branch
  discipline, regardless of how small the change is.
- **NEVER** spawn more than one reviewer — if one reviewer isn't enough,
  that's a sign this bead needed `coordinator`/`work`, not a reason to add
  a second Quick reviewer.
- **ALWAYS** use the `quick-reviewer` agent type for the review step, not a
  generic subagent — its lower effort level is what makes this actually
  cheaper, not just fewer reviewers.
