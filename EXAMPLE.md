# A Concrete Workflow

### **Let users connect their own OAuth account to the chatbot**.

The app is a typical web service with a handful of API endpoints, a database, an auth layer and a chatbot that answers through a model key the company owns. Every user's usage bills to that one key. Some users want their own provider account to pay instead. The work is a new set of endpoints that sit beside the existing ones and reuse the chatbot.

---

## Step 1: Brainstorm and discover

Brainstorm and discovery are one step: you say what you think you know, and the agent checks it against what is actually there.

You start by writing down what you think you know, unpolished:

> LMK what you're not confident about in mechanically building this and how you understand this specific task. `/discover` and `/document` for me.

We have `/api/chat` and it uses our server key. I want users to connect their own OAuth account with the provider and have the chatbot use their token instead. Probably a connections table, a start and callback route, and a change in the chat handler. I'm not sure how we store tokens today, or whether the auth middleware can tell which user a callback belongs to.

The agent runs `discover`. It sorts your message into what it can already answer and what it cannot, looks in the knowledge base for notes written in earlier sessions, then sends read-only sub-agents to check the rest against the code. Each sub-agent owns a separate question, so two of them never read the same files.

You get one reply:

> The chat handler reads the key from `config.modelKey` at `chat/handler.ts:41` and never looks at the user. Sessions are cookie-based, and the middleware attaches `req.user`. That means the callback route will know who is returning. The app has no secret-storage code. Anything that stores tokens will be new, and I found no encryption helper. One thing I could not settle: whether the provider's sandbox allows a redirect URI on localhost. Worth checking before anyone writes the callback.

Each finding is saved to the knowledge base with the files it depends on, so the next session starts from it instead of re-reading the same code.

You answer the open question, correct anything it got wrong, and go again. Stop when the agent's "could not confirm" list is empty or something you have chosen to defer. The agent is working from two kinds of understanding at this point: what the task is, and how the code is laid out.

If you want a record you can read later, ask for the `document` skill. It writes a page in `~/Downloads` with short plain-language sections for you (why, how it works, backlog) and a collapsed "For agents" section that holds the long notes. That section mostly links: to the knowledge-base notes, to file paths, to the tracker. The page stays short enough to read, and an agent that opens it follows the links it needs and ignores the rest.

---

## Step 2: Plan

### `/plan`

Planning starts from the discovery you just did. The planner picks up the discover notes instead of exploring again, discusses the tradeoffs with you (where tokens are stored, whether to fall back to the server key, what revoking does), and then files an epic and tasks:

```
epic: bd-c41   "Bring-your-own OAuth for chat"
  bd-c41.1  Connections table, with tokens encrypted at rest
  bd-c41.2  Start and callback endpoints            <- depends on .1
  bd-c41.3  Chat handler prefers the user's token   <- depends on .1
  bd-c41.4  Revoke endpoint                         <- depends on .2
```

Two things in the epic description do most of the work later. The first is the **problem statement**, copied in whole: users share one billing key and some want to pay with their own account. When you ask the planner to put it there, every later agent can read the reason before it reads the task.

The second is the **win condition**, covered in step 3.

### Plan review

A plan reviewer reads the filed tasks against the code and tries to break them. A typical finding on this plan:

> `.3` says the chat handler decrypts the user's token, but `.1` only creates `encrypt` and `decrypt` inside `crypto.ts`, which the handler cannot import without a layering violation (handlers go through services, per the existing pattern in `services/`). Add a service function to `.1` or make `.3` depend on a new task.

You decide, the planner updates the tasks, and the reviewer runs again. `/plan` runs the reviewer once and shows you the result. To loop until approval, say so: "keep running the plan reviewer after each fix until it approves."

---

## Step 3: Backlog

The backlog is the set of units of work, the graph between them, and the win condition. Each unit of work says what it is, why it exists, what breaks without it, and how to prove it.

### What a task looks like

Each task has to be understood by an agent that has never seen this conversation. Here is `bd-c41.1`:

