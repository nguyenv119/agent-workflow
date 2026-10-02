---
name: document
description: Build and keep updating Long's living HTML working document for a session's topic — a local editable page in ~/Downloads with short plain-language sections for Long (why, how it works, diagrams, backlog) and a collapsed "For agents" section holding the thorough notes, decisions, verified facts, file paths and discover-KB links. Every update is written in a new text color. Use when Long says "document", "put this in the HTML", "update the HTML/doc/tracker/page", "write this up as a page", or asks to keep a running page for a piece of work. Invoked via /document [topic or path].
---

# document

A single HTML page that grows through a session. Two readers use it:

- **Long** reads the main sections: short, plain, first principles, the decisions and brainstorming in brief.
- **Agents** read the collapsed "For agents" section to learn what was decided, what was verified, and where everything lives, so a fresh session can pick the work up cold.

## 1. Find or create the page

`<skill-dir>` below is this skill's folder: the "Base directory for this skill" shown when it loads (usually `.claude/skills/document` in the repo, or `~/.claude/skills/document`).

- If Long names a page or one exists for this topic in `~/Downloads/`, update that file. Never start a second page for the same topic.
- Otherwise copy the template and fill in the title, date and standfirst:
  ```bash
  cp <skill-dir>/template.html ~/Downloads/<topic-slug>-<YYYY-MM-DD>.html
  ```
  The date is the creation date and stays in the filename across updates.
- The template is the shared page shell: Inter, the Conspectus tokens, dither background, Edit/Save buttons (Save rewrites the file via the file picker; Cmd+S works). Keep all of that as is.

**Before every edit**, check whether Long changed the page since your last write (he edits in the browser and saves). Compare the file's modified time with your last write. If it changed, read it and keep his edits; never overwrite them from a copy you held.

## 2. Structure

