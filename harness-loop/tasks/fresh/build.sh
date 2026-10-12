#!/usr/bin/env bash
# Build the fresh exam set from candidates-<YYYY-MM>.json with SWE-gen. PAID.
#
#   build.sh [--candidates FILE] [--limit N] [--per-task-usd 12] [--usd-per-hour 13.5]
#            [--cc-timeout 3200] [--tasks-dir DIR] [--confirm-budget USD]
#
# 1. Prints the candidate count and estimated cost, and stops unless --confirm-budget
#    is at least the estimate. Nothing is spent before this.
# 2. Checks the daytona SDK, DAYTONA_API_KEY, GEMINI_API_KEY_DEV and the AWS key + region. GITHUB_TOKEN is
#    optional (SWE-gen's PR fetch works unauthenticated, 60 GitHub calls/hour, ~11 per task).
# 3. Per candidate (the first --limit N, default all): daytona_build.py runs
#    `swegen create` through swegen_shim.py in a throwaway Daytona sandbox (see there),
#    itself started from a scratch folder under `env -i` with only an allowlisted env.
#    SWE-gen exits non-zero unless its own nop=0 / oracle=1 validation passes; only
#    passing ids go to passed.txt. Ids already in passed.txt / failed.txt are skipped.
#    After each task its cost (measured from the log, else elapsed x --usd-per-hour) is
#    added to a running total; the loop stops once the total reaches --confirm-budget.
# 4. Writes <tasks-dir>/fresh-<YYYY-MM>.json via collect.py (test_command, workdir,
#    leak check).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS="$(dirname "$HERE")"
DRIVER="$HERE/daytona_build.py"
CANDIDATES=""
LIMIT=""
PER_TASK_USD=12
USD_PER_HOUR=13.5
DAYTONA_USD_PER_HOUR=0.34   # 4 CPU x $0.0504 + 8 GiB x $0.0162, rounded up
CC_TIMEOUT=3200
BUDGET=""

die() { echo "build.sh: $*" >&2; exit "${CODE:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --candidates) CANDIDATES="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --per-task-usd) PER_TASK_USD="$2"; shift 2 ;;
    --usd-per-hour) USD_PER_HOUR="$2"; shift 2 ;;
    --cc-timeout) CC_TIMEOUT="$2"; shift 2 ;;
    --tasks-dir) TASKS="$(cd "$2" && pwd)"; shift 2 ;;
    --confirm-budget) BUDGET="$2"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown argument $1" ;;
  esac
done

if [ -z "$CANDIDATES" ]; then
  CANDIDATES="$(ls "$HERE"/candidates-*.json 2>/dev/null | sort | tail -n 1 || true)"
  [ -n "$CANDIDATES" ] || die "no candidates-*.json; run pick_prs.py first"
fi
MONTH="$(basename "$CANDIDATES" | grep -oE '[0-9]{4}-[0-9]{2}')" \
  || die "candidates file name must contain YYYY-MM"

# 1. Cost gate (exit 3 = refused, nothing spent).
python3 "$HERE/collect.py" plan "$CANDIDATES" --per-task-usd "$PER_TASK_USD" \
  ${LIMIT:+--limit "$LIMIT"} ${BUDGET:+--confirm-budget "$BUDGET"}

# 2. Tools and credentials, all before any paid work.
CODE=4
python3 -c "import daytona" 2>/dev/null \
  || die "the daytona SDK is missing: install harness-loop/tasks/fresh/requirements.txt into the venv on PATH"
[ -n "${DAYTONA_API_KEY:-}" ] || die "set DAYTONA_API_KEY (each task runs in its own Daytona sandbox)"
[ -n "${GEMINI_API_KEY_DEV:-}" ] || die "set GEMINI_API_KEY_DEV (the instruction writer runs on Gemini)"
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION; do
  [ -n "${!v:-}" ] || die "set $v (the build session runs on Bedrock)"
done
CODE=2

