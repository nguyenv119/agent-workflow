# Open Pull Request

Open a PR for branch **$ARGUMENTS** after human review is complete.

1. Get the current branch if no argument given: `git branch --show-current`
2. Verify quality gates pass in the worktree
3. Create the PR:

```bash
gh pr create --title "<type>: <title>" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points>

## Changes
<list of significant changes>

## Test plan
- [ ] Tests pass
- [ ] <manual verification steps if any>

Beads: <comma-separated list of all beads issue IDs included in this PR>

Generated with Claude Code
EOF
)"
```

4. Label beads issues as `in-pr`:
   ```bash
   bd update <id> --set-labels in-pr --json
   ```
