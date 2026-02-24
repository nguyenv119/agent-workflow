# GitHub Issue Workflow

Work on GitHub issue **$ARGUMENTS** end-to-end: fetch it, create a beads issue, and implement it.

## 1. Fetch the GitHub Issue

```bash
gh issue view $ARGUMENTS --json title,body,labels,number
```

## 2. Create a Beads Issue

Create a beads issue from the GitHub issue content. Map GitHub labels to beads issue types (`bug`, `feature`, `task`). Include the GitHub issue number in the description for traceability.

```bash
bd create "<title>" -t <type> -p <priority> --json
```

Use priority 1 for bugs, 2 for features/tasks unless the issue indicates urgency.

## 3. Implement

Follow the coordinator workflow below. The coordinator will triage the work and create a branch/PR.

## 4. PR Must Reference the GitHub Issue

When creating a PR, the body **must** include:

```
Closes #<github-issue-number>
```

This auto-closes the GitHub issue when the PR merges.

---

@.claude/skills/coordinator/SKILL.md
