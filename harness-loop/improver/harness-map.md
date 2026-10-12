# Harness map: which file steers which step of /work

The harness is everything under `.claude/` plus `AGENTS.md`. A target repo's own `CLAUDE.md`
supplies its Quality Gates table (the test, lint and typecheck commands); the loop never edits it.
Paths are relative to the repo root. `§` numbers are the coordinator's own section numbers.

| /work step | What happens | Steered by |
|---|---|---|
| Entry | `/work <id>` loads the coordinator | `.claude/commands/work.md`; `AGENTS.md` "Workflows" |
| Exam mode | Under the exam: no `/quick` offer, no approval waits, push without `gh` | `.claude/skills/coordinator/SKILL.md` "Exam mode" (read-only for the improver) |
| Triage | Parse input, file or read beads, print `Quick-eligible:` per bead | coordinator "Phase 1: Triage" §1-§3; `AGENTS.md` "Issue Tracking with bd" |
| Ready beads, conflicts | Pick open unblocked beads; keep overlapping files sequential | coordinator "Phase 2" §1-§2 |
| Worktree | One worktree and `feature/bd-<id>-<slug>` branch per bead, off `origin/main` | coordinator §3 "Create persistent worktrees" |
| Implementer spawn | Prompt with ROLE, SKILL, standards, task, worktree, PREVIOUS TRY | coordinator §3 "Spawn implementers"; `.claude/agents/implementer.md` (model, effort) |
| Implementer: plan | Read the bead, announce the approach | `.claude/skills/implementer/SKILL.md` "Before You Start", "Phase 0" |
| Implementer: tests first | Failing tests, then code, then verify | implementer "Phase 1: Write Failing Tests", "Phase 2", "Phase 3" (incl. "Real-acceptance check") |
| Implementer: coverage | List changes, audit refactors, fill test gaps | implementer "Phase 4: Test Coverage Review" |
| Implementer: hand back | Commit, structured summary (or error block) | implementer "Phase 5: Commit and Summary" |
| Standards | The rules reviewers check against | `.claude/skills/standards/quality.md` (§A-§I), `.claude/skills/standards/correctness-patterns.md` |
| Three reviewers | Correctness, Test Quality, Architecture in parallel, each with an injected checklist | coordinator §4a "Run Reviews in Parallel" (section-to-reviewer table); `.claude/skills/reviewer-correctness/`, `reviewer-tests/`, `reviewer-architecture/` `SKILL.md` |
| Review fixes | Trivial: fix directly; non-trivial: bead + implementer in the same worktree | coordinator §4c "Handle Review Results" |
| Test-runner gates | A haiku sub-agent runs CLAUDE.md's Quality Gates, scoped to changed packages, and reports counts and the commit | coordinator §4c; `.claude/skills/test-runner/SKILL.md` |
| Real acceptance | The bead's runnable check against reality, not mocks; under the exam, the repo's visible tests | coordinator §4c2; `.claude/skills/win-condition/SKILL.md` "Bead-level acceptance" |
| Save the try | Gates, diff, note and transcript saved to a try folder | coordinator §4c3; `.claude/skills/coordinator/save_try.py`, `cut_tries.py` |
| Push and PR | Push the branch; `gh pr create` (skipped under the exam) | coordinator §4d; `.claude/commands/pr.md` |
| Approval, status | Human gate (or win-condition eval in loop mode), bead status | coordinator §4e, §4f, "Autonomous Loop Mode" |
| Failures, watchdog | Retry rules, blocked beads, stalled sub-agents | coordinator §5, §6 |
| Quiet execution | One `BEAD COMPLETE` block per bead, no narration | coordinator "Stay quiet during execution" |
| Small diffs | `/quick`: one reviewer for low-risk diffs (never offered under the exam) | `.claude/skills/quick/SKILL.md`; `.claude/agents/quick-reviewer.md` |

## Hooks (`.claude/settings.json` wires them; all under `.claude/hooks/`)

| Hook | Fires | Enforces |
|---|---|---|
| `enforce-reviewer-standards.sh` | PreToolUse, Agent | Blocks a reviewer spawn whose prompt lacks its `SKILL.md` reference and the injected checklist content |
| `bd-dolt-pull.sh` | PreToolUse, Bash | Pulls the beads Dolt remote before a `bd` command (never blocks) |
| `bd-dolt-push.sh` | PostToolUse, Bash | Pushes the beads Dolt remote after a `bd` write (never blocks) |
| `save-compact-summary.sh` | PostCompact | Saves the compaction summary as a handoff file |
| `sync-compact-instructions.py` | run by hand | Copies the handoff skill into a CLAUDE.md "Compact Instructions" section |

`settings.json` also prints `AGENTS.md` at session start and sets `autoCompactWindow`.

## What the improver may not touch

The gate guard in `harness-loop/loop.py` rejects a change that (a) deletes, renames or changes
the mode of a reviewer, test-runner or standards file or a hook, or drops a hook entry from
`settings.json`; (b) removes or rewrites a line of those files, or a line that mentions a gate
or sits under a heading that does; (c) adds a line telling an agent to skip, shorten, sample or
bypass a gate, reviewer, test or acceptance step. Adding a line to those files is allowed.
See the improver skill, rule 8.
