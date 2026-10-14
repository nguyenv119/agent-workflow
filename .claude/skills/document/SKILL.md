---
name: document
description: Build and keep updating the user's living HTML working document for a session's topic — a local editable page in <pages-dir> with short plain-language sections for the user (why, how it works, diagrams) and a collapsed "For agents" section holding the thorough notes, decisions, verified facts, file paths and discover-KB links. Only the latest update is highlighted, in one color; older text is plain. Use when the user says "document", "put this in the HTML", "update the HTML/doc/tracker/page", "write this up as a page", or asks to keep a running page for a piece of work. Invoked via /document [topic or path].
---

# document

A single HTML page that grows through a session. Two readers use it:

- **The user** reads the main sections: short, plain, first principles, the decisions and brainstorming in brief.
- **Agents** read the collapsed "For agents" section to learn what was decided, what was verified, and where everything lives, so a fresh session can pick the work up cold.

## 1. Find or create the page

`<skill-dir>` below is this skill's folder: the "Base directory for this skill" shown when it loads (usually `.claude/skills/document` in the repo, or `~/.claude/skills/document`).

- If the user names a page or one exists for this topic in `<pages-dir>/`, update that file. Never start a second page for the same topic.
- Otherwise copy the template and fill in the title, date and standfirst:
  ```bash
  cp <skill-dir>/template.html <pages-dir>/<topic-slug>-<YYYY-MM-DD>.html
  ```
  The date is the creation date and stays in the filename across updates.
- The template is the shared page shell: Inter, the color tokens, dither background, Edit/Save buttons (Save rewrites the file via the file picker; Cmd+S works). Keep all of that as is.

**Before every edit**, check whether the user changed the page since your last write (they may edit it in the browser and save). Compare the file's modified time with your last write. If it changed, read it and keep their edits; never overwrite them from a copy you held.

## 2. Structure

Work from `template.html` and the rules here. If a private add-on ships finished example pages in this skill's folder, use them as references, but never Read one whole: pull just the section you need by its heading:

```bash
awk '/<h2>Why/,/<\/section>/' <skill-dir>/<example-page>.html | head -60
```

Swap `Why` for `How it works` or `For agents` as needed. Start the page on day one of a project, even with little known: keep every section and mark the empty ones `To do`, except a layer, which takes no status tags.

Every new finding goes in exactly one place: **Why / How it works** if it explains the system, **Open questions** if it waits on the user, **For agents** (or its linked notes file) for status, findings, plans and everything else. When unsure, For agents.

Main sections, in this order:

1. **Hero**: kicker (`Draft N · date · owner · internal working document`), title, one-sentence standfirst, legend (status tags plus one swatch for the latest update).
2. **Why**: the problem from first principles, at most two short paragraphs.
3. **Done means** (the win condition), right under Why: one bold sentence per goal a script can check, with a status tag. When a goal moves to another epic or is dropped, say so here in one line.
4. **How it works, layer by layer**: one architecture diagram that grows layer by layer (section 4, "The default shape"). Each layer is one `<div class="layer">` holding, in order:
   1. `<h3>`: the layer's point as a sentence.
   2. `<p class="pain">` opening with **Original pain point:** (always red): the concrete problem that made this layer matter, in one or two plain sentences with the real example.
   3. **What it is:** a brief explanation to the first-principles standard (section 5), each term explained where its box first appears.
   4. `<figure class="dia ent">`: the one diagram, redrawn with this layer's boxes highlighted on top of the earlier ones.
   5. `<p class="done"><b>Done:</b> ...</p>`: one or two plain sentences on what is built so far and how it moves Done means.
   Nothing else: no found/plan notes, to-do boxes, task lists or status tags in a layer. Older pages: on the next update, wrap each layer in `div.layer`, move any `div.upd` or `div.ltodo` into For agents ("Earlier turns") and compress what they finished into the layer's Done line.
5. **Open questions** (optional), just before For agents: only the questions waiting on the user right now, written to the first-principles standard in section 5, recommendation in bold. Once answered, the decision goes in the Decisions table and the question leaves the page. One line at the end may list the reader's own small to-dos ("Also on you: ...").
6. **For agents** (`<details class="agents">`, closed by default), holding everything thorough:
   - **Build state** table: bead, branch tip, PR and base, evidence; plus worktree and log paths.
   - **Decisions** table: `D1…Dn`, the decision, who and when. Proposed decisions say "proposed".
   - **Background**: detail moved down from the main sections, in plain text.
   - **Verified facts** with `path/file.ts:line`, commit hash or query, and the date checked.
   - **Files and locations**: absolute paths for every input file, script, output.
   - **Knowledge base and memory**: link every relevant discover-KB file. Find them with
     `grep -ril "<keyword>" <repo-root>/.claude/discover-kb/*/` and each repo's `INDEX.md`. Also link the memory file for this work.
   - **Research sources**: URLs with the sentence that mattered.
   - Detail tables, schemas, numbers, and a **Reproduce** block of commands.

