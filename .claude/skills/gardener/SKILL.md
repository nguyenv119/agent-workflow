---
name: gardener
description: Weekly weeding pass for a repo and its harness. Runs /prune in report mode, then a correct pass that groups the week's repeated corrections into mistake classes and proposes, for each, the strongest check that would have caught it. Proposals only; writes nothing but its report, merges nothing, files nothing. Invoked via /gardener or a weekly routine.
---

# Gardener

If `.claude/project-rules.md` exists and has a section for this skill, follow it; it overrides the generic defaults here.

Lessons the harness learns tend to land as prose (memory notes, skill text), and an agent can skip prose. The gardener finds the lessons that keep coming back and proposes moving each one into something that fails loudly. It only proposes: a human or the harness loop decides.

Adapted from cursor/plugins pstack skills (MIT).

## Output and limits

- Everything goes to one report dir: `~/.claude/gardener/<YYYY-MM-DD>/` (`report.md`, plus `harness-loop.md` and `beads.md` when there is something to route). Name the dir at the top of the report.
- Writes nothing else. No edits to the repo, memory, skills or beads; no commits, no PRs, no merges. Reads only.
- Each proposal that needs a human becomes one desk card (or whatever single to-do surface the project uses), pointing at its report section.

## Pass 1 — prune report

Run the `prune` skill's Phase 2 (scan + verify) in report mode against `origin/main`: lists only, never Phase 3-4, never a delete. Diff against the last report's lists and put only the delta, with each `why:` line, under `## Prune` in `report.md`.

## Pass 2 — correct

### 1. Gather the week (last 7 days, by file date or record timestamp)

- Memory feedback notes: `~/.claude/projects/<project>/memory/feedback_*.md`. Each is one correction a human made.
- Try records: `~/.claude/tries/<repo>/index.tsv` (one row per `/work` try) and each try's `note.md`, `gates.txt`, `diff.patch`. Review findings and failed gates in the notes are corrections too.
- Optional: reverts and fix-up commits on the default branch in the same window.

### 2. Group into mistake classes

A class is one kind of mistake, named by what the agent did wrong (e.g. "re-implemented a helper that already exists"), not by the file it happened in. A class counts only when it was seen twice: two dated occurrences, each cited by file and date. One occurrence is a note, not a class; list it under `## Watching` and stop there.

### 3. Pick the highest rung that works

For each class, propose a fix at the first rung that can catch it:

1. **architecture**: make the mistake impossible (one owner per piece of state, one supported way, internals hidden so the wrong import fails, one source of truth instead of hand-synced lists, delete the old way an agent would copy).
2. **types/lint/hook/script**: the bad state does not compile, or a check fails with an error that names the right file or function to use. If the pattern is already common, fail only on new additions.
3. **test**: a test that fails on the mistake (and would not pass if the code under test returned nothing).
4. **doc text** last: only for real judgment calls. Nothing fails when an agent skips it.

Say why each higher rung does not work. A doc-text proposal for a class that already has a doc rule is a repeat: push it up a rung.

### 4. Prove it

Every proposal carries proof that the check would have failed on a real past mistake: run it (in a scratch dir, not the repo) against an occurrence's `diff.patch` or commit, at that try's start commit, and paste the output. Report the noise too (how many hits were not the mistake). No proof, no proposal: it goes under `## Watching`.

### 5. Route

- **About how agents work in general** (true in any repo: reporting, planning, reviewing, reuse habits): write it to `harness-loop.md` as an idea for the harness improver loop, in its proposal shape (Idea / Files / Evidence / Expected effect / Risk). The loop exam-tests it. Never edit a skill in place.
- **About one project's code**: write it to `beads.md` as a proposed bead for that project (title, why, rung, proof, files). Do not file it.

### 6. Rule-to-enforcer table and retirements

Keep a table in `report.md`: rule, where it is written, what enforces it (check name, or "nothing"). Carry it forward from last week's report. For each memory note whose lesson a check now enforces, propose retiring the note (quote the check that replaces it). Never delete the note yourself.

## report.md shape

```
# Gardener <date> — <repo>
Report dir: ~/.claude/gardener/<date>/   Window: <from> .. <to>
## Prune            (delta only)
## Mistake classes  (per class: name, evidence x2 with dates, rung, why not higher, proof, route)
## Watching         (single sightings, unproven ideas)
## Rule -> enforcer
## Retire           (notes a check now covers)
```

## Hard rules

- Proposals only. The report dir is the only place this skill writes.
- A class needs two dated occurrences. A proposal needs proof it would have fired.
- Harness classes go to the improver loop for exam-testing, never straight into a skill.
- Never weaken or remove a check to make a class go away.
