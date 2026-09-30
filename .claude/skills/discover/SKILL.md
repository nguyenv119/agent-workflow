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

- **Ask Jev which notes to read first.** One command, about a second, a
  fraction of a cent:

  ```bash
  python3 .claude/skills/discover/kb_topics.py route .claude/discover-kb/<repo-slug> \
    "<the user's question, plus your not-confident claims in a sentence>"
  ```

  It prints up to 15 notes, best first (about two seconds, ~600 tokens).
  `pick` lines are Jev's confident choices; `yes` lines are the rest,
  ordered by a per-note yes/no score. Read the titles, open the ones that
  fit, then run the freshness check below on each. On a held-out test the
  right note was in the first 8 lines 86% of the time, so scan the whole
  list rather than stopping at the first line. If none of the 15 titles
  clearly fits a not-confident claim, read `INDEX.md` too before spawning
  verifiers: Opus reading the full index found the right note in its first
  8 picks 90% of the time on the same test, and either one found it 93% of
  the time, so the fallback is worth its ~9k tokens. Jev ranks every note while
  there are at most 255; past that it first keeps only the notes tagged
  with the topics that match the question. If it prints
  "read INDEX.md directly" (no `topics.json` yet, or no `TYPESAFE_API_KEY`),
  fall back to reading `INDEX.md` yourself.
- `INDEX.md` is grouped by topic (the slugs in `topics.json`). Each note
  is listed once, under its first topic; its other topics live on the
  note's own `Topics:` line, and `route` finds it under all of them. Read a
  topic's section when you want to browse rather than ask.
- Keep `INDEX.md` a real index: one line per note, genuinely
  short (a hook + a link, not a summary of the finding). Detail belongs in
  the topic file, never inlined into the index — a bloated index defeats
  the point of having one.

  **The cap is measured in characters, not lines.** An index entry is at
  most **130 characters total**: an optional status marker, a label of
  ≤110 chars, and the link. The label is the topic file's own H1, truncated
  — never a second, longer abstract written into the index. This wording
  used to say "~150 lines"; the file reached 130 KB while obeying it,
  because 204 entries each carrying a full paragraph is still 204 lines.
  Lines are not the unit that costs tokens.
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
  One file per finding: when a finding spans topics, it still gets one
  file, and tagging (next bullet) lists it under every topic it touches.
  Put any status marker (🔴 ✅ ⭐) at the start of the H1.
- **Tag it with Jev, which also files it in the index:**

  ```bash
  python3 .claude/skills/discover/kb_topics.py tag .claude/discover-kb/<repo-slug> <topic-slug>.md [more.md ...]
  ```

  Jev reads each note and writes a `Topics: <first>, <others>` line under
  its H1 (the topics come from `topics.json`), then the script rebuilds
  `INDEX.md` grouped by first topic. A new note's index label is its H1,
  truncated to 110 chars. Don't hand-edit `INDEX.md`; re-run `tag` or
  `kb_topics.py index <kb>` instead. Note text is sent to TypeSafe's API, so
  notes never hold tokens or secrets (they shouldn't anyway).

  If the KB has no `topics.json`: under ~40 notes, skip tagging and add the
  index line by hand as `- [<H1 truncated to 110 chars>](<topic-slug>.md)`.
  At ~40 or more, draft 8–15 topics as `{"slug": "one sentence on what it
  covers"}`, save it as `topics.json` in the KB folder, and run
  `kb_topics.py tag <kb> --all` once.
- **Before finishing, run the index guard.** It is one command, it is cheap,
  and it is the only thing standing between this index and the 130 KB it
  reached once already:

  ```bash
  KB=.claude/discover-kb/<repo-slug>
  awk 'length>220{n++; print "  OVERLONG: "substr($0,1,90)"…"} END{exit n>0}' \
    "$KB/INDEX.md" && echo "index OK ($(wc -c < "$KB/INDEX.md") bytes)"
  ```

  220 is the whole line — a ≤110-char label plus the link — with headroom
  over today's longest entry (184). It is not a style nit: it is the tripwire
  for someone pasting a paragraph into a label again.

  If it reports overlong entries, shorten them before you stop — do not
  leave them for the next run. If `INDEX.md` passes the per-entry check but
  still exceeds **40 KB**, move resolved/superseded topics into `ARCHIVE.md`
  (same directory); the detail files aren't deleted, just unlisted.

A finished note, condensed from a real one:

<!-- few-shot: discover-kb-note-filled -->
```markdown
# 🔎 Few-shot audit: which harness skills show worked examples of good output
Topics: ci-and-dev-workflow
Verified 2026-10-01 by 4 read-only verifiers over every SKILL.md in .claude/skills/.
Freshness: .claude/ is not in git; re-verify if a listed skill changed after 2026-10-01.

## Verdict
Few skills show a finished good output; judgment outputs (pass/fail, risky/not) most
often lack one. Already good: win-condition (filled block :69-101). Missing, with a
recorded miss in the notes: reviewers (no dropped-false-positive sample;
notes/reviewers-miss-out-of-tree-evals.md), test-runner (false PASS;
notes/gate-false-pass.md), discover (Report body is a placeholder).
```

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

<!-- few-shot: discover-report-filled -->
```
## Discover: which harness skills show a worked example

Only a handful do: win-condition shows a filled Win Condition block
(win-condition/SKILL.md:69-101). The judgment outputs are the gap: reviewers
never show a false positive being dropped, and test-runner only has a template
even though the notes record a false PASS on a failing suite
(notes/gate-false-pass.md). You were right that discover itself has no sample;
its Report body is a placeholder.
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
