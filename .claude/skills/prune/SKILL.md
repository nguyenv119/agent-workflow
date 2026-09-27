---
name: prune
description: Find and remove dead code with reasoning attached to every verdict. knip (run out-of-tree, never installed) is the candidate source for files/deps; live run-history is the candidate source for scheduled/dashboard tasks; a reachability verifier is the judge; planted controls + convergence are the win condition. Produces verified lists and epics; the ONLY thing that ever enters the product repo is a deletion PR. Invoked via /prune.
---

# Prune — dead-code removal

## What "dead" means here

"No importer" is a **candidate**, never a verdict. A file can be reached six ways; import graphs cover one:

| surface | how to check |
|---|---|
| module import | `git grep -lE "from ['\"][^'\"]*/<stem>['\"]"` — quote-insensitive; a `"./x"` grep silently misses single-quoted imports |
| live UI string | the filename or command rendered in a page/component (empty-state instructions, help text) |
| runbook / doc prose | `git grep -n "<stem>" -- '*.md'` — "model it on X", "copy X for new rooms" means X is reference material, not spent |
| runtime error message | `throw new Error("... run scripts/x.ts first")` |
| **task trigger by id** | multi-line grep (`git grep -nE -A2 "tasks\.trigger(AndWait)?|batchTrigger"`) for the task's **id string** — calls often put the id on the next line, so a single-line grep under-counts; an id passed as a variable or template string is **unresolvable → KEEP** every task it could name |
| operator docs outside the repo | uncommitted runbooks, Downloads, Notion — no scan can see these; the user usually can't remember either, so this surface is covered by the bias rule, not by asking |

Plus the traps: issue-tracker exports (`.beads/backup/*.jsonl`, ticket text) are planning prose, not reach — at most a weak doc hit → ASK, never KEEP on their own; `git ls-files '*package.json'` does NOT match the ROOT `package.json`; framework entrypoints (routes, scheduled/dashboard tasks) are imported by nothing **by design**; machine-generated docs (e.g. a `_bmad-output/` tree) are not a reachability surface — exclude them from mention greps.

**Bias rule (the whole skill hangs on this):** a wrong KEEP costs one more pass. A wrong DELETE costs a revert and the user's trust. So: any single surface hit → **KEEP**. Any ambiguity → **ASK**. **DELETE** requires every surface clear with the evidence lines cited. **Untracked files are never candidates** — deleting them is unrecoverable; list them for the user separately and move on.

**Every verdict carries a `why:` line** a human can check in ten seconds: the surfaces checked, what hit, and (for tasks) the run counts and dates. A verdict without reasoning is not a verdict.

## Two homes — the product repo gets deletions only

| what | where | ships in git? |
|---|---|---|
| this skill | harness `.claude/skills/prune/` | harness only |
| knip config, its frozen hash, controls, eval, candidate lists, inventories, per-pass logs | out-of-tree: `.claude/loop-evals/prune/` and `.claude/discover-kb/<repo-slug>/prune/` | **never** |
| the deletions | product repo, via `/work`, one PR per workspace | yes — the only thing that ever lands |

knip is never installed into the repo: `pnpm dlx knip@<pinned> --config .claude/loop-evals/prune/knip.json --reporter json --no-progress --no-exit-code` — **pin the version** (an unpinned `dlx` pulls whatever is newest and silently changes the candidate set between passes); the pinned version string is recorded next to the config hash and the eval asserts both. No devDependency, no task-runner task, no CI wiring. Consequence: dead code can regrow, so Phase 5 is a periodic rescan, not a CI gate.

## Phase 0 — Tooling (out-of-tree, no PR)

**Freshness preflight (lived 2026-09-07: a whole pass ran against a checkout 216 commits behind; one target workspace had already been deleted upstream):** before any scan, `git fetch origin && git rev-list --count HEAD..origin/main` must be 0 on the checkout you scan, or scan a fresh worktree of `origin/main`. Record the scanned SHA in every list you write; a candidate list without a SHA is not evidence.

Write `.claude/loop-evals/prune/knip.json`: workspaces from the workspace file; **entries** knip can't infer (task dirs discovered by the framework config, CLI scripts named in root `package.json`); **ignores** for never-imported-never-dead data (seed/fixture JSON, `_data/**` stores, bundled asset dirs, anything memory says never delete). Run the baseline, record per-workspace counts, then **freeze**: `shasum -a 256 knip.json > knip.json.sha256`. The eval asserts the hash; widening ignores to reach zero is the loop's favourite cheat. Strip the dotenv banner line from the JSON output before parsing.

