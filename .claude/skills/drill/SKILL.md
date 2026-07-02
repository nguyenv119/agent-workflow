---
name: drill
description: AI review client over Anki FSRS — a 3-stage quiz loop (regurgitate, transfer, reasoning-graded ease) that reads due cards from Anki and writes grades straight back. Used by /drill.
---

# Drill

`/drill` is a review **client** of Anki, like the desktop review screen: Anki
is the passive calendar and the single source of truth for scheduling. This
skill is a temporary reader of it — nothing runs between sessions, all state
lives in Anki. It replaces card-flipping with regurgitation → a transfer
problem → reasoning-graded ease written back immediately.

## One-time setup note (deck config)

Before the first real drill session, set the **Concepts** deck's learning
steps to short-or-none (Anki Deck Options → Learning steps): either a single
short step or none at all, so a failed card's next appearance is ~1 day out.

Why: Anki's default learning-step ladder (1m / 10m) is designed for
3-second flashcard flips that you can re-ask minutes later. Our transfer
problems take minutes to work through, so faithfully simulating the default
ladder would make a session with any failure unbounded for little retention
gain. This is a one-time manual step in Anki's UI — the skill does not
configure it.

## Session Loop

### 1. Fetch due cards

```bash
bash .claude/hooks/anki.sh due
```

Returns a JSON array of card ids (`deck:Concepts (is:due OR is:new)` — plain
`is:due` would exclude never-reviewed cards, hiding fresh captures). Cap the
session at the first **5** card ids; leave the rest for a later session.

If the array is empty, report "Nothing due" and stop — do not run the loop.

For each card id, fetch its fields:

```bash
bash .claude/hooks/anki.sh info <cardId> [<cardId> ...]
```

This returns `cardsInfo` — read `fields.Concept.value`, `fields.Summary.value`,
and `fields.Context.value` per card (the note fields created by `anki.sh
capture`) to drive the quiz.

### 2. Per card: three stages

Work through the capped set of due cards one at a time, in the order
returned. For each card:

**Stage 1 — Regurgitate.** Ask: "Explain `<Concept>` from memory." Let the
human answer in writing. This is retrieval, not recognition — do not show the
Summary/Context yet.

**Stage 2 — Transfer.** Generate a small design/coding problem where the
concept is load-bearing, set in a **novel surface context** — not the
Context field the concept was originally learned in (e.g. if the concept was
learned via a Postgres migration, pose the transfer problem in an unrelated
domain like a cache-invalidation or queueing scenario). The human answers in
writing.

**Stage 3 — Grade the reasoning.** Grade the trade-offs the human actually
weighed, not vocabulary recall. Map the outcome to an Anki ease:

| Outcome | Ease |
|---|---|
| Failed recall (stage 1 wrong or blank) | 1 (Again) |
| Recalled, but transfer weak or wrong | 2 (Hard) |
| Clean transfer | 3 (Good) |
| Effortless, extended the idea further | 4 (Easy) |

Clean transfer counts as strong retrieval — do not under-grade a correct
transfer just because the explanation was terse.

### 3. Write back immediately — never batch

As soon as a card is graded, write it back before moving to the next card:

```bash
bash .claude/hooks/anki.sh answer <cardId> <ease>
```

Do this per card, not batched at the end of the session. A failed card must
re-enter Anki's learning queue right away so the re-query in step 4 can pick
it up.

**Check the exit code.** A non-zero exit from `answer` means the grade was
**not** recorded (AnkiConnect reports a bad/unanswerable card id as
`result:[false]`, which `anki.sh` treats as failure). Do not continue
silently:
- Tell the human the grade for this card did not save.
- Retry once (`answer` is safe to retry — it is not a mutating accumulation).
- If it fails again, stop the loop and surface the failure instead of
  proceeding to the next card as if nothing happened.

### 4. Re-query and retry failed cards

After finishing the capped pass, re-run:

```bash
bash .claude/hooks/anki.sh due
```

Any card that was graded 1 (Again) and is now due again gets a **quick
regurgitation-only retry** — Stage 1 only, no fresh transfer problem. Grade
and write back the same way (step 2's ease map, step 3's write-back rule).

Repeat this re-query → retry cycle until `due` returns nothing due from this
session's cards, then stop.

Quitting mid-loop loses nothing: any card not yet answered simply stays due
in Anki and will surface again at the next `/drill`.

### 5. Session summary

End with a one-line summary, e.g.:

```
Drilled 4 cards (3 clean, 1 retried after Again) — 0 due remaining.
```

### 6. Log

Append one JSONL row per card graded (including regurgitation-only retries)
to `.learning/drills.jsonl`:

```json
{"concept": "<Concept>", "stages": {"regurgitate": "pass|fail", "transfer": "pass|weak|fail|n/a"}, "ease": 1, "ts": "2026-07-02T12:00:00Z"}
```

`.learning/` is already gitignored — this log is local-only, never committed.

## Quiz Mechanics

These rules govern how questions are asked and answers are handled,
throughout both the Regurgitate and Transfer stages:

- Use `AskUserQuestion` for quizzes.
- Mix open-ended and multiple-choice questions.
- For multiple-choice: randomize the position of the correct answer across
  questions (never always the same option letter/position).
- **Never reveal the answer in the question options** — wait until after the
  human submits.
- After they answer, explain WHY the correct answer is right and why the
  others are wrong.
- If they get it wrong, that's the signal for the ease map above — don't
  re-teach and re-quiz within the same stage; let the grade reflect what
  happened and move on (re-teaching happens outside `/drill`, e.g. via the
  `teach` skill).
