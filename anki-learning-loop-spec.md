# Spec: Anki-backed Learning Loop (capture → FSRS schedule → AI drill)

## Goal

Capture concepts I learn during agentic dev sessions into Anki, let Anki's FSRS own
all scheduling, and replace passive card review with an AI drill: regurgitation →
transfer problem → reasoning-graded answer that is written back to Anki.

Anki is the single source of truth and scheduler of record. We never touch its
SQLite directly — all reads/writes go through AnkiConnect (add-on, HTTP JSON API on
`localhost:8765`, `version: 6`). AnkiConnect only works while the Anki desktop app
is running, so every write path needs an offline fallback queue.

## Components

### 1. `.claude/hooks/anki.sh` — shared helper
- Thin curl wrapper for AnkiConnect actions: `version` (connectivity check),
  `createDeck`, `addNote`, `findNotes`, `findCards`, `cardsInfo`, `answerCards`.
- Deck: `Concepts`. Note model `Concept` with fields: `Concept`, `Summary`,
  `Context` (project, bead id, date), `Source`. Card template: front = Concept,
  back = Summary — so plain mobile Anki review still works as regurgitation.
- Availability: if `version` fails, try `open -ga Anki` (background launch, no
  focus steal), poll `version` for ~15s, then proceed. Recommend adding Anki to
  macOS Login Items so it survives reboots; lid-close is only sleep — Anki
  resumes on wake, so no restart is needed there.
- Offline fallback: if Anki is still unreachable after the launch attempt,
  append the capture as JSONL to `.learning/queue.jsonl`. Flush the queue at
  the start of any later call that finds Anki reachable.
- Dedupe: `findNotes "deck:Concepts Concept:<name>"` before add; on hit, update
  Context instead of creating a duplicate.

### 2. `/learned <concept>` — manual capture command
- Claude writes a one-sentence Summary from the current session context, fills
  Context (repo, bead, date), shows it for a one-keystroke confirm, then addNote.

### 3. Session-end capture — fold into `/handoff` (or a Stop-hook reminder)
- At session wrap, Claude scans the transcript and proposes ≤3 candidate concepts.
  Criteria: novel-to-me, non-trivial, decision-relevant (would change a design
  choice). I approve/reject each; approved → addNote.
- Per-session only, never per-message. SessionEnd hooks can't prompt the user, so
  implement as a step in `/handoff`, optionally with a Stop hook that reminds the
  agent to run it.

### 4. `/drill` — review command (the AI wrapper around FSRS)
- `/drill` is a review *client* of Anki, like the desktop review screen: Anki is
  the passive calendar; the drill session is a temporary reader of it. Nothing
  runs between sessions — all state lives in Anki.
- **Session loop** (this is the core mechanic, not a one-shot fetch):
  1. Fetch due cards: `findCards "deck:Concepts is:due"`, cap at ~5 per drill.
  2. Quiz each card through the three stages below, writing each grade back via
     `answerCards` immediately (not batched at the end — so a failed card
     re-enters Anki's learning queue right away).
  3. After the pass, **re-query** `findCards "deck:Concepts is:due"`. Failed
     cards that have re-entered learning and are due again now get a quick
     re-retrieval (regurgitation retry only — not a fresh transfer problem).
  4. Repeat step 3 until nothing is due, then end with a one-line session
     summary. If I quit mid-loop, nothing is lost: unanswered cards simply stay
     due in Anki and surface at the next `/drill` or on mobile.
- **Deck config**: set the Concepts deck's learning steps short-or-none (e.g. a
  single short step or none, fail → next appearance ~1 day). Minute-scale
  re-ask ladders (1 min / 10 min) are designed for 3-second flashcard flips;
  our transfer problems take minutes, so faithful simulation would make
  sessions unbounded for little retention gain.
- Per card, three stages:
  1. **Regurgitate**: "Explain <Concept> from memory." I answer in writing.
  2. **Transfer**: Claude generates a mini design/coding problem where the concept
     is load-bearing, with a novel surface context (not the context I learned it
     in). I answer in writing.
  3. **Grade the reasoning** (trade-offs weighed, not vocabulary), then map to an
     Anki ease and write back via `answerCards`:
     - failed recall → 1 (Again)
     - recalled, transfer weak/wrong → 2 (Hard)
     - clean transfer → 3 (Good)
     - effortless, extended the idea → 4 (Easy)
- Append each drill result (concept, stage outcomes, ease given) to
  `.learning/drills.jsonl` for later analysis.
- Quizzing mechanics: reuse the teach skill's rules (`.claude/skills/teach/SKILL.md`
  in conspectus) — use AskUserQuestion, mix open-ended and multiple-choice,
  randomize correct-option position, never leak the answer in the options, explain
  why wrong answers are wrong after I submit.

### 5. Teach → `/learned` bridge (capture from deep-dive sessions)
- The teach skill (`.claude/skills/teach/SKILL.md` + `/teach` command in
  conspectus) ends with a graduation summary: "Key things to remember: 3–5
  bullets." Those bullets are exactly the concepts worth spacing.
- Modify the teach skill's Graduation Criteria step: after printing the summary,
  offer each "key thing to remember" as a capture candidate — same one-keystroke
  approve/reject flow as `/handoff` capture — and write approved ones via
  `anki.sh addNote`, with Source = the PR/bead/topic that was taught.
- Rationale: teach produces deep encoding once; the deck turns that encoding into
  scheduled retrieval. Without the bridge, a teach session evaporates like any
  other session.
- Note: teach lives in the conspectus repo while this loop starts in
  agent-workflow. `anki.sh` should therefore be path-independent (no repo-relative
  assumptions) so both repos can source it; simplest is installing it to
  `~/.claude/hooks/anki.sh` with repo-local symlinks or copies.

### 6. Planner predict-then-diff (misses feed capture)
- Pre-plan ritual (mine, before `/plan` reveals anything): (1) my plan and win
  condition, (2) my beads and each bead's acceptance criteria, (3) which beads can
  run in parallel and where they attach in the codebase, (4) hidden test cases we
  might have missed.
- One-line change to the planner skill prompt: before presenting its plan, ask for
  my sketch, then present its plan as a diff against mine — "you missed X, I
  missed Y."
- Any miss on my side is a capture candidate → offer it to `/learned` on the spot.
  This makes capture a byproduct of planning instead of a separate discipline.

## Guardrails
- Never write to `collection.anki2` directly; AnkiConnect only.
- Never lose a capture: unreachable Anki → queue, never error out of the session.
- Grading maps to FSRS's assumption honestly: ease must reflect retrieval
  strength, and clean transfer counts as strong retrieval.

## Win condition (real integration tests, no mocks)
Against a live Anki instance with a throwaway profile/deck:
1. `/learned` → note exists in `Concepts` with all fields populated.
2. Duplicate `/learned` on same concept → no second note; Context updated.
3. Capture with Anki closed → row lands in `.learning/queue.jsonl`; next call with
   Anki open flushes it into the deck.
4. `/drill` on a due card → `answerCards` succeeds and the card's due date moves
   according to the given ease.
5. Teach graduation with 3 key takeaways → approved takeaways appear as notes in
   `Concepts` with Source set to the taught topic.
6. Drill loop: fail a card in stage 1 → after the pass, re-query shows it due
   again (if a learning step is configured) and it gets a re-retrieval within
   the same session; quitting mid-loop leaves it due for the next session.