## Phase 1 — Controls (evidence-selected; the user reviews the reasoning, not each file)

The user cannot be expected to know 300 one-off scripts by name — that surface is exactly the one no one can see. So controls are chosen by evidence and the user sees a short reasoning list to veto, not a quiz:
- **Expected DELETE (≥ 10)** — git-tracked AND zero text mentions repo-wide (md/ts/tsx/json/yml/.github, generated docs excluded) AND a one-off header or name. Reasoning line per file: the mention grep that returned nothing + the header.
  **Plus an effects screen (lived 2026-09-07: 9 of 11 mechanically-picked dead controls turned out to be live by effect, not by mention):** the file must not be the *sole importer* of otherwise-live code, not the *only writer* of an asset read at runtime, and not named in or implied by an *in-flight rollout/migration doc*. A file can have zero mentions and still hold live code up.
- **Expected KEEP (≥ 10)** — unused by import AND named on a non-import surface, with the exact `file:line` cited. Seed from memory of past false-dead verdicts, then from runbook mentions.
**Best DELETE-control signal (validated 2026-09-07, 14/14):** files a human already deleted as dead in past cleanup commits and never restored. `git log --diff-filter=D --name-only` over cleanup-style commits → screen each at the scan SHA (still absent; zero stem mentions; every import still resolves; not the sole importer of anything; no runtime-asset writes) → `git checkout <sha>^ -- <path>` into the scan worktree so they are staged plants (never committed, never pushed; `git reset --hard` removes them after grading). The human's past judgement is the second signal, and it is one the verifier cannot compute. Expect knip to miss any plant placed under an entry dir — hand the plants to the verifier regardless.
**Independence rule:** the verifier will run the same greps, so a control chosen only by grep tests nothing but determinism. Every DELETE control needs a **second signal the verifier does not compute** (git: the file has exactly one commit ever and it predates the last run of whatever it fed; or the platform shows its task ran once and never again). KEEP controls should span surfaces — at least three from UI strings / runtime errors / trigger-by-id / past incidents, not runbooks alone — so a verifier that only checks docs fails them. Write `.claude/loop-evals/prune/controls.json` with the evidence inline. Present the reasoning list; the user vetoes what they recognise; silence on a file is acceptance. Grade the verifier against these **before any deletion** — planted-dead must still exist when graded.

## Phase 2 — Scan + verify (produces lists, deletes nothing)

**2a. Files + dependencies (knip).** Passes 1–2 = unused files + unused dependencies only; unused **exports** are a later pass (an API layer's "unused" export can be reached via a route table or RPC registry knip can't see). Drop untracked files from the candidate list up front. **knip is silent about anything declared as an entry** — the task dirs, and every workspace `scripts/` dir the config lists so CLI heads stop showing as unused (lived 2026-09-07: 5 of 14 planted-dead files sat in `apps/*/scripts` and `packages/*/scripts` and knip flagged none). For those directories the candidate source is a mechanical listing, not knip: every tracked file there that no package.json `scripts` value names, nothing imports (quote-insensitive stem grep), and no doc mentions — then the same verifier judges it. Partition by workspace; spawn one **read-only** verifier per workspace (`Explore` type), all in one message:

```
ROLE: Prune Verifier
SCOPE: workspace <name> — exactly these candidates: <list>
BOUNDARY: do not touch candidates outside SCOPE; other agents own those.
PER CANDIDATE: check all six surfaces + the root package.json trap + the
quote-insensitive import grep. Exclude machine-generated doc trees from
mention greps. "No importer" proves nothing for framework entrypoints.
VERDICT per candidate: KEEP | DELETE | ASK + `why:` (surfaces checked,
what hit, file:line). DELETE only when every surface is clear.
Do not take knip's word for anything.
```

**Quarantine (lived 2026-09-07: 17 of 21 real candidates were < 14 days old; two DELETE verdicts landed on same-day "delete after use" scratch files):** a file whose first commit is younger than 14 days is never DELETE in this pass — at most ASK, addressed to its author. The tree cannot show whether a fresh scratch tool has finished its use. Compute first-commit date and author per candidate (`git log --diff-filter=A --follow --format='%ad %an' -- <path>`) after the verdicts return; it also feeds the PR table's provenance column.

