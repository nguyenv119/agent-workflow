---
name: copy-harness
description: Install this agent harness into another repo that is already cloned, so /plan, /work, /merged, /handoff and the other skills and commands work there. Use it whenever someone wants to move, copy, port, install, set up or refresh the harness, "our skills", "the workflow" or "the slash commands" in another repo or folder, even if they never say "harness". Invoked via /copy-harness <path-to-repo>.
---

# Copy the harness into another repo

The installer in agent-workflow does this job. It lives in `harness-loop/`, which is never installed into target repos, so run it from a clone of agent-workflow:

1. If `AGENT_WORKFLOW_DIR` is unset, tell the user to clone github.com/nguyenv119/agent-workflow and set `AGENT_WORKFLOW_DIR` to that clone, then stop until they have.
2. Run `python3 "$AGENT_WORKFLOW_DIR/harness-loop/install_harness.py" --commit main --target <repo> [--addon <dir>]`. Pass `--addon` only when the user names a private add-on directory (project-only skills and `project-rules.md`, laid out like `.claude/`).
3. If it exits 2, relay its message as-is. A previous install needs `--force`, which deletes only the files that install's manifest lists.
4. Report the commit it printed and the number of files installed.

`agent-cli pr status|wait|land` read three optional lines from the target's `.claude/project-rules.md`, under `## coordinator`. Tell the user they can add them with their repo's real check names (exact, case-sensitive); without `required-checks:` every non-skipped check is required:

```
## coordinator
required-checks: <test check>, <lint check>, <preview deploy check>
advisory-checks: <bot reviewer check>
preview-check: <preview deploy check>
```

The weekly gardener's Pass 3 (`agent-cli hook scan`) reads one more optional line, under `## gardener`: the dirs whose scripts it counts for chores still done by hand (comma-separated, relative to the repo root). Without it, Pass 3 is skipped:

```
## gardener
scan-dirs: <dir with eval or helper scripts>, <another dir>
```
