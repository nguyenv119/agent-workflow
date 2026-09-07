---
name: prune
description: Find and remove dead code. knip is the candidate source, a five-surface reachability verifier is the judge, convergence + planted controls are the win condition. Produces verified candidate lists and epics; never deletes on its own — deletion goes through /work, one PR per workspace, apps before packages. Invoked via /prune.
---

# Prune — dead-code removal

## What "dead" means here

"No importer" is a **candidate**, never a verdict. Import graphs cover one of the five ways a file can be reached in a real repo:

| surface | how to check |
|---|---|
| module import | `git grep -lE "from ['\"][^'\"]*/<stem>['\"]"` — quote-insensitive; a `"./x"` grep silently misses single-quoted imports |
| live UI string | the filename or command rendered in a page/component (empty-state instructions, help text) |
| runbook / doc prose | `git grep -n "<stem>" -- '*.md'` — "model it on X", "copy X for new rooms" means X is reference material, not spent |
| runtime error message | `throw new Error("... run scripts/x.ts first")` |
| operator docs outside the repo | uncommitted runbooks, Downloads, Notion — ask the user; no scan can see these |

Plus two traps: `git ls-files '*package.json'` does NOT match the ROOT `package.json` (check it by name); and framework entrypoints (Next.js routes, scheduled/dashboard-invoked tasks) are imported by nothing **by design** — for those, "no importer" proves nothing at all.

**Bias rule (the whole skill hangs on this):** a wrong KEEP costs one more pass. A wrong DELETE costs a revert and the user's trust. So: any single surface hit → **KEEP**. Any ambiguity → **ASK**. **DELETE** requires every surface clear with the evidence lines cited.

## Three homes — never mixed

| what | where | ships in git? |
|---|---|---|
| this skill | harness `.claude/skills/prune/` | harness only |
| `knip.json` + devDependency + `knip` turbo task | product repo, via `/work` | yes — a normal PR |
| controls, eval, candidate lists, per-pass logs | out-of-tree: `.claude/loop-evals/prune/` and `.claude/discover-kb/<repo-slug>/prune/` | **never** (R2) |

An implementer that commits `controls.json` has broken R2. Say which home every artifact belongs to in every bead you file.

## Phase 0 — Tooling (one product-repo PR, once)

Bead: add `knip` as a root devDependency and `knip.json` at the repo root. Configure:
- **workspaces** from the package manager's workspace file (pnpm/yarn/npm).
- **entries** knip can't infer: every framework entrypoint directory (scheduled/dashboard-invoked task dirs, CLI scripts named in root `package.json` scripts), test runners.
- **ignores** for data that is never imported but never dead: seed/fixture JSON, `_data/**` profile stores, bundled asset dirs. Also anything the repo's memory says "never delete".
- a `knip` task in the task runner, **report-only** (non-blocking) for now.

Quick-eligible (small diff, no schema/auth) **only if** the CI wiring is split into its own later bead (Phase 5). Run it once and record the raw candidate counts per workspace in the bead notes — that's the noise baseline.

Existing `noUnusedLocals`/`noUnusedParameters` in tsconfig already catch in-file dead code; knip's job is unused **files**, **dependencies**, and (later) **exports**. Don't add a redundant lint pass.

## Phase 1 — Controls (with the user, once, before any verification)

Build `.claude/loop-evals/prune/controls.json`:
- **≥ 10 known-dead** files: from the first knip report, hand-confirmed by the user.
- **≥ 10 known-live-but-import-invisible** files: things reached only by a non-import surface — a scheduled task, a script named only in a runbook, a file named in a UI string or error message. Start from the repo's memory of past false-dead verdicts.

The verifier is graded against these **before any deletion**, so planted-dead files must still be present when graded. An eval with no fail control has not been tested (win-condition R1/R3). Controls are a floor, not a target — if the verifier is borderline on 10, widen the set.

## Phase 2 — Scan + verify (discover-style; produces lists, deletes nothing)

1. `knip --reporter json > .claude/discover-kb/<repo-slug>/prune/knip-pass-<n>.json`. Passes 1–2 consider **unused files + unused dependencies only**. Unused **exports** are a later pass — an "unused" export in an API layer can be reached through a route table or RPC registry that knip can't see.
2. Partition candidates by workspace. Spawn one **read-only** verifier per workspace (`Explore` type; no Edit/Write), all in one message, with this block — vague delegation produces overlapping or missing checks:

