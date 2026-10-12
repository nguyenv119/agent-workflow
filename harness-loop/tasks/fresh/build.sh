#!/usr/bin/env bash
# Build the fresh exam set from candidates-<YYYY-MM>.json with SWE-gen. PAID.
#
#   build.sh [--candidates FILE] [--per-task-usd 12] [--cc-timeout 3200] [--confirm-budget USD]
#
# 1. Prints the candidate count and estimated cost, and stops unless --confirm-budget
#    covers it. Nothing is spent before this.
# 2. Checks that swegen and docker are installed.
# 3. Per candidate: `swegen create` through swegen_shim.py (instruction writer on Gemini,
#    build session on Bedrock Opus, capped by --cc-timeout). SWE-gen exits non-zero
#    unless its own nop=0 / oracle=1 validation passes; only passing ids go to passed.txt.
#    Ids already in passed.txt or failed.txt are skipped, so a rerun resumes.
# 4. Writes ../fresh-<YYYY-MM>.json via collect.py (test_command, workdir, leak check).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS="$(dirname "$HERE")"
CANDIDATES=""
PER_TASK_USD=12
CC_TIMEOUT=3200
BUDGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --candidates) CANDIDATES="$2"; shift 2 ;;
    --per-task-usd) PER_TASK_USD="$2"; shift 2 ;;
    --cc-timeout) CC_TIMEOUT="$2"; shift 2 ;;
    --confirm-budget) BUDGET="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "build.sh: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [ -z "$CANDIDATES" ]; then
  CANDIDATES="$(ls "$HERE"/candidates-*.json 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$CANDIDATES" ] || { echo "build.sh: no candidates-*.json; run pick_prs.py first" >&2; exit 2; }
fi
MONTH="$(basename "$CANDIDATES" | grep -oE '[0-9]{4}-[0-9]{2}')" \
  || { echo "build.sh: candidates file name must contain YYYY-MM" >&2; exit 2; }

# 1. Cost gate (exit 3 = refused, nothing spent).
python3 "$HERE/collect.py" plan "$CANDIDATES" --per-task-usd "$PER_TASK_USD" \
  ${BUDGET:+--confirm-budget "$BUDGET"}

# 2. Tools.
if ! command -v swegen >/dev/null 2>&1; then
  echo "build.sh: swegen is not installed. Installing it (uv pip install swegen) needs Long's OK." >&2
  exit 4
fi
command -v docker >/dev/null 2>&1 || { echo "build.sh: docker is required by swegen create" >&2; exit 4; }

# Model routing (see the README): Gemini for the instruction writer, Bedrock for Claude Code.
: "${GEMINI_API_KEY_DEV:?build.sh: set GEMINI_API_KEY_DEV (the instruction writer runs on Gemini)}"
: "${GITHUB_TOKEN:=$(gh auth token)}"
export GITHUB_TOKEN
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
export OPENAI_API_KEY="$GEMINI_API_KEY_DEV"
export SWEGEN_LLM_MODEL="${SWEGEN_LLM_MODEL:-gemini-pro-latest}"
export SWEGEN_CLAUDE_MODEL="${SWEGEN_CLAUDE_MODEL:-global.anthropic.claude-opus-5-5}"
export CLAUDE_CODE_USE_BEDROCK=1
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN

# 3. Build.
OUT="$TASKS/cache/fresh-$MONTH"
mkdir -p "$OUT"
touch "$OUT/passed.txt" "$OUT/failed.txt"
while read -r id repo pr; do
  if grep -qxF "$id" "$OUT/passed.txt" "$OUT/failed.txt"; then
    echo "skip $id (already built)"; continue
  fi
  echo "== $id ($repo#$pr)"
  if python3 "$HERE/swegen_shim.py" create --repo "$repo" --pr "$pr" \
       --output "$OUT/$id" --state-dir "$OUT/.swegen" --cc-timeout "$CC_TIMEOUT" </dev/null \
       >"$OUT/$id.log" 2>&1; then
    echo "$id" >>"$OUT/passed.txt"; echo "   passed"
  else
    echo "$id" >>"$OUT/failed.txt"; echo "   failed (see $OUT/$id.log)"
  fi
done < <(python3 "$HERE/collect.py" list "$CANDIDATES")

# 4. Manifest.
python3 "$HERE/collect.py" manifest "$CANDIDATES" "$OUT" "$TASKS/fresh-$MONTH.json"
