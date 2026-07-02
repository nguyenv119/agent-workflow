# Capture a Learned Concept

Capture **$ARGUMENTS** as a concept in the Anki-backed learning loop.

If `$ARGUMENTS` is empty, ask the user what concept to capture.

## 1. Draft the note

From the current session context, draft:
- **Summary** — one sentence: the concept itself, stated as a fact or rule.
- **Context** — `<repo> / <bead-id if any> / <today's date>`.
- **Source** — the file, PR, or topic that taught it.

## 2. Semantic dedupe (haiku subagent)

Spawn a subagent via the Agent tool with `model: "haiku"` to keep the deck contents out of the main context:

```
Run `bash .claude/hooks/anki.sh concepts` from the repo root. It prints a JSON
array of existing concept names (or exits 1 if Anki is unreachable — treat
that as "no candidates", not an error).

Compare "$ARGUMENTS" against the list using SEMANTIC similarity, not string
match (e.g. "KV store" should match "key-value store"). Return at most 3
candidate matches, each with the existing concept's exact name, or "none".
```

## 3. Confirm with the user

Use AskUserQuestion:

- **No candidates** → options: `Save as new`, `Edit summary`, `Skip`.
- **Candidates found** → options: `Merge into "<best candidate>"`, `Save as new`, `Skip`.

"Merge" means the capture call uses the EXISTING concept's exact name so anki.sh's own dedupe hits and only Context is updated — no new note is created.

## 4. Capture

On `Save as new` or `Merge`:

```bash
bash .claude/hooks/anki.sh capture "<concept>" "<summary>" "<context>" "<source>"
```

Use the existing candidate's exact name as `<concept>` for a merge; use `$ARGUMENTS` (or the edited name) for a new save. anki.sh handles launch-attempt, exact-name dedupe, and offline queueing itself — do not reimplement any of that here.

On `Skip`, do nothing further.

## 5. Report

One line:
- `Saved new concept: "<concept>"`, or
- `Merged into existing concept: "<concept>"`, or
- `Queued offline (Anki unreachable): .learning/queue.jsonl`, or
- `Skipped.`
