# Work Coordinator

Coordinate work on **$ARGUMENTS** using the coordinator workflow.

1. Fetch work details: `bd show $ARGUMENTS --json`
2. If this is an epic, also fetch subtasks: `bd list --parent $ARGUMENTS --json`
3. Follow the coordinator skill instructions below

---

@.claude/skills/coordinator/SKILL.md