```
ROLE: Prune Verifier
SCOPE: workspace <name> — exactly these candidates: <list>
BOUNDARY: do not touch candidates outside SCOPE; other agents own those.
PER CANDIDATE: check all five surfaces + the root package.json trap + the
quote-insensitive import grep. For framework entrypoints (scheduled tasks,
route files): "no importer" proves nothing — verify against the live
system instead (run history / dashboard / prod query per the repo's
CLAUDE.md) that the one-shot completed AND its output is maintained
elsewhere.
VERDICT per candidate: KEEP | DELETE | ASK, with the evidence line(s)
(file:line, grep hit, API response). DELETE only when every surface is
clear. Do not take knip's word for anything.
```
3. Persist `<workspace>.md` lists (KEEP / DELETE / ASK with evidence) to `.claude/discover-kb/<repo-slug>/prune/`.
4. **Grade the controls.** Any planted-dead → not DELETE, or any planted-live → DELETE: **STOP**, fix the verifier prompt, re-run Phase 2. Do not file beads on a verifier that failed its controls.
5. Show the user the ASK list and the totals. Nothing is deleted yet.

## Phase 3 — File the epics (via `/plan`; `reviewer-plan` runs its adversarial pass)

Pass order is structural — deleting a file exposes the dead exports under it:
- **Pass 1: apps / leaf workspaces.** Unused files + deps.
- **Pass 2: shared packages.** Same scope. Only after Pass 1 is merged and knip re-run.
- **Pass 3: three separate epics** — unused exports (may run in loop mode), `scripts/`-style operator one-shots (human-gated: spent one-shot vs reference material is the user's call), framework tasks from a run-history candidate source (human-gated: needs the live-system proof).

Per epic:
- one bead per workspace; fold tiny workspaces into one bead. **DELETE items only.** ASK items are listed on the epic for the user, never inside a bead.
- bead acceptance: knip reports zero unused files for that workspace **AND** unit + integration suites pass (invoke the test runner directly per workspace — a cached task runner can serve a stale PASS) **AND** the app build succeeds (tests can pass while the build fails) **AND** gates are run alone, not alongside other agents' gates.
- tests of deleted code go with it. **A failing test is never deleted during a prune** — it means the removal was wrong.
- Quick candidates: small leaf workspaces with mechanically-uniform deletions and no schema/auth/CI contact.

Epic `## Win Condition` (the win-condition skill writes it; this is what it must contain):
- **Convergence** — a fresh knip run across all workspaces reports zero unused files and dependencies.
- **Ignore-list guard** — `knip.json`'s ignore list equals the one approved in the Phase 0 PR. Otherwise the loop can "win" by ignoring more.
- **Controls** — all planted-dead scored DELETE, all planted-live scored KEEP (graded in Phase 2, re-asserted here).
- **Zero breakage** — every route file renders (a redirect to sign-in counts as alive) and the count of registered scheduled tasks equals the pre-prune baseline. The eval authors this smoke if it doesn't exist (R2).
- **Bounded** — max 3 passes. A 4th pass finding new files → BLOCKED with the residual list; that means the layering is deeper than expected or the verifier is leaking.
- Eval prints `<promise>WIN</promise>` only when all hold (R5).

## Phase 4 — Delete (`/work`)

One PR per bead. Apps merged before packages. Re-run knip after each layer merges and feed new candidates into the next pass. Loop mode is allowed for passes 1–2 and the exports epic; the operator-script and framework-task epics stay human-gated.

## Phase 5 — Lock it in

Last bead, after Pass 2 merges: flip the `knip` task to **blocking** in CI so dead code can't regrow. This is the durable payoff; without it the prune is a one-off.

## Hard rules

- Read-only until Phase 4, and Phase 4 only through `/work`. This skill never runs `rm`.
- Never widen ignores to reach zero. The ignore list is frozen at the Phase 0 PR.
- Never delete a control file before it is graded.
- Never treat "no importer" as a verdict for framework entrypoints or operator scripts.
- Never delete a failing test to make a prune pass.
- Never file deletion beads from an ungraded or failed-controls verifier run.