The template is a blanked copy of `example-firehose.html` (in this skill's folder), a finished page frozen as the reference for this shape. The example is about 35,000 tokens: never Read it whole. When unsure how a section should look, pull just that section by its heading:

```bash
awk '/<h2>Backlog/,/<\/section>/' <skill-dir>/example-firehose.html | head -60
```

Swap `Backlog` for `Why`, `How it works` or `For agents` as needed. Start the page on day one of a project, even with little known: keep every section and mark the empty ones `To do`.

Every new finding goes in exactly one place: **Why / How it works** if it rarely changes, **Open questions** if it waits on Long, **Backlog** if it is a piece of work or its status, **For agents** (or its linked notes file) for everything else. When unsure, For agents.

Main sections, in this order:

1. **Hero**: kicker (`Draft N · date · owner · internal working document`), title, one-sentence standfirst, legend (status tags plus one swatch per update color).
2. **Why**: the problem from first principles, at most two short paragraphs.
3. **How it works**: one layer at a time, why before how, plain words. Diagrams go here (section 4).
4. **Open questions** (optional), just before the Backlog: only the questions waiting on Long right now, each with its answer or recommendation in bold, then a few short bullets. Once a question is answered, take it off the main page: the decision goes in the Decisions table, and any lasting explanation moves into Why / How it works. Topic sections for one-off answers follow the same rule. They don't stay on the main page after the question is settled.
5. **Backlog**, in this order: the plan (one bold "Done means" sentence a script can check, then build order); Progress, DATE (state in one bold sentence, then a PR table: item, PR, merge after, what merging turns on); Waiting on an answer (by person); What we're not sure of yet; then the work items grouped by the layer they change, each with **What it is / Why we need it / Without it / Done when** and a status tag.
6. **For agents** (`<details class="agents">`, closed by default), holding everything thorough:
   - **Build state** table: bead, branch tip, PR and base, evidence; plus worktree and log paths.
   - **Decisions** table: `D1…Dn`, the decision, who and when. Proposed decisions say "proposed".
   - **Background**: detail moved down from the main sections, kept in its original colors.
   - **Verified facts** with `path/file.ts:line`, commit hash or query, and the date checked.
   - **Files and locations**: absolute paths for every input file, script, output.
   - **Knowledge base and memory**: link every relevant discover-KB file. Find them with
     `grep -ril "<keyword>" <repo-root>/.claude/discover-kb/*/` and each repo's `INDEX.md`. Also link the memory file for this work.
   - **Research sources**: URLs with the sentence that mattered.
   - Detail tables, schemas, numbers, and a **Reproduce** block of commands.

No worklog section. What happened lives in the decisions and facts, and in git.

Noise goes down, not out: when Long says a main section is too long, move the detail into "For agents" instead of deleting it.

**Keep the main page current.** Colors show what changed, but they don't make old text true. On every update, reread the main sections and prune them:
- **Keep** what still explains the problem and how the system works (the Why, and the first-principles parts of How it works), updated to match what was built.
- **Move down** anything that has served its purpose: discovery findings, corrections, a one-off test plan, answered questions, superseded plans. Put it in "For agents" (Background or Verified facts), or, when it is long, in a markdown notes file beside the page (`<page-name>.notes.md` in `~/Downloads/`) that "For agents" links to. That keeps the HTML small enough for an agent to read.
- **Fix or delete** text that is no longer true, such as "nothing is built yet" or a status tag that changed. Don't leave a stale claim on the page next to a newer color that contradicts it.

The main page reads top to bottom as: what the problem is and why it exists (with diagrams), how it works in first principles, the open questions for Long (if any), the Backlog, then the collapsed "For agents". Nothing else sits between those.

A filled topic section (update 2), then one "For agents" decision row and fact:

<!-- few-shot: document-section-filled -->
```html
<section class="block u2">
  <h2 id="risky-task">Test 3: is this /work task risky?</h2>
  <p class="u2"><b class="u2">Jev ties Opus: 65 of 69 right, 7 of 8 risky tasks caught, 3 false alarms, for $0.00006.</b> <span class="st live">Have</span></p>
  <ul class="plain">
    <li class="u2">Jev read only the ticket, before any code existed, so it could not peek at the answer.</li>
    <li class="u2">The keyword rule never misses but cries wolf on a third of tasks, so keep it as the backstop.</li>
  </ul>
</section>
<!-- inside details.agents -->
<tr class="u2"><td class="mono">D3</td><td class="u2">Proposed: Jev scores /quick eligibility as advice; keyword rule is the backstop.</td><td class="nw">proposed</td></tr>
<li class="u2">Routing test, 69 beads, strict key = PR adds <code class="mono">packages/db/drizzle/*.sql</code> or edits <code class="mono">.github/workflows/</code>: Jev 65/69, Opus 65/69, keyword 48/69 (checked 30 Sep).</li>
```

## 3. Update colors

Every update after the first draft gets the next unused color class, `u1` through `u9` (defined in the template's page style block; add `.u10` the same way if needed). Never reuse one.

- Wrap new or changed text in that class: a whole `<section class="block u2">`, a `<p class="u2">`, a `<tr class="u2">`, or a `<span class="u2">` inside a sentence.
- Add a swatch to the legend: `<span class="u2"><span class="sw"></span> update 2, 30 Sep</span>`.
- Text carried over unchanged keeps the color it already had.
- In chat, name the color used for this update.
- Specificity trap: `ol.steps p` beats a bare `.uN` parent. The template already has rules for `ol.steps p.uN` and `.uN p`; if some new element renders grey, add a rule instead of inline styles.

## 4. Diagrams

Inline SVG in the `figure.dia.ent` convention (commented example in the template): monospace, hairline boxes (`bx`), small caps labels (`lb`), item lines (`it`), arrows (`ar`) with a marker whose id is unique per SVG, captions (`cap`), and `acc` to highlight the one box the page is about. Draw the real mechanism with real names, not a generic flowchart. Keep box text short enough to fit: about 23 characters per line in a 170-wide box.

## 5. Writing

- Invoke `anti-slop-writing` before drafting. No em dashes in page copy:

<!-- few-shot: document-no-em-dash -->
  BAD: `Jev ties Opus — 65 of 69 right — at a sliver of the cost.`
  GOOD: `Jev ties Opus: 65 of 69 right, at a sliver of the cost.`

- Plain language for Long; exact names, paths and numbers go in "For agents".
- State what is checked versus assumed. Numbers carry their source.
- Use the status tags: `<span class="st live">Have</span>`, `<span class="st soon">To do</span>`, `<span class="st">Ask</span>`.

## 6. Verify, then send

The browser pane cannot screenshot `file://` pages. Render headless and look at it:

```bash
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$C" --headless=new --disable-gpu --hide-scrollbars --window-size=900,6000 \
  --screenshot=<scratchpad>/page.png file://<page>
```

Crop to 900×1600 slices with PIL and Read them. To check the agents section, render a copy with `<details class="agents" open>`. Look for grey text that should be colored, overflowing table notes, and SVG text spilling out of boxes.

Then send it with `SendUserFile` (`display: "render"`), and in chat give a short summary of what changed and the update color. This is a local file; do not publish it as an Artifact unless Long asks.