# The only env the per-task driver sees. SWE-gen's Claude session runs with permission
# checks off on untrusted repo text, so it runs in a Daytona sandbox, never here, and the
# driver forwards only daytona_build.SANDBOX_ENV into it (DAYTONA_API_KEY stays here).
CHILD_ENV=(
  "DAYTONA_API_KEY=$DAYTONA_API_KEY"
  "PATH=$PATH" "HOME=$HOME" "TMPDIR=${TMPDIR:-/tmp}"
  "OPENAI_BASE_URL=${OPENAI_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai/}"
  "OPENAI_API_KEY=$GEMINI_API_KEY_DEV"
  "SWEGEN_LLM_MODEL=${SWEGEN_LLM_MODEL:-gemini-pro-latest}"
  "SWEGEN_CLAUDE_MODEL=${SWEGEN_CLAUDE_MODEL:-global.anthropic.claude-opus-5-5}"
  "CLAUDE_CODE_USE_BEDROCK=1"
  "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY"
  "AWS_REGION=$AWS_REGION"
)
[ -n "${AWS_SESSION_TOKEN:-}" ] && CHILD_ENV+=("AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN")
# Never the gh login: set GITHUB_TOKEN yourself, to a fine-grained read-only public-repo token.
[ -n "${GITHUB_TOKEN:-}" ] && CHILD_ENV+=("GITHUB_TOKEN=$GITHUB_TOKEN")
while IFS= read -r v; do
  case "$v" in SWEGEN_LLM_MODEL|SWEGEN_CLAUDE_MODEL) ;; *) CHILD_ENV+=("$v=${!v}") ;; esac
done < <(compgen -e | grep -E '^(SWEGEN_|ANTHROPIC_DEFAULT_[A-Z]+_MODEL$|ANTHROPIC_SMALL_FAST_MODEL$)' || true)

LIST="$(python3 "$HERE/collect.py" list "$CANDIDATES" ${LIMIT:+--limit "$LIMIT"})" \
  || die "collect.py list failed"

# 3. Build, from a scratch folder.
OUT="$TASKS/cache/fresh-$MONTH"
mkdir -p "$OUT"
touch "$OUT/passed.txt" "$OUT/failed.txt" "$OUT/spend.tsv"
WORK="$(mktemp -d "$OUT/work.XXXXXX")"
cd "$WORK"
# The budget covers the whole build set: start from what earlier runs already spent.
SPENT="$(python3 -c "import sys; print(round(sum(float(l.split('\t')[1]) for l in open(sys.argv[1]) if l.strip()), 2))" "$OUT/spend.tsv")"
while read -r id repo pr; do
  [ -n "$id" ] || continue
  if grep -qxF "$id" "$OUT/passed.txt" "$OUT/failed.txt"; then
    echo "skip $id (already built)"; continue
  fi
  if python3 -c "import sys; sys.exit(float(sys.argv[1]) < float(sys.argv[2]))" "$SPENT" "$BUDGET"; then
    echo "stopping: spent \$$SPENT of the \$$BUDGET budget"; break
  fi
  echo "== $id ($repo#$pr)"
  start=$SECONDS
  if env -i "${CHILD_ENV[@]}" python3 "$DRIVER" --repo "$repo" --pr "$pr" \
       --out "$OUT/$id" --cc-timeout "$CC_TIMEOUT" </dev/null >"$OUT/$id.log" 2>&1; then
    echo "$id" >>"$OUT/passed.txt"; result=passed
  else
    echo "$id" >>"$OUT/failed.txt"; result="failed (see $OUT/$id.log)"
  fi
  COST="$(python3 "$HERE/collect.py" cost "$OUT/$id.log" \
    --seconds "$((SECONDS - start))" --usd-per-hour "$USD_PER_HOUR" \
    --daytona-usd-per-hour "$DAYTONA_USD_PER_HOUR")" || die "collect.py cost failed"
  read -r usd how <<<"$COST"
  printf '%s\t%s\t%s\n' "$id" "$usd" "$how" >>"$OUT/spend.tsv"
  SPENT="$(python3 -c "import sys; print(round(float(sys.argv[1]) + float(sys.argv[2]), 2))" "$SPENT" "$usd")"
  echo "   $result; \$$usd ($how), running total \$$SPENT"
done <<<"$LIST"

# 4. Manifest.
python3 "$HERE/collect.py" manifest "$CANDIDATES" "$OUT" "$TASKS/fresh-$MONTH.json"
