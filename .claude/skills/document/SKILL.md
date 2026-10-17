---
name: document
description: Build and keep updating the user's living HTML page for a project: a local editable page explaining how the system works (why, done means, layers with diagrams) plus a closed "For agents" section with notes, decisions, facts and paths. Load it on any turn that changed how a project's system works (a new piece, a changed mechanism, a new pain point, or an open question the page can't yet explain), without being asked; also when the user says "document", "update the HTML/doc/page", or asks for a running page. Invoked via /document [topic or path].
---

# document

Look for a `## document` section first in the project's `.claude/project-rules.md`, else in the user's `~/.claude/project-rules.md`; follow the first one found; it overrides these defaults (pages dir, where status and questions go, page commands, a companion short page, template).

Defaults, when neither file has that section (`<skill-dir>` is this skill's folder, the "Base directory for this skill" shown when it loads):

- Pages dir: `$DESK_DOWNLOADS` when that env var is set, else `~/.claude/pages` (create it if missing).
- Status and questions: the page's Open questions section, just before For agents, each with background, why it came up, the choice and a bold recommendation.
- Page commands: `agent-cli page <check|mark-update|new-layer|new> ... --rules <skill-dir>/page-rules.json` (`agent-cli page --help`).
- No companion short page. Template: `<skill-dir>/template.html` (page-rules.json names it).

One HTML page per project, growing through the work. The page explains how the system works; status and questions go where the `## document` section says, by default the Open questions section; findings, plans and costs go in For agents. The user reads the open sections cold; For agents, kept closed, is for the next agent.

`agent-cli page check <page> --rules <skill-dir>/page-rules.json` must print PASS before you send; it holds the format rules, so fix what it prints.

## Every turn, unasked

If the turn changed how the system works (a new piece, a changed mechanism, a new pain point, an open question the page can't explain), update the affected layer in place: usually a few sentences and one box. If only status moved, leave the page alone.

## Find or start

- Update the topic's existing page; never start a second one. New: `agent-cli page new <slug> --dir <pages dir> --rules <skill-dir>/page-rules.json`.
- The user edits in the browser: before every edit, compare the mtime with your last write; if it changed, re-read and keep their edits.

## An update

1. `agent-cli page mark-update <page> --note "what changed" --rules <skill-dir>/page-rules.json`.
2. Write the change into the layers it touches, never as a free-standing section; wrap new or changed text in class `new`.
3. New layer: `agent-cli page new-layer <page> --title "Layer N. Its point as a sentence" --rules <skill-dir>/page-rules.json`, then replace every FILL line.
4. Fix or delete text no longer true; move served-its-purpose material (findings, answered questions, superseded plans) into For agents.
5. `agent-cli page check <page> --rules <skill-dir>/page-rules.json` prints PASS.

## Content

- **Why:** the problem from first principles, two short paragraphs. **Done means:** one bold, script-checkable sentence per goal.
- **Layers:** heading stating the point, the original pain point with the real example, a brief first-principles "what it is", the one diagram, a Done line. Nothing else. When a layer hides machinery the user must trust to judge results (how a test runs, what each paid run is for), add zoom layers (4a, 4b) with one real example.
- **For agents:** build state, decisions D1..Dn (who, when), verified facts with source and date, absolute paths, discover-KB links and the memory file, a Reproduce block, earlier turns newest first.
- Too long: move detail into For agents or `<page>.notes.md`; never delete diagrams. Freeze an overgrown draft as `<page>.draftN.html`.
- A short version is a separate page only when the `## document` section names one (e.g. a companion `<page>.exec.html`); this page stays the full one.

## Diagrams

One picture that grows, generated from one script (copy `<skill-dir>/example-layered-svg.py`), with real names and one real example running through it. Each layer adds boxes in `acc` (about 23 characters per line); existing boxes never move. Order layers from the data's origin to the problem: the broken path solid red, the fix dashed. Paper terms go in brackets after a plain name; no glossary. PR pills: orange open, grey merged, one legend line per PR saying what it changes. Tables only inside For agents.

If a private add-on ships finished example pages in this skill's folder, use them as references, but never Read one whole: pull just the section you need by its heading:

```bash
awk '/<h2>Why/,/<\/section>/' <skill-dir>/<example-page>.html | head -60
```

## Writing

- Invoke `anti-slop-writing` first. First principles, why before how, no jargon; explain each term where its box first appears.
- A question for the user goes where the `## document` section says (default: Open questions) with background, why it came up, the choice and your recommendation.
- Exact names, paths and numbers go in For agents. Say what is checked versus assumed.

## Look, then send

Render headless and read 900x1600 slices:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
  --hide-scrollbars --window-size=900,6000 --screenshot=<scratchpad>/page.png file://<page>
```

Check both themes and For agents open; look for SVG text spilling out of boxes. Send with `SendUserFile` (`display: "render"`) and say nothing in chat.