No worklog section. What happened lives in the decisions and facts, and in git.

Noise goes down, not out: when the user says a main section is too long, move the detail into "For agents" instead of deleting it.

**Keep the main page current.** The highlight shows what changed, but it doesn't make old text true. On every update, reread the main sections and prune them:
- **Keep** what still explains the problem and how the system works (the Why, and the first-principles parts of How it works), updated to match what was built.
- **Move down** anything that has served its purpose: discovery findings, corrections, a one-off test plan, answered questions, superseded plans. Put it in "For agents" (Background or Verified facts), or, when it is long, in a markdown notes file beside the page (`<page-name>.notes.md` in `<pages-dir>/`) that "For agents" links to. That keeps the HTML small enough for an agent to read.
- **Fix or delete** text that is no longer true, such as "nothing is built yet" or a status tag that changed. Don't leave a stale claim on the page next to newer text that contradicts it.

The main page reads top to bottom as: Why, Done means, the layer-by-layer picture, Open questions, then the collapsed "For agents". Nothing else sits between those.

**No per-update section on the main page**. An update is written INTO the layers it touches. On every update:
- Strip last update's blue first (section 3), then edit the explanation of each layer whose mechanism changed, in place, coloring only the new or changed sentences. Never replace a layer wholesale with "what this update found".
- A finding that needs a new box goes into the diagram as a new layer, not into a free-standing section.
- Earlier update notes and any older free-standing sections move INTO the "For agents" `<details class="agents">` block (an "Earlier turns" sub-section, newest first). The main page has no collapsed "How we got here" blocks: everything outside For agents is open.
- When a whole earlier draft gets too long, freeze it as `<page>.draftN.html` (a copy with a red "frozen copy" kicker line) and link it, instead of deleting anything.
- When the user says a part must be handed to another agent, write a handoff file and point to it from that layer's Done line.

<!-- few-shot: document-layer -->
```html
<div class="layer">
<h3>Layer 2. A cache keeps recent answers</h3>
<p class="pain"><b>Original pain point:</b> every repeat request recomputed the same answer, about 2 s each.</p>
<p>A <b>cache</b> is a small table in memory: key in, last answer out. ...</p>
<figure class="dia ent">...</figure>
<p class="done"><b>Done:</b> <span class="new">the cache is built; repeat requests now take 0.1 s.</span></p>
</div>
```

<!-- few-shot: document-collapse-earlier-turns -->
```html
<section class="block new"><h2 id="harness-loop">The harness loop, in four parts</h2> ... this turn, open ... </section>
<!-- inside <details class="agents">: -->
<h3>Earlier turns</h3>
<h4>The picture in 8 steps (update 3)</h4> ... the old section's content, as written ...
```

A filled topic section (the latest update), then one "For agents" decision row and fact:

<!-- few-shot: document-section-filled -->
```html
<section class="block new">
  <h2 id="cache">Test 3: does the cache cut page load time?</h2>
  <p class="new"><b>Cached pages load in 120 ms instead of 900 ms; 2 of 50 test pages showed stale data.</b> <span class="st live">Have</span></p>
  <ul class="plain">
    <li class="new">Each test page was loaded cold first, so the cache had to earn every hit.</li>
    <li class="new">Both stale pages had been edited inside the cache window, so the fix is a shorter window, not a new cache.</li>
  </ul>
</section>
<!-- inside details.agents -->
<tr class="new"><td class="mono">D3</td><td>Proposed: cut the cache window from 10 to 2 minutes.</td><td class="nw">proposed</td></tr>
<li class="new">Load test, 50 pages, cold then warm: median 900 ms cold, 120 ms warm; 2 stale reads (checked DATE).</li>
```

## 3. Update color (one color, latest update only)

Only the latest update is colored. Everything older is plain text in the normal ink, with no color class.

