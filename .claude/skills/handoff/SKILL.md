---
name: session-handoff
description: Use when the user says "session handoff", "wrap up session", "hand off", "handoff summary", or wants a structured end-of-session summary before clearing context. Produces a chat-only handoff — a briefing + operator manual for a fresh agent — covering what was built, how it fits together, work→PR traceability, non-obvious traps and why, how to run it, running state, key files, what's held for the human, verification, deferrals, and open questions.
---

# Session Handoff

Produce a repeatable end-of-session artifact so the user can `/clear` and start a fresh agent without losing continuity. The next agent should be able to pick up by reading this alone.

This is a **context-handoff artifact** for a future instance of you — and, when the session built or changed something real, a **briefing + operator manual** for that thing. Not a status report for a stakeholder, not a retro.

**Scale the depth to the session.** A one-file bugfix gets a short handoff (a few sections). A multi-component build or a shipped epic gets the full briefing (architecture, work→artifact table, traps, run recipe). The sections marked *(when applicable)* below are included only when the session warrants them — never pad a small session into a big template, never compress a big session into a thin one.

## When to invoke

User says: "session handoff", "wrap up session", "hand off", "handoff summary", "let's wrap up", "summarize before I clear", or any near-equivalent. Also invoke proactively if the user says they're about to `/clear` without having run it yet.

## How to produce it