```markdown
## Summary
Add a `connections` table (user_id, provider, token_ciphertext, created_at) so the
chat handler can later use a user's own token. Without it, nothing downstream has
anywhere to read a token from, and .2 and .3 cannot be tested against a real row.

## Files to modify
- db/migrations/0042_connections.sql (new)
- lib/crypto.ts (new: encrypt/decrypt with the app's existing KMS key)

## Implementation steps
1. Migration creates the table with a unique (user_id, provider).
2. crypto.ts exposes encrypt(plaintext) and decrypt(ciphertext).

## Real acceptance (dev database, never prod)
1. Apply the migration from an empty database. It succeeds.
2. Insert a row through encrypt(); read the raw column. It is not equal to the
   plaintext. decrypt() on it returns the plaintext.
3. Control: inserting a second row for the same user and provider fails with a
   unique-violation.
```

The Summary says what the task is, why it exists, and what stays broken without it. That last part is what lets you cut tasks that do not need to exist: if nobody can say what breaks, the task goes.

"Real acceptance" is a command and an expected result, in a dev copy of the system. Unit tests still run in CI. They are not the acceptance bar, because an agent can write a mock that agrees with its own code. A schema change, shared infrastructure, or anything touching production data has to have a real check here. The coordinator runs it before asking you to review, and attaches the output to the PR.

### The dependency graph, and what runs at the same time

The dependency graph is where most of the planning effort goes. It decides what can run at the same time. `.2` and `.3` both wait only on `.1`, and they edit different files, so once `.1` is in (merged, or stacked under them) they can run in parallel, each in its own worktree. `.4` depends on `.2` because both edit the connect router, and two agents editing one file produce a merge conflict nobody asked for. When the coordinator is unsure, it adds a dependency.

```
.1 --+--> .2 ---> .4
     |
     +--> .3
```

---

### The win condition

The epic carries one check for the whole feature. Per-task acceptance proves each piece. The win condition proves the pieces connect.

```markdown
## Win Condition

**Outcome**: A user can connect a provider account, chat using that account's token
instead of the server key, and revoke it, all in the dev environment.

**Verification**: bash .claude/loop-evals/bd-c41/byo-oauth.sh

**Done signal**: the script exits 0 AND its last line is <promise>WIN</promise>.
The script prints that line only after every assertion passes.

**Does NOT count**:
- Unit tests passing
- A stubbed token store or a mocked provider client inside the app
- Any manual step during the run

**Stop conditions**:
- max-iterations: 8
- stagnation: no progress in 3 iterations, or the same error 5 times -> BLOCKED
```

The script drives the running dev app over HTTP against a stub provider that records every request it receives:

1. Sign in as a test user and run the start and callback routes against the stub provider.
2. Send a chat message. Assert the stub provider received a request carrying the *user's* token, and none carrying the server key.
3. Revoke. Send another message. Assert the server key is used again.

Three habits make the check worth trusting:

- **It states the end result, not the absence of errors.** "The provider saw the user's token" is something that happened. "No exceptions were thrown" is also true of an app that does nothing.
- **It is checked against cheats before you rely on it.** For each assertion, ask the cheapest way to print WIN without the outcome holding: hard-code the token in the stub, count requests instead of reading what they carried. Each cheat gets its own assertion or goes into "Does NOT count."
- **It has a fail control and a pass control.** Run it on the code from before the feature: it must fail at step 1. Run it with a deliberately broken chat handler that still sends the server key: it must fail at step 2. A check that has never failed has not been shown to discriminate.

The script lives in `.claude/loop-evals/bd-c41/`, outside the product repo. It is scaffolding for the loop. It never becomes a task or a PR.

The dev copy is whatever your project can safely break: a database branch plus a dev deploy, a docker-compose stack, a seeded local database. Production sees the change only after you merge.

---

## Step 4: Work, once per task

### `/work`

The coordinator finds the unblocked tasks. At the start that is only `.1`. Once `.1` is in, `.2` and `.3` unblock together, and each gets its own agent, worktree and branch at the same time. The steps below are the same for every task.

For `bd-c41.1`:

1. Creates a worktree and branch from the current main.
2. Spawns an **implementer** that writes failing tests first, implements, runs the quality gates, checks coverage and commits.
3. Reads the shared standards files and builds a **checklist per reviewer**, each reviewer getting only its relevant items, injected into its prompt.
4. Spawns three **reviewers** in parallel (correctness, tests, architecture). Each answers every checklist item with N/A, PASS or FAIL and gives a verdict. A hook on the `Agent` tool blocks any spawn that skips the injected checklist, so enforcement does not depend on the coordinator remembering.
5. Fixes trivial findings and files issues for the rest.
6. Runs the task's **real acceptance** and records the output.
7. Pushes and opens a PR.

