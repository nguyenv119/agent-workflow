# House rules on top of cli-for-agents

`SKILL.md` beside this file is cursor's upstream skill, unedited (pin in `UPSTREAM`); never edit it here.
These three rules are ours. Each extends one upstream section; where they disagree, these win.

## 1. One exit-code table per CLI (extends "Fail fast with actionable errors")
- Every command in a CLI shares one table, and the CLI's top-level `--help` prints it.
- 0 ok; 1 checked and failed; 2 usage error or refused; 3 and up named once per CLI.
- A number never changes meaning: callers and evals branch on it.

## 2. Every read command takes `--json` (extends "Success output")
- With `--json`, stdout is one JSON object per line and nothing else; logs and child output go to stderr.
- Without it, print `key: value` lines carrying the same fields.

## 3. Never prompt (extends "Non-interactive first")
- Upstream allows an interactive fallback when flags are missing. We do not, not even when stdin is a TTY.
- A missing value exits 2 with the error and a correct invocation; a bare command group prints its own help.