1. **Review the full conversation**, not just the last few turns. Handoffs miss things when they only summarize recent context.
2. **Pull state from these sources (in order):**
   - Plan files referenced this session (check `/Users/nguyenv/.claude/plans/` if a plan was mentioned).
   - TodoWrite / task state — any in-progress or pending items.
   - Background processes you started with `run_in_background` — shell/agent IDs are load-bearing for the next agent.
   - Files created or modified this session — you know what you touched; don't grep to re-discover.
   - Tracked work you shipped — beads/issues closed, PRs opened/merged, commits (you know these from the session; don't audit).
   - Memory files written or updated (`/Users/nguyenv/.claude/projects/<project>/memory/`).
   - Unresolved questions — things you asked the user that never got a clear answer, or things the user asked that got deflected.
   - **Decisions you deliberately did NOT act on** — anything spend-affecting, production-facing, or destructive that you held for the human. These are the easiest thing to lose and the most dangerous.
3. **Do NOT audit the filesystem to reconstruct.** This is synthesis of what happened in THIS session. No broad `Glob`/`git log` sweeps to rediscover. (Pulling a PR number or commit hash you already produced this session is fine.)
4. **Produce the output in chat.** Do not write a file. Do not update memory. Chat-only.

## Output template — use these sections, in this order; skip the *(when applicable)* ones that don't fit

```
# Session Handoff — <one-line title>

## What this is        (when applicable: the session built or changed a system/feature)
<the thing in one short plain-English paragraph: what it does, for whom, and the shape of it.
Write for someone who forgot everything — motivate WHY before HOW.>

## Where it started
<2-3 sentences: what the user asked for, key framing or constraints that emerged.>

## Architecture / how it fits together        (when applicable: multi-component work)
<a short ascii flow OR a component list. For each component, name the artifact that built it
(bead/issue id, PR, or file path). Keep boxes/labels sparse; the prose carries detail.>

## Work shipped        (when applicable: tracked items — beads/issues/PRs)
<a table when there are IDs to map, else bullets:>
| id | what | artifact (PR # / commit) |
|----|------|--------------------------|
Include the final integration commit / branch state.

## Non-obvious traps + why        (when applicable)
<the things a fresh agent would trip on, WITH the reasoning — the "why it's built this way",
the subtle bug that hid, the credential/config gotcha. This is forward-looking knowledge for the
next agent, NOT a what-went-well retro. Explaining the WHY here is encouraged, not terseness.>

## How to run / operate        (when applicable: there's an operable artifact)
<a paste-ready recipe: env exports, the command/invocation, the acceptance or eval to run.
Include the load-bearing gotchas inline (PATH, sandbox flags, which key/provider).>

## Running state
- Background processes: <shell/agent IDs + what they are + how to stop> — or "none"
- Dev servers / ports: <url + port> — or "none"
- Open worktrees / branches: <paths> — or "none"

## Key files for next session
- `<absolute path>` — <why the next agent should read this first>
- Plan file: `<path>` (if a plan drove the session — name it FIRST)
- Memory files touched: `<paths>` (if any)

## Held for you (gated / irreversible)        (when applicable)
<decisions deliberately NOT taken, awaiting the human: spend-affecting switches, production
cutovers, deletes, secret rotation, external sends. State exactly what's needed to proceed and
why you held. A recommendation is allowed here.>

## Verification — how to confirm things still work
- `<command>` — <expected outcome>

## Deferred + open questions
- Deferred: <item> — <why pushed to later> (link a filed bead/issue if one exists)
- Open: <question needing the user's input> — <context>

## Pick up here
<the single most likely next action for a fresh agent. A one-line recommendation is allowed.>
```

## Concept capture (after the summary, before final output)

Scan the **whole session** (not just recent turns) for concepts worth capturing into the Anki-backed learning loop. Propose **at most 3** candidates, each meeting ALL of:

- **Novel to the user** — not something they clearly already knew going in.
- **Non-trivial** — not a one-line syntax fact.
- **Decision-relevant** — knowing it would change a design choice.

**Zero qualifying candidates → skip this step silently.** Do not mention it, do not ask the user "nothing to capture, right?" — just omit it.

For each candidate, follow **§§1–4 of `.claude/commands/learned.md`** (draft the note's Summary/Context/Source per §1's field conventions → haiku dedupe subagent → AskUserQuestion confirm → `anki.sh` capture) with `<concept>` bound to that candidate — §1's ask-if-empty clause doesn't apply, since the candidate is already bound. Do not restate those steps here — always defer to learned.md so the capture UX has one source of truth. Present multiple candidates as separate approve/reject decisions (a single multi-select AskUserQuestion across candidates is fine).

This step runs **after** the handoff summary has been produced and shown, never before — the summary is the priority; capture is a coda.

## Hard rules

1. **Chat output only for the handoff summary.** Never write the summary to a file. Never update memory from this skill. *Carve-out:* the concept-capture step may invoke `anki.sh`, which writes to `.learning/queue.jsonl` when Anki is unreachable (see learned.md §4) — that is the capture flow's own persistence, not the handoff summary, and does not violate this rule.
2. **Never invent state.** If a *core* section (Where it started, Running state, Key files, Verification, Deferred + open, Pick up here) has nothing to report, write "none" — don't omit it. The *(when applicable)* sections are the opposite: omit them entirely when they don't fit, rather than writing an empty heading.
3. **Absolute paths always.** The next agent may have a different working directory.
4. **If a plan file drove the session, name it first** in "Key files".
5. **Match register to section.** State sections (Running state, Key files, Verification) stay terse and concrete — paths, commands, IDs, no prose. Framing sections (What this is, Non-obvious traps + why, Held for you) may explain the WHY at the length it takes to be understood — that's the point of them. No emojis, no hype, no "great job", no what-went-well/poorly retro anywhere.
6. **Background process / agent IDs are critical.** If you started any `run_in_background` shells or agents, their IDs must appear in "Running state" with how to stop them — the next agent cannot find them otherwise.
7. **Surface what you held.** Anything spend-affecting, production-facing, or destructive that you deliberately did not do goes in "Held for you" (or, for a small session, called out in "Pick up here") — never silently dropped.

## Anti-patterns — do not do these

- Summarizing the last 3 turns and calling it a handoff.
- Listing files by relative path.
- Padding a trivial session with an empty Architecture/Work-shipped/Run-recipe scaffold — omit those when they don't apply.
- Compressing an epic-sized session into the thin template — when the session built a multi-component system, the architecture + work→artifact + traps sections are not optional.
- Skipping a *core* section because "nothing is running" — write "none".
- Writing the summary to any file. Chat-only by design. (Does not apply to the concept-capture step's own `anki.sh` queue write — see hard rule 1's carve-out.)
- A "what went well / what went poorly" retrospective. "Non-obvious traps + why" is forward-looking knowledge, not a retro.
- Burying a held-for-the-human production/spend decision inside a bullet list instead of its own "Held for you" section.
