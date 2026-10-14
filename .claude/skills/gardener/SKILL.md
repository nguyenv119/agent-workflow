---
name: gardener
description: Weekly weeding pass for a repo and its harness. Runs /prune in report mode, then a correct pass that groups the window's repeated corrections into mistake classes and proposes, for each, the strongest check that would have caught it, then counts scripts that still hand-roll a chore a CLI covers (agent-cli hook scan). Proposals only; writes only its report dir, a scratch dir and prune's own files, merges nothing, files nothing unless the caller names a bead db. Takes an optional past window. Invoked via /gardener or a weekly routine.
---

# Gardener

If `.claude/project-rules.md` exists and has a section for this skill, follow it; it overrides the generic defaults here.

Lessons the harness learns tend to land as prose (memory notes, skill text), and an agent can skip prose. The gardener finds the lessons that keep coming back and proposes moving each one into something that fails loudly. It only proposes: a human or the harness loop decides.

Adapted from cursor/plugins pstack skills (MIT).

## Output and limits

- Caller parameters (each optional, given in the prompt):
  - `window: YYYY-MM-DD..YYYY-MM-DD`: the days to read, inclusive, local dates. Default: the 7 days ending today (today minus 6 days through today).
  - `report-root: <dir>`: where report dirs go. Default `~/.claude/gardener/<repo>/`. A replay of a past window passes its own, so it never mixes with the weekly reports Pass 3 compares against.
  - `transcripts: <dir>`: Claude Code session logs to read as corrections too (see Pass 2 step 1).
- Report dir: `<report-root>/<YYYY-MM-DD>/`, named after the window end date (`report.md` and `scan.json`, plus `harness-loop.md`, `beads.md` and proof scripts when there is something to route). Name the dir at the top of the report.
- Allowed writes: the report dir, a temp scratch dir for proof runs, and prune's own out-of-tree files. No edits to the repo, memory, skills or beads (beyond the filing exception below); no commits, no PRs, no merges.
- Proposals that need a human are listed in the report for the caller to file on the project's to-do surface. The gardener files nothing itself. One exception: when the caller's prompt explicitly names a bead db to file `beads.md` proposals into, file exactly those and list their ids in `filed.txt` in the report dir; nothing else is ever filed.

## Pass 1: prune report

Run prune's Phase 5 rescan as written (report mode, never Phase 3-4, never a delete) and paste its delta, with each `why:` line, under `## Prune` in `report.md`. If prune can't run (repo never pruned, pinned tooling missing), write `## Prune: skipped (<reason>)` and go on to Pass 2.

## Pass 2: correct

### 1. Gather the window

The window is the caller's `window:`, else the 7 days ending now. Date every input by its record timestamp (a dated line, a JSONL row's timestamp, a commit date) or its file birth date (`stat -f %SB`, Python `st_birthtime`), never mtime alone: an edit moves mtime but not when the correction happened. Anything dated after the window end is ignored, even when the file is on disk.

- Memory feedback notes: `~/.claude/projects/<project>/memory/feedback_*.md`. Each is one correction a human made; a dated update inside a note is a sighting on its own date.
- The try records the coordinator saves (save_try.py; `$HARNESS_TRIES_DIR`, default `~/.claude/tries`). Review findings and failed gates in them are corrections too. Text injected into every session (a memory index, CLAUDE.md) is not a sighting.
- When the caller passes `transcripts: <dir>`: the Claude Code JSONL files under it, user turns only (the human's own words, not tool results or pasted skill text), each dated by its message timestamp inside the window.
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

Proof contract, so a caller can rerun it: each proof is an executable file in the report dir, called as `<proof> <input path>`, that exits non-zero when the input shows the mistake and 0 when it is clean. Per class, the report lists the proof's path and the input it fired on.

### 5. Route

- **About how agents work in general** (true in any repo: reporting, planning, reviewing, reuse habits): write it to `harness-loop.md` as an improver-shaped proposal (Idea / Files / Evidence / Expected effect / Risk) for a human to hand to the harness loop, which exam-tests it. Never edit harness files.
- **About one project's code**: write it to `beads.md` as a proposed bead for that project (title, why, rung, proof, files). Do not file it, unless the caller names a bead db (see Output and limits).

### 6. Rule-to-enforcer table and retirements

Keep a table in `report.md`: rule, where it is written, what enforces it (check name, or "nothing"). Start from the `## Rule -> enforcer` table in the repo's `AGENTS.md` (read it first; propose new rows for it in the report, never edit `AGENTS.md` yourself), then carry it forward from the previous report under the same report root. For each memory note whose lesson a check now enforces, propose retiring the note (quote the check that replaces it). Never delete the note yourself.

## Pass 3: chores still hand-rolled

The agent CLIs only pay off if new scripts use them. This pass counts the files that still hand-roll a chore a command covers.

1. Run `agent-cli hook scan --from <start> --until <end> --json` from the repo root, with the window's first and last dates. With no paths it scans the dirs on the `scan-dirs:` line under `## gardener` in `.claude/project-rules.md` (comma-separated, relative to the repo root). If it exits 2 saying there is no `scan-dirs:` line, write `## Hand-rolled chores: skipped (no scan-dirs)` and stop this pass.
2. Save its output as `scan.json` in the report dir, so the next report can diff against it.
3. Under `## Hand-rolled chores`, write one row per rule from the summary's `hits_by_rule`: rule id | new files in the window | files in the previous report (`hits_by_rule` on the last line of the previous report's `scan.json` under the same report root, or `-`) | 2 example paths. Put the summary's `bypassed` count under the table: a file that carries `# agent-cli: allow <rule>` still hand-rolls the chore.
4. Compare the last two reports' `scan.json` with this one's: a rule whose count rose two reports running is a mistake class candidate for Pass 2: cite two of its files, each with its date, as the two occurrences.

## report.md shape

```
# Gardener <date>: <repo>
Report dir: <report-root>/<window end>/   Window: <from> .. <to>
## Prune            (delta only)
## Mistake classes  (per class: name, evidence x2 with dates, rung, why not higher, proof path + input it fired on, route)
## Hand-rolled chores  (rule | new in window | previous report | examples; bypassed count)
## Watching         (single sightings, unproven ideas)
## Rule -> enforcer
## Retire           (notes a check now covers)
```

## Hard rules

- Proposals only (filing only what the caller's prompt names). Writes only the report dir, a temp scratch dir and prune's own out-of-tree files.
- A class needs two dated occurrences. A proposal needs proof it would have fired.
- Harness classes go to a human as improver-shaped proposals, never straight into a skill.
- Never weaken or remove a check to make a class go away.
