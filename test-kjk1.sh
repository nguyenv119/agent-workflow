#!/usr/bin/env bash
# Test script for agent-workflow-kjk.1
# Validates shell script syntax and JSON validity

set -e

echo "=== Testing shell script syntax ==="
if [ -f ".claude/hooks/ensure-code-review-graph.sh" ]; then
    bash -n .claude/hooks/ensure-code-review-graph.sh
    echo "PASS: Shell script syntax is valid"
else
    echo "FAIL: ensure-code-review-graph.sh does not exist"
    exit 1
fi

echo ""
echo "=== Testing JSON validity ==="
if command -v jq >/dev/null 2>&1; then
    jq empty < .claude/settings.json
    echo "PASS: settings.json is valid JSON"
else
    # Fallback: try python
    if command -v python3 >/dev/null 2>&1; then
        python3 -c "import json; json.load(open('.claude/settings.json'))"
        echo "PASS: settings.json is valid JSON"
    else
        echo "WARN: Cannot validate JSON (no jq or python3 available)"
    fi
fi

echo ""
echo "=== Testing .gitignore contains .code-review-graph/ ==="
if grep -q "^\.code-review-graph/" .gitignore; then
    echo "PASS: .gitignore contains .code-review-graph/"
else
    echo "FAIL: .gitignore does not contain .code-review-graph/"
    exit 1
fi

echo ""
echo "=== All tests passed ==="