On every update:
1. **Strip the previous highlight first.** Remove the `new` class everywhere it appears (`class="new"` → no class; `class="block new"` → `class="block"`; `<span class="new">x</span>` → `x`). Do this before adding anything, so the only colored text on the page is this turn's.
2. **Wrap this update's new or changed text in `new`**: a whole `<section class="block new">`, a `<p class="new">`, a `<tr class="new">`, or a `<span class="new">` inside a sentence.
3. **Legend: one swatch only**, replaced each time: `<span class="new"><span class="sw"></span> update N, DATE: what changed</span>`. Keep the update number counting up so the reader can tell which turn it is.
4. In chat, say "new text is in the highlight color" and summarise what changed.

- **Legacy pages** with `u1`…`uN` classes: on the first update under this rule, neutralise them, either by deleting the `.uN` color rules from the page's style block or by removing the classes, so old text turns black. Then add the `.new` rules from the template if the page lacks them.
- Diagrams are separate: `acc` red still marks the newest part of a growing diagram.
- Specificity trap: `ol.steps p` beats a bare `.new` parent. The template has rules for `ol.steps p.new` and `.new p`; if some new element renders grey, add a rule instead of inline styles.

## 4. Diagrams

Inline SVG in the `figure.dia.ent` convention (commented example in the template): monospace, hairline boxes (`bx`), small caps labels (`lb`), item lines (`it`), arrows (`ar`) with a marker whose id is unique per SVG, captions (`cap`), and `acc` to highlight the one box the page is about. Draw the real mechanism with real names, not a generic flowchart. Keep box text short enough to fit: about 23 characters per line in a 170-wide box.

**Diagrams over tables, and one picture that grows.** On the main page, explain with diagrams, not tables (tables are fine inside "For agents"). Start with one high-level diagram and redraw the same diagram as each idea adds to it, with the new part in `acc` red, rather than drawing a new unrelated figure each time. A zoom diagram is fine for detail inside one box. Paper or field terms go in brackets after a plain name, e.g. "improver (paper: proposer)"; don't add a glossary. Red highlights on arrows and captions need `.ent .ar.acc` and `.ent .cap.acc` rules; add them if the page lacks them.

**The layer-by-layer explanation is the page's goal state: high level, layered, diagrams kept.** Work every update toward one section, just before Open questions, that explains the whole system in layers: layer 1 is one simple diagram, and each later layer redraws that same diagram with what it adds in the orange accent (`acc`), so reading top to bottom shows the picture growing. Keep each layer to a short heading, two or three plain sentences and its diagram. When an update changes how the system works, update the layers (add one, or redraw the affected ones). Never delete these diagrams to shorten the page; move prose detail to "For agents" instead.

**The default shape: less text, more architecture.** Every page's How it works uses this from draft 1:
- **Draw one diagram of the real system.** Use real component names with one real example running through it, e.g. "NEWS RECORD / Kevin Warsh / 816 articles" rather than "Entity". Each box should answer what it is and what it holds for the example.
- **Each layer adds boxes; existing boxes never move.** Fix every box's coordinates once. Layer N draws only the boxes and arrows introduced at layer ≤ N, and the ones new at N are `acc`. Crop the early layers' `viewBox` height so they don't sit on empty space.
- **Generate the layers from one script.** Write a small Python file in the scratchpad that holds each box as (x, y, w, h, label, lines, layer) and each arrow as (x1, y1, x2, y2, layer, style), and prints the SVG for a given layer. Splice its output into the page. Hand-copying five SVGs drifts.
- **Text per layer:** a heading that states the layer's point as a sentence ("Our news lookup skips the rule"), then at most three or four plain sentences, then the diagram. A term gets explained in the layer where its box first appears, not in a glossary.
- **Order layers from the data's origin to the problem.** The last layer shows the path that breaks today as a solid red arrow labelled "today" and the fix as a dashed arrow labelled "fix".
- **Pin every PR to the boxes it changes, on every layer.** Once a page's work has PRs, each box a PR changes carries a small pill tag with the PR number (`#102`) on its top edge, in every layer where that box is drawn, not only the last one. Orange (`acc`) = open, grey = merged. Under each layer's diagram, one legend line per PR whose box is in that layer: number, then what it changes, in plain words. When a PR changes something the picture has no box for (a cache, a queue), add that box in a new layer rather than leaving the PR off the picture. The turn section that reports the PRs reuses the same tagged picture, not a separate PR-stack chart, and states the merge order in its caption. The generator holds the tags as a table, `PR = [(box_key, dx, "#NNNN", merged)]`, next to the boxes; `example-layered-svg.py` has the code.

