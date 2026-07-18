---
name: graph
description: Per-session concept/architecture graph. Maintains a Mermaid diagram of the ideas and architecture discussed this session, rendered in a local HTML viewer, so the user has a glanceable map to update instead of re-reading past messages. Invoked via /graph.
---

# Concept graph

A living Mermaid diagram of **this session's** concepts and architecture. Unlike
the implementer/planner skills (which capture the *model's* work), this captures
the **user's mental model** — you summarize what they're learning/deciding into
nodes and edges. One graph per session, keyed on `$CLAUDE_CODE_SESSION_ID`.

Source of truth: `graph.mmd` (find it with `bash SKILL_DIR/graph.sh path`).
`viewer.html` is generated from it — never hand-edit the HTML.

## Every invocation

1. **Locate + read** the current graph:
   `MMD=$(bash <skill-dir>/graph.sh path)` then Read `$MMD`. It always exists
   (seeded to `graph TD`). Understand what's already mapped before changing it.
2. **Apply the user's request** as a *surgical* Edit to `$MMD` — add a node,
   add/relabel an edge, group under a subgraph, or restructure. Keep it valid
   Mermaid. Do not rewrite the whole file when a small edit suffices.
3. **Rebuild + show:** `bash <skill-dir>/graph.sh open` (first time this session,
   opens the browser) or `build` (later, just regenerates — the tab auto-refreshes
   every 2s). Give the user the `file://` path once.
4. **One-line summary** of what changed. Nothing more.

If the user gives no instruction (bare `/graph`), just build/open and report the
path — treat it as "show me the graph."

## Draw for understanding (this is the whole point)

The graph exists so the user *grasps something they don't yet*. It is a
comprehension aid, not a documentation dump. Optimize every graph for "can I
understand this at a glance," borrowing from the `teach` skill:

- **Fewest nodes that carry the idea. Aim for 5–9, never a wall.** Every node
  must earn its place: if deleting it loses no understanding, delete it. Collapse
  a list of specifics into ONE node ("runs a batch of safety checks"), not one
  box per item. Detail the user didn't ask for lives in a `%% comment` or the
  chat reply — not on the canvas.
- **Name clusters by the QUESTION they answer, not the tool.** "Is the code OK?"
  beats "GitHub Actions + Blacksmith." Why-first, made visual. The reader should
  see *what each part is for* before *what it's called*.
- **Plain words in the box; the real term demoted.** Like teach's *elii* mode:
  everyday phrase on line 1, the technical name quietly on a second line
  (`Rebuilds the site<br/>billed by Vercel`). Never a bare acronym or jargon
  token as the whole label. No `next build`, `CodeQL`, `migration guard` as
  labels — say what they DO.
- **Make the contrast the shape.** When the insight is a distinction (A vs B,
  before vs after, this-not-that), lay the sides apart so the difference is
  obvious at a glance. Don't bury it under incidental cross-edges.
- **No prose boxes.** A paragraph in a node is a smell. One-line takeaways go in
  your chat reply, not the canvas.
- **Edges are plain-verb relationships:** `a -->|blocks| b`, `a -->|pays for| b`,
  `a -.->|small overlap| b`. Not technical connectors.
- **Optional everyday analogy** when a concept is abstract (teach's *eli5*) — a
  parallel node, only if it genuinely clarifies.

## After drawing: check understanding, then layer (the teach loop)

Don't treat the graph as one-shot. After you build/update it, ask the user which
node is still fuzzy, and offer to **expand just that one** into its own detail —
add a small subgraph hanging off it, on demand. Reveal detail layer by layer as
they ask, never pre-loaded. This keeps every view minimal while letting them
drill into exactly the part they don't get.

## Mechanics (keep it hand-editable and mergeable later)

- **Title + icon (do this automatically, never ask).** Keep two comment lines
  right under `graph TD`:
  `%% title: <3–6 word summary of what this graph is about>` and
  `%% icon: <one emoji that fits the topic>`. They set the browser tab name and
  favicon. Refresh the title as the graph's subject sharpens; pick the emoji once.
- `graph TD` top-down. Stable snake_case node ids, human labels:
  `auth_flow["Auth flow"]`. Ids stay put so edits and future cross-session merges
  are clean; only labels change wording. `<br/>` gives a second line.
- Group related concepts in a `subgraph` titled with the question it answers.
- Content is the **user's** conceptual model in their words — not internal model
  state, not file-by-file detail. Summarize; don't dump.
- `%% comments` survive rendering — use them for detail parked off-canvas.

## Scope

Per-session only. Combining several sessions' graphs is deliberately out of scope
for now — that's an ad-hoc "read these N graph.mmd files and synthesize a new one"
job for later, not something this skill does.
