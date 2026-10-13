---
name: blast-radius
description: Before a risky change ships, name the one fact it is safe because of and prove that fact by running real code, not by writing a convincing paragraph. Use on risky beads (schema, shared infra, cross-service contracts, anything that can touch prod), on "what could this break", or on a small diff you don't trust.
---

# Blast radius

A risky change is usually safe because of one fact: "the new column is nullable, so old code never writes it", "this cache only drops entries nobody reads". Find that fact, then prove it. A list of callers is not the job; grep gives you that in a second. The job is the breakage grep can't see.

Adapted from the `blast-radius` skill in cursor/plugins (pstack, MIT License, Copyright (c) 2026 Lauren Tan). Rewritten for this harness.

## When to use

- Every **risky bead**, as tiered in `.claude/skills/win-condition/SKILL.md` § Bead-level acceptance: DB schema and migrations, shared infra, cross-service contracts, anything that can affect prod.
- Any review of a diff you don't trust yet, at any tier.

Standard beads (pure code, UI, refactors) don't need it.

## Proof levels

Say how far each load-bearing fact got. Write the level next to it.

1. **Asserted.** You said so. Worth nothing alone, because a wrong write-up reads as well as a right one.
2. **Cited code.** A real `file:line` in this repo or in the pinned library source.
3. **Test.** A unit or integration test that fails if the fact is false.
4. **Ran real code.** You ran the real thing against real data: a script calling the shipped function, a read-only query on a dev DB branch, the old app's query against the new schema. You pasted what it printed.

Risky beads need level 4. Stopped lower? Write `unproven (level N)` and say what level 4 would take. Do not round up.

## Steps

1. **Read the change.** The diff, what it adds, changes and deletes, and what now behaves differently that the diff doesn't spell out.
2. **Find the one fact.** Ask "this is safe because...". If the answer clears most of the scary cases at once, it is the fact. Spend your time here.
3. **Look past grep.** Follow what a symbol search misses: a DB column read by another service, a JSON shape a client parses, a wire format, a feature flag, an env var, a job three hops downstream, the deploy window when old code meets the new schema.
4. **Weigh each risk.** For each: how it breaks, `file:line`, how likely, how bad. Keep confirmed risks apart from ones you checked and cleared.
5. **Prove the fact.** Pick the cheapest read-only check that would fail loudly if the fact were false. Run it. Copy the command and its real output.

Keep the proof read-only unless the bead says otherwise: dev branches, never prod; no writes; no paid calls the bead didn't budget for.

## Output

One paragraph, pasted into the `BEAD COMPLETE` block (see `.claude/skills/coordinator/SKILL.md` § c2):

```
Safe because: <the one fact>.
Proved by: <exact command> -> <its real output, trimmed> (level 4)
```

If the fact stayed unproven, `Proved by:` says `unproven (level N): <what would prove it>`. Confirmed risks and cleared risks go below it as short bullets.

<!-- few-shot: blast-radius-good-vs-bad -->
```text
GOOD  Safe because: the new column is nullable with no default, so deploy-window old
      code never writes it.
      Proved by: old app's INSERT on a fresh dev branch -> 1 row, new column NULL (level 4)
BAD   Safe because: nothing else reads this table.
      Proved by: grep found no other callers
  why: a grep is level 2 at best, and it misses readers in other services or SQL views.
```

## Public repos

Real outputs from a private project go to the bead notes, not the PR body or commits. The PR body gets a masked line such as `blast-radius: level 4 proof, command ran`.
