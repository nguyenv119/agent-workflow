#!/usr/bin/env bash
# Auto-install code-review-graph on first Claude Code session
# Fast path: <100ms when already installed
# First-time: ~10s pip install + background build
# Exits 0 on all failures to never block Claude Code startup

set +e  # Don't exit on errors

# Fast path: check if already installed and initialized
if command -v python3 >/dev/null 2>&1; then
    if command -v code-review-graph >/dev/null 2>&1 && [ -d ".code-review-graph" ]; then
        exit 0
    fi
fi

# No python3? Exit gracefully
if ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

# First-time setup: install the package
echo "code-review-graph not found, installing..." >&2

# Try pip3 first, then pip as fallback
if command -v pip3 >/dev/null 2>&1; then
    pip3 install --user code-review-graph >/dev/null 2>&1
elif command -v pip >/dev/null 2>&1; then
    pip install --user code-review-graph >/dev/null 2>&1
else
    echo "No pip found, skipping code-review-graph installation" >&2
    exit 0
fi

# Verify installation succeeded
if ! command -v code-review-graph >/dev/null 2>&1; then
    echo "code-review-graph installation failed, skipping" >&2
    exit 0
fi

# Run code-review-graph install to write .mcp.json and git hooks
code-review-graph install >/dev/null 2>&1 || {
    echo "code-review-graph install failed, skipping" >&2
    exit 0
}

# Copy .code-review-graphignore.template to .code-review-graphignore if template exists and target doesn't
if [ -f ".code-review-graphignore.template" ] && [ ! -f ".code-review-graphignore" ]; then
    cp .code-review-graphignore.template .code-review-graphignore
fi

# Background the initial build - don't block startup
mkdir -p .code-review-graph
nohup code-review-graph build > ".code-review-graph/build.log" 2>&1 &

echo "code-review-graph installed, build running in background" >&2
exit 0
