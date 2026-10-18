# Open or Update Pull Request

If `.claude/project-rules.md` exists and has a section for this skill, follow it; it overrides the generic defaults here.

Create or update the PR for **$ARGUMENTS** (or the current branch if no argument given) with an AI-generated summary.

The coordinator auto-creates a PR when it pushes a branch. Use this command to regenerate or update the PR summary after additional commits, or to manually create a PR for a branch the coordinator didn't push. This command is idempotent — running it multiple times on the same branch always regenerates from the latest diff.

## 1. Determine the branch

```bash
BRANCH="${ARGUMENTS:-$(git branch --show-current)}"
```

If `$ARGUMENTS` is empty, use the current branch.

## 2. Read the diff

```bash
git fetch origin main
git log origin/main..$BRANCH --oneline
git diff origin/main...$BRANCH
```

## 3. Pull beads context

Extract bead IDs from commit messages on the branch:

```bash
git log origin/main..$BRANCH --format="%B" | grep "^Bead:" | sort -u
```

For each bead ID found, fetch its description:

```bash
bd show <bead-id> --json
```

## 4. Check for GitHub issue linkage

If any commit message contains `gh-issue:` or `Closes #N`, note the issue number — include `Closes #<N>` in the PR body.

## 5. Derive the repo slug

```bash
REPO=$(git remote get-url origin | sed 's|.*github\.com[:/]||' | sed 's|\.git$||')
```

## 6. Check if a PR already exists

```bash
gh pr list --repo $REPO --head $BRANCH --json number,url --jq '.[0]'
```

If a PR already exists, note its number — you will **update** it instead of creating a new one.

## 7. Generate the PR body and create or update

Read the diff and beads context, then generate a rich, accurate PR body.

**If no PR exists — create:**
```bash
gh pr create --repo $REPO --title "<type>: <concise title>" --body "<generated body>"
```

**If PR already exists — update:**
```bash
gh pr edit <number> --repo $REPO --title "<type>: <concise title>" --body "<generated body>"
```

**PR body template:**
```
## Summary
<2-4 bullets — what this PR does and why, written from the diff>

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

## Evidence
<paste output of a real run proving the change works — command output, eval
result, or screenshot. Unit-test pass alone does not qualify. For diffs that
change no runtime behavior, write "N/A (docs-only)".>

Beads: <comma-separated bead IDs, or omit if none>

Closes #<github-issue-number>  ← include only if applicable

Generated with Claude Code
```

**Title format:** Use conventional commits — `feat:`, `fix:`, `refactor:`, `chore:`, etc.

**Summary quality bar:** The summary should be written from reading the actual diff — not generic. If the diff adds a caching layer, say what it caches, why, and what the TTL is. If it fixes a bug, name the bug.

**Evidence:** name a real run and what it observed, not "tests pass".
<!-- few-shot: pr-evidence-good-vs-bad -->
```text
BAD   ## Evidence
      All unit tests pass. Eval: 14 passed.
  why: unit-only; and one of the 14 asserted "Legacy total" is absent, a
       string nothing writes any more, so it could never fail.
GOOD  ## Evidence
      live-check vs dev DB branch (app-42): 19 of 29 sampled accounts resolve
      to /\$\d+\.\d{2}/; invoice-7 summary: no_data lines = 0
  why: a real run naming observed values, matched on what the code emits.
```

**Idempotent:** Running `/pr` multiple times on the same branch always regenerates the summary from the latest diff. Safe to re-run after the agent pushes fixes.

## 8. Start the harness canary (laptop only)

A PR that changes harness paths cannot land until commit status `harness-canary` is posted on its head, and only a laptop can run the exam. Only this step is laptop-gated; the rest of /pr is unchanged.

**On a laptop (`CLAUDE_CODE_REMOTE` unset):** run `agent-cli pr status <number> --json --repo $REPO`. When `canary.required` is true and `canary.state` is null, start `agent-cli harness canary --pr <number> --repo $REPO` with Bash `run_in_background` (about 30 min; it posts `pending` "running" at once, then the verdict) and say that it started. A non-null `canary.state` means it already ran, or is running (`pending`), for this head: start nothing.

**In a cloud session (`CLAUDE_CODE_REMOTE` set):** do not call `agent-cli pr status` (its `gh pr` GraphQL call returns 403 there) and do not start the canary (it refuses there). When `git diff --name-only origin/main...$BRANCH` touches a harness path (`harness-paths:` under `## harness` in `.claude/project-rules.md`, else `.claude/` skills, commands, agents, hooks, bin, cli, settings.json, CLAUDE.md, project-rules.md, harness-addon, plus AGENTS.md and CLAUDE.md), print:

```
harness PR <number> waits for a laptop canary run (the project's daily job or a laptop /pr)
```

## 9. Verify the Evidence gate (mechanical)

After creating or updating, confirm the PR body carries a real-run Evidence section:

```bash
gh pr view $BRANCH --repo $REPO --json body --jq .body | grep -q '^## Evidence' \
  && echo "evidence-gate: OK" \
  || echo "BLOCKED: PR body has no '## Evidence' section — add real-run output (or 'N/A (docs-only)') and re-run /pr"
```

Run it unfiltered: if a command-rewriting hook or output filter wraps `gh`, bypass it here, since a compressed body can elide the marker line. Do not hand the
PR to review while this prints BLOCKED.
