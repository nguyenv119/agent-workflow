---
name: discover
description: Lead-researcher pattern for grounding what's true. Silently forms a confidence split, verifies against real ground truth (code, logs, MCP, DBs, live APIs) with scoped sub-agents, persists to a knowledge base, and surfaces exactly one triage report at the end. Standalone, and the exploration step /plan runs.
---

# Discover

You act as the lead researcher, not a single explorer. You plan, delegate to
scoped helpers, and synthesize — you never do the deep-verification reading
yourself once claims are identified.

## Silent by default — one report at the end

Nothing below is narrated as it happens. Form the confidence split, absorb
whatever correction the user already gave, plan the research, spawn
verifiers, synthesize, persist to the knowledge base — all of it runs without
progress chatter. No "here's what I'm confident/not confident about" as a
standalone message, no "spawning N agents now," no scope/boundary text
surfaced in chat. The **only** output is the Report in Step 5, one message,
once everything is done.

**Exception:** if the request is too ambiguous to even form a confidence
split, or resolving a claim genuinely requires an either/or only the user can
answer (not something verification can settle), ask ONE crisp question. That
is the only mid-flight interruption this skill makes.

## Step 1 — Form the confidence split (internal only)

Before or after a quick skim, form two lists — internally, not as chat
output. Each item is one specific, checkable question or assertion — it
doesn't need to name a file or location, that's what Step 3 finds. Examples:
"X lives in file Y and does Z" / "clicking this button — which of these two
code paths actually fires?" / "what's in the prod DB for this table, and
does local/seed data match its shape?"

- Confident: claim + one-line reason (memory, an obvious grep, direct read)
- Not confident: guesses, conflicting signals, things unchecked

If the not-confident list is empty, there's nothing to verify — just answer
the original question directly and skip the rest of this skill. Don't
manufacture a Report for a question that had no real uncertainty in it.

## Step 2 — Absorb correction without pausing for it

Use whatever context or correction the user already supplied — in this
message or earlier in the conversation. Don't stop and present the
confidence split for reaction. If the user wants to correct a claim, they'll
say so in normal chat like anything else, and that becomes input to Step 3
the same way a pre-supplied correction would. Only the ambiguity exception
above interrupts this flow.

## Step 3 — Check the knowledge base, then plan the research

**First, check for existing ground truth before spawning anything.** The
knowledge base lives at `.claude/discover-kb/<repo-slug>/` (out-of-tree —
gitignore it in every project this skill runs in, so nothing here ever
enters a repo or a PR). `<repo-slug>` is the basename of
`git remote get-url origin` for the repo being discussed, or the directory
name if there's no remote.

- Read `INDEX.md` — keep it a real index: one line per topic, genuinely
  short (a hook + a link, not a summary of the finding). Detail belongs in
  the topic file, never inlined into the index — a bloated index defeats
  the point of having one.
- For any not-confident claim that has a matching entry: check freshness.
  Each entry records the files it depends on and the commit SHA they were
  last changed at, at verification time. Re-check with
  `git log -1 --format=%H -- <file>`. If the current value still matches
  what's recorded, the entry is fresh — use it directly, skip spawning an
  agent for that claim. If it's changed or the entry is absent, the claim
  proceeds to verification below.

Only claims with real ground truth to find move forward — skip preference
questions.

Ground truth sources, pick per claim:
- code, config, tests, docs, git history
- local logs (runtime behavior, past errors)
- codebase graph MCP tools, when available (`get_architecture_overview_tool`,
  `get_impact_radius_tool`, `semantic_search_nodes_tool`) — faster than raw grep
- a live database query (e.g. comparing prod vs. local/dev data shape)
- a live API call using this project's env-scoped credentials — check this
  project's `CLAUDE.md` (or `.env` / `.env.local` files) for where the actual
  values live and which var maps to which service; every project wires this
  differently, so don't assume a path

Read-only only — a verification call never mutates state to answer a fact.

Partition the remaining not-confident claims into non-overlapping groups (by
file, service, or table touched). One sub-agent per group.

**Vague delegation causes duplicate work or gaps — this is a documented
failure mode** (Anthropic's own multi-agent research system had two
sub-agents both re-research the same supply-chain question because the
lead's instructions were vague). So every spawn uses this block, same
hand-off convention the `coordinator` skill uses for reviewers:

```
ROLE: Discover Verifier
SCOPE: <the exact claims this agent owns, listed by name — nothing else>
BOUNDARY: Do not investigate claims outside SCOPE — other agents own those.

CLAIMS TO CHECK (per claim, both hypotheses):
1. <claim> — mine: "<original guess>" / user: "<their correction, if any>"
2. ...

SOURCES TO USE: <which of the ground-truth sources above apply to these claims>

OUTPUT FORMAT: per claim — verdict (confirmed-mine / confirmed-yours /
actually neither) + concrete evidence (file:line, log line, query result,
API response) + the file(s) this evidence depends on. Do not accept either
hypothesis without finding that evidence yourself.
```

Spawn all groups in parallel (`Explore` type — already has Bash/MCP/WebFetch,
no Edit/Write), one message, multiple Agent calls, "very thorough" breadth.
None of this — the groups, the scopes, the spawns — is narrated to the user.

## Step 4 — Synthesize and persist (still silent)

Collect verdicts. Update the confidence list internally.

If a verdict surfaces a *new* not-confident claim (verifying one thing
revealed another unknown), you may run one more round — same partition +
spawn + synthesize — but cap it at 2 rounds total. More than that, stop and
carry what's still open into the Report rather than looping indefinitely.

**Write newly-verified claims to the knowledge base.** For each claim
resolved this round:
- Create or update `.claude/discover-kb/<repo-slug>/<topic-slug>.md` with the
  claim, verdict, evidence, and the dependent file(s) + their current commit
  SHA (`git log -1 --format=%H -- <file>`) as the freshness marker.
- Add or update its one-line pointer in `INDEX.md` — genuinely one line.
- If `INDEX.md` grows past ~150 lines OR any single entry stops being a true
  one-liner, move resolved/superseded detail into `ARCHIVE.md` (same
  directory) and trim the index line back down — the detail files aren't
  deleted, just unlisted from the top-level index.

## Step 5 — The Report (the only output)

One message, after everything above is finished. **The final verdict only —
a few plain-language sentences, no jargon, no field-by-field structure.**
State what's actually true and the key evidence inline, prose, not labeled
sections:

```
## Discover: <topic>

<2-4 plain-language sentences: what's actually true, folding in the key
evidence (file:line / log line / query result / API response) inline as
part of the sentence rather than as a separate labeled field. If the user's
correction turned out right, say so in passing — don't give it its own
section.>
```

If this ran ahead of `/plan`, note that in the Bottom line so the plan skill
knows to use these findings instead of re-exploring.

## Constraints

- Read-only against the codebase/services being discussed: no edits, no
  issue tracker changes, no code changes, no mutating API/MCP/DB calls.
- The knowledge base itself is the one thing this skill writes — and only
  under `.claude/discover-kb/`, never into the repo being discussed.
- Never resolve a claim by trusting either party without a sub-agent check,
  even if the knowledge base has a stale entry for it.
- Cap research rounds at 2 — this grounds facts, it doesn't run forever.
- No progress narration during Steps 1-4. The Report in Step 5 is the only
  chat output this skill produces, except the single-question exception in
  "Silent by default" above.
