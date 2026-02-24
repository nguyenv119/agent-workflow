---
name: merge-queue
description: Process open PRs — merge when CI passes, handle rebases, file issues for failures. Run in a dedicated window.
---

# Merge Queue

Process all open PRs. Merge what's ready, rebase what's behind, file issues for failures.

Run this in a dedicated terminal window. Invoke periodically with `/merge` while other windows do `/work`.

## Step 1: Scan

```bash
# Check CI status on main — failing main blocks the entire queue
gh run list --branch main --limit 1 --json status,conclusion

# List open PRs
gh pr list --json number,title,headRefName,statusCheckRollup,mergeable,body,reviewRequests,reviews
```

**If main CI is failing:** file a P0 beads issue (if one doesn't already exist), report prominently, and stop. Nothing can merge until main is green.

```bash
bd create "CI failing on main: <summary>" -t bug -p 0 --json
```

## Step 2: Categorize

For each PR, determine its state:

| State | Action |
|-------|--------|
| CI passing, mergeable, no pending review | Merge |
| CI passing, mergeable, review requested but not approved | Skip, report |
| CI passing, mergeable, review approved | Merge |
| CI pending | Skip, report status |
| CI failing | File issue, report |
| Not mergeable (behind main) | Attempt rebase |

## Step 3: Decide Merge Order

When multiple PRs are ready, use judgment to balance:

- **Impact**: prefer merging high-impact features first
- **Merge conflict risk**: larger changes sitting unmerged cause more conflicts for other PRs
- **Goal**: keep changes flowing. Don't always defer large changes — that makes the conflict problem worse

## Step 4: Choose Merge Strategy

Before merging each PR, inspect its commits to decide how to merge:

```bash
gh pr view <number> --json commits --jq '.commits[] | "\(.oid[:8]) \(.messageHeadline)"'
```

Pick one of three strategies:

| Strategy | When to use | Command |
|----------|-------------|---------|
| **Squash** | All commits serve a single logical change, OR commits are messy/WIP (fixups, "wip", "try again", etc.) | `gh pr merge <number> --squash` |
| **Merge** | Commits represent distinct, well-structured concerns (e.g. "add handler" + "add tests" + "update docs") that are valuable to preserve individually | `gh pr merge <number> --merge` |
| **Rebase cleanup** | Commits mix good structure with noise — some are worth preserving but others should be squashed together. **Use sparingly** — only when clearly beneficial. | Rebase in worktree, force-push, wait for CI, then `gh pr merge <number> --merge` |

**Decision guide:**

1. **Single commit?** Always squash (no difference, but squash keeps PR link in message).
2. **Multiple commits, single feature?** Squash. Example: "implement login" + "fix lint" + "address review" → squash.
3. **Multiple commits, distinct concerns, clean messages?** Merge. Example: "add user profile handler" + "add rate limiting middleware" + "enable profile UI in settings" → merge.
4. **Mixed quality?** If 1-2 WIP commits pollute an otherwise clean history, rebase to clean up, then merge. If it's mostly noise, just squash.

**When in doubt, squash.** A clean single commit is always better than a messy multi-commit history.

## Step 5: Merge

For each mergeable PR (in priority order), using the chosen strategy:

```bash
gh pr merge <number> --squash|--merge  # per Step 4
```

**After each merge:**

1. Parse beads issue IDs from PR body (look for `Beads: id1, id2` line)
2. Close each:
   ```bash
   bd close <id> --reason "Merged in PR #<number>" --json
   ```
3. Remove worktree if it exists:
   ```bash
   git worktree remove ../<project>-<branch-name> 2>/dev/null
   ```
4. Delete feature branch:
   ```bash
   git branch -d feature/<branch-name> 2>/dev/null
   ```
5. Pull main:
   ```bash
   git pull origin main
   ```

**Important:** after each merge, re-check remaining PRs — merging one PR may make others unmergeable (need rebase) or may resolve conflicts.

## Step 6: Handle Rebases

When a PR is not mergeable (behind main):

```bash
git fetch origin main

# Use existing worktree if present, otherwise create one
cd <existing-worktree>  # or: git worktree add ../<project>-rebase-<number> <branch>
git rebase origin/main
```

- **Clean rebase:** force-push, then **wait for CI to finish and merge** (see below).
  ```bash
  git push --force-with-lease
  ```
- **Trivial conflict:** resolve inline if the conflict is mechanical — e.g. adjacent line edits, import ordering, lock file regeneration, or both sides adding to the same list. After resolving, `git add` the files, `git rebase --continue`, and force-push. **Always include a conflict summary in the report:**
  ```
  Resolved 2 conflicts during rebase of PR #18:
  - src/routes/api.ts: adjacent route additions (kept both)
  - package-lock.json: regenerated
  ```
- **Non-trivial conflict:** file a beads issue describing the conflict, which files are affected, and what makes it non-trivial (semantic overlap, structural disagreement, etc.). Do not attempt to resolve. Report to user.

After rebase, clean up any temporary worktree created for the rebase.

**After a clean rebase, poll CI and merge when it passes.** Don't just report "rebased, CI re-running" and stop — unmerged PRs accumulate conflicts. Poll every 60 seconds until CI completes:

```bash
# Poll until all checks finish
gh pr checks <number> --watch
# Then merge (using strategy from Step 4)
gh pr merge <number> --squash|--merge
```

If CI fails after the rebase, follow Step 7 (file an issue). But if it passes, merge immediately — don't wait for the user to re-run `/merge`.

## Step 7: Handle CI Failures

**Test failures are real. Never rerun. Never ignore.**

When CI fails on a PR:

1. Fetch failure logs:
   ```bash
   gh pr checks <number>
   gh run view <run-id> --log-failed
   ```
2. File a beads issue with:
   - The failure details (which test, error message)
   - PR reference
   - Priority based on severity
   ```bash
   bd create "CI failure on PR #<number>: <summary>" -t bug -p 1 --json
   ```
3. Report to user with the beads issue ID

## Step 8: Report Summary

After processing all PRs, output a summary:

```
Merge Queue Summary:
- PR #12: Merged (closed bd-abc, bd-def)
- PR #15: CI passing, awaiting your review
- PR #18: Rebased, CI re-running
- PR #20: CI failing — filed bd-xyz
- PR #22: CI pending (2/3 checks done)

Action needed:
- bd-xyz: Test failure on PR #20, needs /work bd-xyz
- PR #15: Awaiting your review on GitHub
```

Always include beads issue IDs so the user can dispatch `/work` for fixes.

If there are no open PRs, report "No open PRs."

## What This Agent Does NOT Do

- Write code or fix test failures (file issues for `/work` instead)
- Resolve merge conflicts (file issues instead)
- Rerun failed CI (test failures are real)
- Close beads issues without a successful merge
- Merge PRs with pending review requests
