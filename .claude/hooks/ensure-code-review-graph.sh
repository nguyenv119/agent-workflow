#!/usr/bin/env bash
# Auto-install code-review-graph on first Claude Code session
# Fast path: <100ms when already installed
# First-time: ~10s pip install + background build
# Exits 0 on all failures to never block Claude Code startup

set -euo pipefail

# Fast path: already installed and built → exit immediately (<100ms)
if command -v code-review-graph >/dev/null 2>&1 && [ -d ".code-review-graph" ]; then
    exit 0
fi

# No python3? Skip gracefully.
command -v python3 >/dev/null 2>&1 || exit 0

# --- First-time setup (runs once per project) ---

# Install the package (try pip3 first, then pip)
if ! command -v code-review-graph >/dev/null 2>&1; then
    if command -v pip3 >/dev/null 2>&1; then
        pip3 install --user code-review-graph >/dev/null 2>&1 || true
    elif command -v pip >/dev/null 2>&1; then
        pip install --user code-review-graph >/dev/null 2>&1 || true
    else
        exit 0
    fi

    # Verify installation succeeded
    command -v code-review-graph >/dev/null 2>&1 || exit 0
fi

# Configure .mcp.json and git hooks
(code-review-graph install >/dev/null 2>&1) || exit 0

# Copy ignore template if available and target doesn't exist
if [ -f ".code-review-graphignore.template" ] && [ ! -f ".code-review-graphignore" ]; then
    cp .code-review-graphignore.template .code-review-graphignore || true
fi

# Background the initial build — don't block startup
mkdir -p .code-review-graph || true
nohup code-review-graph build > ".code-review-graph/build.log" 2>&1 &

echo "code-review-graph: installed and building in background. Graph tools available next session." >&2
exit 0
