# CLAUDE.md

This is a reference implementation of an agent-friendly development workflow for Claude Code. Copy the `.claude/` directory and `AGENTS.md` into your project, then customize this file for your project.

## Customizing for Your Project

Replace this file with your project's CLAUDE.md. It should include:

### Project Overview

Describe your project in 2-3 sentences. What does it do? What's the tech stack?

### Commands

Document your build, test, and lint commands:

```bash
# Examples — replace with your actual commands
make build           # Build the project
make test            # Run all tests
make lint            # Lint all code
```

### Quality Gates

The skills reference a **Quality Gates** table in CLAUDE.md. Define what commands to run for each area of your codebase:

| Area | Tests | Lint | Typecheck |
|------|-------|------|-----------|
| Backend | `make test-api` | `make lint-api` | — |
| Frontend | `npm test` | `npm run lint` | `npx tsc --noEmit` |
| Integration | `make test-integration` | — | — |

### Development Guidelines

Document your project's conventions:
- Testing requirements
- Code style preferences
- Architecture patterns

## Workflow

This project uses an agent-friendly development workflow. Use `/plan` to decompose features into issues and `/work` to implement them. See `AGENTS.md` for full documentation and `.claude/skills/` for agent instructions.
