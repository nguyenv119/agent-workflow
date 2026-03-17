# agent-workflow

## The Problem

A single AI agent can't hold an entire project in its context window. Large features require multiple files, multiple steps, and more information than fits in memory. If you try to do it all in one shot, the agent forgets what it read at the beginning by the time it reaches the end.

## The Solution

Break large work into small tasks. Give each task to a fresh agent with a clean context window. Use an external system (not the agent's memory) to track what's done, what's next, and what's blocked.

This repo is that system. Five commands, specialized agents, and structured issue tracking.

## Commands

| Command | What it does |
|---------|-------------|
| `/plan <description>` | Explore the codebase, discuss approach with you, file issues with dependencies |
| `/work <id-or-description>` | Implement, review, and open a PR per task |
| `/merged [branch]` | After you merge a PR: close the task, clean up, unblock dependents |
| `/pr [branch]` | Regenerate a PR summary |
| `/setup-remote` | Connect beads to DoltHub for multi-machine collaboration |

## Quick Start

1. Copy `.claude/` and `AGENTS.md` into your project
2. Replace `CLAUDE.md` with your project's build/test/lint commands

3. `/plan "your feature"` then `/work <epic-id>`

## How It Works

See [EXAMPLE.md](EXAMPLE.md) for a full walkthrough.

tldr: `/plan` decomposes a feature into tasks with dependencies. `/work` picks up the next unblocked task, spawns an implementer agent in an isolated worktree, runs three reviewers in parallel, and opens a PR. You merge. `/merged` closes the task and unblocks the next one. Repeat.

## Multi-Machine Collaboration

By default, tasks live in a local database. For teams, `/setup-remote` connects it to DoltHub. After that, Claude Code hooks automatically sync before reads and after writes. No workflow changes needed.

## Architecture

```
.claude/
  commands/         Slash command routers (plan.md, work.md, merged.md, etc.)
  skills/           Agent behavior definitions (coordinator, implementer, reviewers, planner)
  hooks/            Dolt remote sync scripts (auto-pull before bd, auto-push after bd writes)
  settings.json     Permissions, plugins, hooks
AGENTS.md           Loaded into every session — workflow rules and bd usage
CLAUDE.md           Your project's build/test/lint commands (you customize this)
.beads/             Task database (Dolt) + git-friendly JSONL backups
```

## Requirements

- [Claude Code](https://claude.ai/code) CLI
- [beads](https://github.com/jdelfino/beads)
- [Dolt](https://docs.dolthub.com/introduction/installation)
- `gh` CLI (authenticated)
- Git, `jq`

## License

MIT