**Operator docs outside the repo ARE visible from the harness (lived 2026-09-07: a by-the-rules DELETE was live via a harness skill and a memory note):** after the verifiers return and before any DELETE is shown, grep the harness itself — `.claude/skills/`, `.claude/discover-kb/` (excluding this lane's own dir), and the memory dir — for every DELETE stem. A hit there is a KEEP with the harness path as evidence. This is the coordinator's step, not the verifier's (the verifier must not see the lane's controls or KB).

**2b. Scheduled / dashboard tasks (run history — knip is blind here).** Once a task dir is a knip entry, knip is silent about it forever, so usage comes from the live system:
1. **Inventory from source**, not filenames: parse every `task({ id })` / `schedules.task({ id })` / `schemaTask({ id })` — ids ≠ filenames, one file can hold several ids (cron + manual twin), and a `backfill*` name can be a cron. Record `file | id | kind(scheduled|ondemand|oneshot) | last_commit`.
2. **Inventory from the platform** (see the repo's KB for the exact endpoints and traps): the union of task-type queues ∪ schedules' task ids ∪ source ids. Anything in source but absent from the platform: `why: never deployed or custom-queued` → ASK.
3. **Probe usage per task**, never by paging all runs: one filtered call per id with an **explicit** time window (the default window is silently short), `page[size]=10`, modest concurrency. Record `runs_90d, runs_365d (or max retention), last_run, last_status`.
4. **Decide by kind, with reasoning:**
   - **scheduled** (active schedule): never a delete candidate on run history. Missed or all-failing recent runs → `ASK why: schedule active, last N runs FAILED`.
   - **on-demand**: zero runs across the **whole visible retention window** (state its actual reach in the `why:` — e.g. "window reaches 2026-05-01"; if it reaches < 180d, ASK not DELETE) AND no trigger-by-id caller in code → DELETE candidate, `why: no code caller (multi-line grep …), 0 runs since <oldest visible date>`. Any caller → KEEP regardless of runs.
   - **one-shot**: `ran ≥ 1` in the visible window AND `runs_90d == 0` AND no caller AND its output is maintained elsewhere (a prod query naming the table/rows) → DELETE candidate, `why: ran N× last <date>, output = <table> still populated (read-only prod count), no caller`. "Output maintained elsewhere" is concrete: read the task's write target from source (the ORM `.insert(<table>)` / `.update(<table>)` symbol), then a read-only prod count on that table per the repo's prod-read path — never a guess. **Never ran in the visible window** → ASK (either never needed, still pending, or older than retention) — never DELETE on absence alone. A doc that says "copy this backfill for new rooms" → KEEP (reference material).
5. Tests for a deleted task go with it; orphan tests (a `*.test.ts` with no sibling) are their own small candidates.

**2c.** Persist `<workspace>.md` and `trigger-tasks.md` (verdict + why per item) to the KB. **Grade the controls, in two directions.** Any planted-live → DELETE is a verifier failure: STOP, fix the verifier prompt, re-run. A planted-dead → ASK/KEEP with a concrete effect-based reason is usually a *controls* failure (the mechanical screen missed a live effect): record it, fix the screen, and turn the reason into a project-level question for the user. Either way, no deletion beads from this run. Show the user the ASK list, the untracked list, and totals. Nothing is deleted yet.

## Phase 3 — File the epics (`/plan`; `reviewer-plan` runs its adversarial pass)

Pass order is structural — deleting a file exposes the dead exports under it:
- **Pass 1: apps / leaf workspaces.** Unused files + deps.
- **Pass 2: shared packages.** Same scope, only after Pass 1 merged and knip re-run.
- **Pass 3, three separate epics:** unused exports (may run in loop mode); operator scripts (human-gated); platform tasks from 2b (human-gated — each bead line carries the `why:` so the user's review is reading, not remembering).

Per epic: one bead per workspace (fold tiny ones); **DELETE items only, each with its `why:` line**; ASK items listed on the epic for the user, never inside a bead. Bead acceptance: knip (out-of-tree config) reports zero unused files for that workspace AND unit + integration suites pass (invoke the test runner directly — a cached task runner serves stale PASSes) AND the app build succeeds AND gates run alone. Tests of deleted code go with it; **a failing test is never deleted during a prune**. Quick candidates: small leaf workspaces, mechanically-uniform deletions, no schema/auth/CI.

Epic `## Win Condition` (the win-condition skill writes it; it must contain): **convergence** (fresh knip run reports zero unused files/deps); **config guard** (`knip.json` hash equals the frozen one); **controls** (all planted-dead DELETE, all planted-live KEEP); **zero breakage** (every route renders — a redirect to sign-in counts as alive; scheduled-task count equals the pre-prune baseline; eval authors this smoke if absent, R2); **bounded** (max 3 passes; a 4th finding new files → BLOCKED with the residual list). `<promise>WIN</promise>` only when all hold (R5).

## Phase 4 — Delete (`/work`)

One PR per bead, apps before packages, knip re-run after each layer. Loop mode allowed for passes 1–2 and exports; scripts and platform-task epics stay human-gated.

**Deletion beads always go through `quick`, never the full coordinator flow** (Long, 2026-09-07). A deletion carries no new code, so there is nothing for an implementer to implement and nothing for correctness/tests/architecture reviewers to judge; the only question that matters — does anything still use this? — was answered *before filing* by the six-surface verifier, with evidence. The `quick` deny-list labels (auth, shared infra, deploy bundle) describe new-code risk and do not apply to a proven-unused file. What replaces the reviewers is **one composite gate, run once, before the PR**: build + typecheck + tests run directly per workspace (a cached runner serves stale PASSes) + the bead's real acceptance (e.g. the bounded bundle run) + a deterministic diff-shape check — `git diff --name-status origin/main...HEAD` may contain only the listed `D` files, the named manifests/lockfile, and the listed comment trims; a deleted `*.test.*` without its deleted sibling fails. The human PR review is the second gate. Three LLM reviewers re-reading a deletion diff is ceremony, not safety.

**PR body contract (Long, 2026-09-07): one table row per PATH in `git diff --name-status base...HEAD`, no exceptions — deleted files, modified files (comment trims, tsconfig prose, manifests, `pnpm-lock.yaml`), added files, and every dependency/catalog row inside a manifest edit.** A modified file's row says exactly what changed in it and why (e.g. "comment on line 80 named the deleted race-tabs.tsx; reworded"; "lockfile regenerated by `pnpm install` after the removals above — no hand edits"). The lived failure: the first tables covered only deletions, so a reviewer opening the PR found 30+ paths with no row. **Mechanical check (part of the diff-shape step):** every path in the diff must appear verbatim in the table, and every table path must be in the diff; a script compares the two sets and fails the bead on any mismatch. The reviewer reading the PR must be able to judge each deletion without opening the KB. Columns:

| path (file or dependency row) | what it does / did | what changed and why (for a deletion: why it is safe to remove) |

- *what it does / did* — in the file's own words where it has a header comment (quote or paraphrase the first meaningful line, plus its last-commit date and commit count); for a dependency row, what the package provides and who was expected to use it.
- *what changed and why* — for a deletion, the concrete evidence: which surfaces were checked and what came back (zero importers; the only text hit is X and X is a comment; the live duplicate is Y; its output table still has N rows; the real consumer declares its own copy), plus any human decision it rests on ("Long: room seeds are one-off").
Gate results go in a second, smaller table. **Stacked PRs (when the user asks for a stack):** GitHub retargets a PR to `main` the moment its base branch disappears, and then shows the WHOLE stack's diff. Before writing the table, confirm `gh pr view <n> --json baseRefName,changedFiles` matches the bead's own diff; if the base has been retargeted, restore it (`gh pr edit <n> --base <branch>`) or re-push the base branch first. The table always describes the bead's own diff, never the accumulated stack.

## Phase 5 — Keep it pruned (no CI, so a rescan)

Nothing was added to the repo, so nothing stops regrowth. Schedule a report-mode rescan (weekly is plenty) as a scheduled routine (the `schedule` skill): Phase 2a + 2b with the pinned knip and frozen config, diff against the last lists in the KB, post the delta with reasoning. Deletion still goes through Phase 3–4. Filing that routine is the last step of the first prune, not an afterthought.

## Hard rules

- Nothing from this lane enters the product repo except deletion PRs. No knip install, no config file, no CI wiring.
- Read-only until Phase 4, and Phase 4 only through `/work`. This skill never runs `rm`.
- Untracked files are never candidates.
- Never widen ignores to reach zero; the config hash is frozen in Phase 0.
- Never delete a control file before it is graded; never file deletion beads from an ungraded or failed-controls run.
- Never treat "no importer" as a verdict for framework entrypoints or operator scripts; never treat a filename as a task kind.
- Never delete a failing test to make a prune pass.
- Every verdict has a `why:` line.
