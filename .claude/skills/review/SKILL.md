---
name: review
description: Process external review feedback (Devin, human), fix issues, run internal reviewers, and improve the AI harness so the same class of miss doesn't recur.
---

# Review Feedback Loop

You process external review feedback on a PR, fix every issue, analyze why internal reviewers missed each one, and update the harness to prevent recurrence.

**This is NOT the same as /work.** `/work` implements features. `/review` iterates on review feedback after implementation. The two can alternate: `/work` → push → Devin review → `/review` → push → Devin review → `/review` → merge.

## What You Receive

A PR URL (e.g., `https://github.com/org/repo/pull/123`) and optionally a worktree path.

## Phase 1: Gather Feedback

### 1. Fetch the PR and all review comments

```bash
REPO=$(echo "<pr-url>" | sed 's|https://github.com/||' | sed 's|/pull/.*||')
PR_NUM=$(echo "<pr-url>" | grep -o '[0-9]*$')

# PR metadata
gh pr view $PR_NUM --repo $REPO --json title,headRefName,baseRefName,files

# All review comments (inline + general)
gh api repos/$REPO/pulls/$PR_NUM/comments --jq '.[] | {path: .path, line: .line, body: .body, user: .user.login, createdAt: .created_at}'

# General PR comments
gh pr view $PR_NUM --repo $REPO --json comments --jq '.comments[] | {author: .author.login, body: .body, createdAt: .createdAt}'
```

### 2. Identify the worktree

Find the existing worktree for the PR's head branch:
```bash
git worktree list
```

If no worktree exists, ask the user where the code lives.

### 3. Triage findings

For each review comment, classify it:

- **Already fixed** — the code already addresses this (from a prior `/review` run). Skip it.
- **Actionable** — a real issue that needs a code change. Proceed to Phase 2.
- **Disagree** — the suggestion would make things worse or is incorrect. Note it for the user but don't fix it.

Present the triage to the user:

```
REVIEW TRIAGE: <PR title>
Source: <reviewer name> (<n> comments)

Already fixed:
- <description> — fixed in commit <hash>

Actionable (<n>):
1. <file:line> — <short description>
2. <file:line> — <short description>

Disagree (<n>):
- <description> — <why>

Proceeding to fix <n> actionable items.
```

## Phase 2: Fix

For each actionable item:

1. Read the relevant code
2. Make the fix
3. **Refactor cleanup audit** — after every fix, check the modified function for:
   - Dead variables (declared but never read after the change)
   - Stale comments (describing the old code, not the new code)
   - Unused imports
   This step is mandatory. It exists because fix-on-fix commits are the #1 source of orphaned artifacts.

## Phase 3: Verify

### 1. Run quality gates

Run the project's quality gates (typecheck, tests) per CLAUDE.md.

### 2. Run internal reviewers

**This is mandatory, not optional.** Spawn all 3 reviewers in parallel on the worktree:

- **Correctness Reviewer** — `.claude/skills/reviewer-correctness/SKILL.md`
- **Test Quality Reviewer** — `.claude/skills/reviewer-tests/SKILL.md`
- **Architecture Reviewer** — `.claude/skills/reviewer-architecture/SKILL.md`

If any reviewer finds issues, fix them before proceeding. Then re-run quality gates.

### 3. Commit and push

Commit all fixes in a single commit with a clear message explaining what was fixed and why. Push to the PR branch.

## Phase 4: Harness Retrospective

This is the feedback loop that makes the harness improve over time.

For each actionable item that was fixed, answer:

1. **Which internal reviewer should have caught this?** (correctness, architecture, tests, or implementer)
2. **Why didn't it?** Categorize the miss:
   - **Missing check** — the reviewer skill doesn't have a section covering this class of issue
   - **Check exists but too vague** — the section exists but the description wasn't specific enough to trigger on this pattern
   - **Check exists but was skipped** — the reviewer was never spawned (e.g., "optional for small changes" exception)
   - **Tooling gap** — a linter/compiler flag would catch this automatically (e.g., `noUnusedLocals`)
   - **Novel pattern** — genuinely new class of issue not seen before
3. **What harness change would prevent recurrence?** Propose a specific edit to a specific skill file.

Present the retrospective:

```
HARNESS RETROSPECTIVE

| # | Finding | Should catch | Miss type | Proposed fix |
|---|---------|-------------|-----------|-------------|
| 1 | <desc> | reviewer-correctness | missing check | Add "X" section to reviewer-correctness/SKILL.md |
| 2 | <desc> | implementer | tooling gap | Enable Y in project config |
| 3 | <desc> | reviewer-architecture | check too vague | Expand "Z" section with specific pattern |

Apply harness changes? (Will edit skill files in .claude/skills/)
```

Wait for user approval before editing skill files. If approved, make the edits.

**Rules for harness edits:**
- Keep additions language-agnostic (not hardcoded to TypeScript, Python, etc.)
- Add specific patterns and examples, not vague guidance
- Include a "Why this matters" line so future readers understand the motivation
- If a tooling gap is identified, phrase it as "check whether the project has X enabled" rather than "enable X"

## Phase 5: Summary

Output a final block:

```
REVIEW COMPLETE
PR: <url>
Fixes: <n> items from <reviewer name>
Tests: <n>/<n> passing
Harness updates: <n> skill files modified (or "none — all findings already covered")
Commit: <hash>

Items for human review:
- <any "disagree" items or judgment calls>
```

## Anti-Patterns

- Fixing issues without running internal reviewers afterward — the fix itself can introduce new issues
- Editing harness files without user approval — the user owns the harness
- Adding overly specific checks that only apply to one codebase — keep it generic
- Skipping the retrospective because "it was just a typo" — even typo-class misses can reveal tooling gaps
- Treating the retrospective as blame — it's about improving the system, not explaining failure
