#!/usr/bin/env bash
# Auto-install code-review-graph and ensure the watch daemon is running.
# Fast path: <100ms when already installed and watch is alive.
# First-time: ~10s pip install + background build + watch.
# Exits 0 on all failures to never block Claude Code startup.

set -euo pipefail

# --- Ensure watch daemon is running (every session) ---
# The watch daemon auto-rebuilds the graph on file changes. It dies on reboot
# or terminal close, so we restart it every session if needed.
ensure_watch() {
    if command -v code-review-graph >/dev/null 2>&1 && [ -d ".code-review-graph" ]; then
        if ! pgrep -f "code-review-graph watch" >/dev/null 2>&1; then
            nohup code-review-graph watch > ".code-review-graph/watch.log" 2>&1 &
        fi
    fi
}

# Fast path: already installed and built → ensure watch, then exit
if command -v code-review-graph >/dev/null 2>&1 && [ -d ".code-review-graph" ]; then
    ensure_watch
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

# Configure .mcp.json
(code-review-graph install >/dev/null 2>&1) || exit 0

# Copy ignore template if available and target doesn't exist
if [ -f ".code-review-graphignore.template" ] && [ ! -f ".code-review-graphignore" ]; then
    cp .code-review-graphignore.template .code-review-graphignore || true
fi

# Background the initial build, then start watch daemon
mkdir -p .code-review-graph || true
nohup sh -c 'code-review-graph build && code-review-graph watch' > ".code-review-graph/build.log" 2>&1 &

echo "code-review-graph: installed, building in background. Graph tools available next session." >&2
exit 0
