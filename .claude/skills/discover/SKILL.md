---
name: discover
description: Lead-researcher pattern for grounding what's true. State confidence vs. not-confident, take the user's correction, spawn scoped sub-agents against real ground truth (code, logs, MCP, DBs, live APIs), synthesize, and persist verified facts to an out-of-tree knowledge base. Standalone, and the exploration step /plan runs.
---

# Discover

You act as the lead researcher, not a single explorer. You plan, delegate to
scoped helpers, and synthesize — you never do the deep-verification reading
yourself once claims are identified.

## Step 1 — Confidence split (always shown)

Before or after a quick skim, answer with two lists. Each item is one
specific, checkable question or assertion — it doesn't need to name a file or
location, that's what step 3 finds. Examples: "X lives in file Y and does Z"
/ "clicking this button — which of these two code paths actually fires?" /
"what's in the prod DB for this table, and does local/seed data match its shape?"

- Confident: claim + one-line reason (memory, an obvious grep, direct read)
- Not confident: guesses, conflicting signals, things unchecked

Always show this, even for a single-fact question. If the not-confident list
is empty, stop here — nothing to verify, no agents needed.

## Step 2 — User responds

Plain chat. Confirm, correct, or add context. Only claims that got
contradicted or added-to move to step 3.

## Step 3 — Check the knowledge base, then plan the research

**First, check for existing ground truth before spawning anything.** The
knowledge base lives at `.claude/discover-kb/<repo-slug>/` (out-of-tree —
gitignore it in every project this skill runs in, so nothing here ever
enters a repo or a PR). `<repo-slug>` is the basename of
`git remote get-url origin` for the repo being discussed, or the directory
name if there's no remote.

- Read `INDEX.md` (small — one line per topic, capped around 150 lines).
- For any not-confident claim that has a matching entry: check freshness.
  Each entry records the files it depends on and the commit SHA they were
  last changed at, at verification time. Re-check with
  `git log -1 --format=%H -- <file>`. If the current value still matches what's
  recorded, the entry is fresh — use it directly, skip spawning an agent for
  that claim. If it's changed or the entry is absent, the claim proceeds to
  verification below.

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

## Step 4 — Synthesize and persist

Collect verdicts. Update the confidence list.

If a verdict surfaces a *new* not-confident claim (verifying one thing
revealed another unknown), you may run one more round — same partition +
spawn + synthesize — but cap it at 2 rounds total. More than that, stop and
report what's still open rather than looping indefinitely.

**Write newly-verified claims to the knowledge base.** For each claim
resolved this round:
- Create or update `.claude/discover-kb/<repo-slug>/<topic-slug>.md` with the
  claim, verdict, evidence, and the dependent file(s) + their current commit
  SHA (`git log -1 --format=%H -- <file>`) as the freshness marker.
- Add or update its one-line pointer in `INDEX.md`.
- If `INDEX.md` passes ~150 lines, move resolved/superseded entries into
  `ARCHIVE.md` (same directory) to keep the index small — the detail files
  aren't deleted, just unlisted from the top-level index.

## Step 5 — Report

Per claim: verdict + evidence. Updated confidence list. If this ran ahead of
`/plan`, say so explicitly so the plan skill can skip re-exploring.

## Constraints

- Read-only against the codebase/services being discussed: no edits, no
  issue tracker changes, no code changes, no mutating API/MCP/DB calls.
- The knowledge base itself is the one thing this skill writes — and only
  under `.claude/discover-kb/`, never into the repo being discussed.
- Never resolve a claim by trusting either party without a sub-agent check,
  even if the knowledge base has a stale entry for it.
- Cap research rounds at 2 — this grounds facts, it doesn't run forever.
