---
name: session-handoff
description: Use when the user says "session handoff", "wrap up session", "hand off", "handoff summary", or wants a structured end-of-session summary before clearing context. Writes the handoff to a file under /Users/nguyenv/.claude/handoffs/ and puts the path plus a two-line gist in chat. A briefing + operator manual for a fresh agent, covering facts verified this session and how they were verified, what was tried that did NOT work and why, what was built, how it fits together, work→PR traceability, non-obvious traps and why, how to run it, running state, key files, what's held for the human, verification, and what is still open with its single next action.
---

# Session Handoff

Produce a repeatable end-of-session artifact so the user can `/clear` and start a fresh agent without losing continuity. The next agent should be able to pick up by reading this alone.

This is a **context-handoff artifact** for a future instance of you — and, when the session built or changed something real, a **briefing + operator manual** for that thing. Not a status report for a stakeholder, not a retro.

**Scale the depth to the session.** A one-file bugfix gets a short handoff (a few sections). A multi-component build or a shipped epic gets the full briefing (architecture, work→artifact table, traps, run recipe). The sections marked *(when applicable)* below are included only when the session warrants them — never pad a small session into a big template, never compress a big session into a thin one.

## When to invoke

User says: "session handoff", "wrap up session", "hand off", "handoff summary", "let's wrap up", "summarize before I clear", or any near-equivalent. Also invoke proactively if the user says they're about to `/clear` without having run it yet.

## How to produce it

**The governing standard: write it so the next agent starts where this session ENDED, not where it began.** Assume the reader has zero memory of this conversation and cannot ask the user questions. Four things carry that weight, and a handoff missing any of them has failed:

- **Facts verified this session, WITH how they were verified** — so they are not re-checked. A fact with no method attached reads as a guess and gets re-derived.
- **What was tried that did NOT work, and why** — so it is not retried. This is the single most expensive thing to lose; a fresh agent will happily burn an hour re-walking a dead end you already closed.
- **Exact paths, IDs, commands, and the gotchas that cost time** — absolute paths, real IDs, paste-ready commands. No "the config file", no "the usual script".
- **What is still open, each with its single next action** — one item, one next move. An open item with no next action is a worry, not a handoff.

1. **Review the full conversation**, not just the last few turns. Handoffs miss things when they only summarize recent context.
2. **Pull state from these sources (in order):**
   - Plan files referenced this session (check `/Users/nguyenv/.claude/plans/` if a plan was mentioned).
   - TodoWrite / task state — any in-progress or pending items.
   - Background processes you started with `run_in_background` — shell/agent IDs are load-bearing for the next agent.
   - Files created or modified this session — you know what you touched; don't grep to re-discover.
   - Tracked work you shipped — beads/issues closed, PRs opened/merged, commits (you know these from the session; don't audit).
   - Memory files written or updated (`/Users/nguyenv/.claude/projects/<project>/memory/`).
   - Discover knowledge-base entries — if `discover` ran this session, the facts it verified and wrote to `.claude/discover-kb/<repo-slug>/` (index + topic files, each with a freshness marker) so the next agent trusts them instead of re-verifying.
   - Session concept graph — if `/graph` was used this session, the map at `~/.claude/graphs/<session-id>/graph.mmd` (session id = `$CLAUDE_CODE_SESSION_ID`) captures the concepts/architecture visually. Point the next agent at it.
   - **Approaches that failed** — what you tried that did not work, the symptom, and the root cause if you found it. You know these from the session; they are the highest-value content in the handoff and the easiest to forget to write down.
   - Unresolved questions — things you asked the user that never got a clear answer, or things the user asked that got deflected.
   - **Decisions you deliberately did NOT act on** — anything spend-affecting, production-facing, or destructive that you held for the human. These are the easiest thing to lose and the most dangerous.
3. **Do NOT audit the filesystem to reconstruct.** This is synthesis of what happened in THIS session. No broad `Glob`/`git log` sweeps to rediscover. (Pulling a PR number or commit hash you already produced this session is fine.)
4. **Write the handoff to a file, then post a pointer in chat.**
   - Path: `/Users/nguyenv/.claude/handoffs/YYYY-MM-DD-<short-slug>.md` (flat dir, always the same location — NOT the repo and NOT a worktree, which `/merged` deletes).
   - In chat, post ONLY: the absolute path, plus a two-line gist (what this session did / what is next). Never paste the full handoff into chat.
   - Do not update memory from this skill.

   Why a file and not chat: the file survives `/clear` and context compaction, and the next agent can re-read the detail at the moment it needs it instead of paying for the whole briefing up front.

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

## Ruled out — do not retry
<what was attempted this session that did NOT work, each with the reason it failed. Include the
approach, the symptom, and the root cause if it was found. If nothing was ruled out, write "none".
A fresh agent reads this to avoid re-walking dead ends — this is not a retro, it is a fence.>

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
- Concept graph: `~/.claude/graphs/<session-id>/graph.mmd` (if `/graph` was used — the visual map of this session's concepts; open its viewer with `graph.sh open`)
- Discover KB: `.claude/discover-kb/<repo-slug>/INDEX.md` (if `discover` ran — verified facts from this session, already fresh-checked; the next agent can trust these without re-verifying)

## Held for you (gated / irreversible)        (when applicable)
<decisions deliberately NOT taken, awaiting the human: spend-affecting switches, production
cutovers, deletes, secret rotation, external sends. State exactly what's needed to proceed and
why you held. A recommendation is allowed here.>

## Verified this session — and how
<facts established this session, each with the method that established it, so they are not
re-checked: the query run, the file read, the live probe, the log line. A bare claim with no
method does not belong here.>
- <fact> — verified by `<command / file:line / probe>`

## Verification — how to confirm things still work
- `<command>` — <expected outcome>

## Deferred + open questions
- Deferred: <item> — <why pushed to later> (link a filed bead/issue if one exists)
- Open: <question needing the user's input> — <context> — **next action:** <the single next move>

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

1. **File output, chat pointer.** The summary goes to `/Users/nguyenv/.claude/handoffs/YYYY-MM-DD-<slug>.md`. Chat gets the absolute path and a two-line gist, nothing more. Never update memory from this skill. *Carve-out:* the concept-capture step may invoke `anki.sh`, which writes to `.learning/queue.jsonl` when Anki is unreachable (see learned.md §4) — that is the capture flow's own persistence and is unrelated to where the handoff itself is written.
2. **Never invent state.** If a *core* section (Where it started, Ruled out, Running state, Key files, Verified this session, Verification, Deferred + open, Pick up here) has nothing to report, write "none" — don't omit it. The *(when applicable)* sections are the opposite: omit them entirely when they don't fit, rather than writing an empty heading.
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
- Leaving an open item with no next action attached.
- Pasting the full handoff into chat instead of the file path plus a two-line gist.
- Writing the handoff into the repo or a worktree — `/merged` deletes worktrees and the handoff goes with them.
- Stating a verified fact with no method attached — the next agent cannot tell it from a guess, so it gets re-checked.
- Omitting the dead ends because "nothing shipped from them" — the failed attempts are the expensive knowledge.
- A "what went well / what went poorly" retrospective. "Non-obvious traps + why" is forward-looking knowledge, not a retro.
- Burying a held-for-the-human production/spend decision inside a bullet list instead of its own "Held for you" section.