What you see is short:

```
BEAD 1 COMPLETE
bd-c41.1: added the connections table and encrypted token storage so the
next two tasks have somewhere to read a token from. Tests pass, real acceptance:
migration applied from empty, ciphertext differs from plaintext, duplicate
(user, provider) rejected. PR: https://github.com/acme/app/pull/212.

Waiting for approval to proceed to bead 2.
```

### Review

You read the PR. The acceptance output is attached, so you can see what was checked and against what. If something is off, `/work bd-c41.1` picks up the same branch. When you are happy, you can merge it, or you can leave it open and stack the next task on it. Step 5 covers both.

---

## Step 5: Merge or stack, then go around again

Merging is not the end of the loop. It is one of two ways to move on.

**Merge, then continue.** After you merge a PR on GitHub, `/merged feature/bd-c41.1-connections` verifies the merge, closes the task, removes the worktree and deletes the branch. `.2` and `.3` branch from a main that now contains `.1`. This is the default. It keeps a bad foundation from carrying forward, at the price of you being the pacing step.

**Stack, then continue.** When you want to keep moving, the next task's branch starts from the previous task's branch, and its PR sits on top. Nothing has merged yet. `.2` builds on `.1`'s branch, and you review the stack when you are ready. The coordinator will not open the next PR while the previous one has red checks, so a broken lower layer stops the stack and does not carry into the layers above it.

Either way, the loop continues until the win condition passes. If the epic has one, `/work` offers to run unattended. Each round the coordinator runs the win-condition script and reads the result. If it exits 0 and prints WIN, the loop stops. If it does not, the coordinator picks the next move: run a ready task, file a new task for the gap the failure revealed, make a small fix, or stop. A task gets three attempts. The loop also stops at the iteration cap, or when it stalls (no progress in three rounds, or the same error five times).

In the example, a run of the script before `.3` lands fails at its step 2, because the chat handler still sends the server key. That failure is the next task's reason to exist. When `.1` through `.4` are all in and the script prints WIN, the epic is done.

Merges stay yours. A passing win condition says the feature works in the dev copy. It does not merge anything.

---

## Why the checks sit where they do

- **Brainstorm** catches a wrong picture of the codebase while it is cheap to fix. In this example, learning that no encryption helper exists changes the plan before anything is built.
- **Plan review** catches structure: missing dependencies, tasks that collide, assumptions that no task supplies.
- **Real acceptance per task** catches a piece that works alone and does not work for real.
- **The win condition** catches pieces that each pass and do not fit together.
- **Your merge** stays a gate you control. Merge each PR to stop a bad foundation carrying forward, or stack PRs to keep moving and review the stack later. Either way the loop only ends when the win condition passes.

---

## Quick path: small fixes

For a typo, a copy change or a one-line fix with an obvious cause, ask for the `quick` skill. It still uses a worktree, a branch and a PR, but with one reviewer, and it refuses anything involving schema, auth, payments or CI config and hands that to `/work`.

---

## Standards enforcement

Shared standards (`quality.md`, `correctness-patterns.md`) hold rules learned from past incidents. The question is how to be sure a spawned reviewer used them. The harness does not tell reviewers to go read a file. The coordinator extracts the relevant sections and puts them in the prompt as a numbered checklist, and a `PreToolUse` hook on the `Agent` tool looks for section headers from the standards files before allowing the spawn. A reviewer launched with ad-hoc instructions is blocked.

```
Coordinator reads standards files
  -> extracts relevant sections per reviewer
  -> injects them as a numbered checklist
  -> hook checks for the signatures before the spawn is allowed

Reviewer answers each item: N/A, PASS or FAIL
  -> verdict: APPROVED or CHANGES NEEDED
```

---

## Multi-machine collaboration

The task database is local, a Dolt database in `.beads/dolt/`. Dolt has `push` and `pull` with git semantics, and DoltHub plays GitHub's role. Two hooks make sync automatic: pull before any `bd` command, push after any `bd` write. Both do nothing without a configured remote, and a network failure never blocks the agent. Run `/setup-remote` once per project.

This does not stop two machines from claiming the same task at the same moment. On one machine the coordinator prevents it. Across machines, small teams can coordinate by talking, and larger teams would need a lock on top.