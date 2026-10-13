# Verdict library for loop evals (bash 3.2+): source "$(agent-cli eval lib --sh)"
#   ok <msg>       record a passing check
#   fail <msg>     print it and exit 1
#   refuse <msg>   exit 2 (refused)    blocked <msg>   exit 3 (blocked)
#   win            print the WIN sentinel as the last line and exit 0, only if no fail was recorded;
#                  with EVAL_ONLY set (agent-cli eval run --only) it exits 5 (partial) and never prints it
# A fail inside a subshell ( ... ) or $( ... ) is recorded in a marker file keyed by $$ (the
# script's own pid in every subshell), so win still refuses after it.
_VERDICT_MARK="${TMPDIR:-/tmp}/agent-cli-verdict.$$"
rm -f "$_VERDICT_MARK"
ok() { printf 'ok: %s\n' "$*"; }
fail() {
  printf 'fail: %s\n' "$*"
  if [ "${BASH_SUBSHELL:-0}" -gt 0 ]; then : > "$_VERDICT_MARK"; fi
  exit 1
}
refuse() { printf 'refused: %s\n' "$*"; exit 2; }
blocked() { printf 'blocked: %s\n' "$*"; exit 3; }
win() {
  if [ -n "${EVAL_ONLY:-}" ]; then
    printf 'partial: only %s ran, no WIN\n' "$EVAL_ONLY"
    exit 5
  fi
  if [ -e "$_VERDICT_MARK" ]; then
    rm -f "$_VERDICT_MARK"
    printf 'fail: win refused, a check failed earlier\n'
    exit 1
  fi
  printf '%s\n' '<promise>WIN</promise>'
  exit 0
}