<!-- few-shot: document-pr-tags-on-layers -->
```text
BAD   A row of four boxes "#101 MERGED -> #102 GREEN -> #103 -> #104" beside the layers.
      Why: says the merge order, not what each PR changes in the system.
GOOD  Layer 1's FIRST PASS box wears #101 (grey) and #104 (orange); Layer 2's CHECKS box
      wears #102; a new CACHE box in Layer 6 wears #103; each layer's legend lists only
      the PRs drawn on it.
```
- **No Backlog or to-do list after the layers.** A short conclusions section after the last layer is optional; then Open questions, then For agents.
- **Reference page:** `<skill-dir>/example-layered.html` (if present; else `template.html`) is a finished example of this shape. Its generator is `<skill-dir>/example-layered-svg.py`: a table of boxes and arrows plus a `svg(layer)` function. Copy and adapt the generator; don't start from scratch. Pull one layer with `awk '/Layer 2\./,/<\/figure>/' <page> | head -60` instead of reading it whole.

## 5. Writing

- Invoke `anti-slop-writing` before drafting.

- **Questions for the user and anything that changed the plan get written to this standard:** "Always explain from first principles, assuming no prior knowledge. Motivate WHY before HOW. Build understanding layer by layer. Be concise at each step. Explain without jargon." The architecture diagrams carry the foundations; the risk is the small deviations and new findings during the work, which arrive without context. So every open question, plan change or new finding on the page gets, in order:
  1. **Background:** one or two plain sentences on the part of the system it touches, as if the reader had never seen the page (point at the layer diagram it lives in).
  2. **Why it came up:** what we found or what changed, with the number that triggered it.
  3. **The choice:** the options in plain words, what each costs or risks.
  4. **The recommendation** in bold, and what happens next if they say yes.
  Never ask a bare question like "accept 830 a day or drop the three?" without steps 1-3 around it.

<!-- few-shot: document-question-with-context -->
```text
BAD   3. Should the Fed news feed take about 830 articles a day? Recommend: start with all 16.
GOOD  3. How big should the Fed news feed be?
      Background: profiles only get new articles a feed asks for, and a model reads each
      article to pull out facts, so every article costs money (layer 1).
      Why it came up: one count says the Fed search would bring about 830 a day; the plan
      assumed 136. Warsh, Powell and Bessent are most of it.
      The choice: take all 830, or leave those three out of this one search.
      Recommendation: take everyone for a few days, look at what arrives, then decide.
```
- Plain language for the reader; exact names, paths and numbers go in "For agents".
- State what is checked versus assumed. Numbers carry their source.
- Use the status tags: `<span class="st live">Have</span>`, `<span class="st soon">To do</span>`, `<span class="st">Ask</span>`.

## 6. Verify, then send

After every page edit, run the checker and fix every line it prints until it says PASS:

```bash
agent-cli page check <page>        # same rules as python3 <skill-dir>/check_page.py <page>
```

It fails a `div.layer` missing its h3, `p.pain`, `figure.dia` or `p.done`, a `Layer ...` h3 outside a `div.layer`, a `div.upd` or `div.ltodo` outside For agents, a legend with more than one update swatch, any em dash in visible text, and `FILL:` text left from a skeleton. Those rules are not repeated here. A project can turn on more (kicker shape, layer part order and lead words, a closed For agents, one legend line shape, unique ids, legacy colour classes) in a `page-rules.json` seam; `agent-cli page --help` lists them. Three commands make the edits the rules want: `agent-cli page mark-update <page> --note "..."` strips the last update's marks and writes the new legend line, `agent-cli page new-layer <page> --title "Layer N. ..."` inserts a full layer skeleton, and `agent-cli page new <slug> --dir <dir> --template <skill-dir>/template.html` starts a page. Then look at the page:

The browser pane cannot screenshot `file://` pages. Render headless and look at it:

```bash
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$C" --headless=new --disable-gpu --hide-scrollbars --window-size=900,6000 \
  --screenshot=<scratchpad>/page.png file://<page>
```

Crop to 900×1600 slices with PIL and Read them. To check the agents section, render a copy with `<details class="agents" open>`. Look for grey text that should be colored, overflowing table notes, and SVG text spilling out of boxes.

Then send it with `SendUserFile` (`display: "render"`), and in chat give a short summary of what changed and that new text is in the highlight color. This is a local file; do not publish it as an Artifact unless the user asks.
