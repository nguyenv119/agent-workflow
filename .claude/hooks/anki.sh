#!/usr/bin/env bash
# anki.sh — path-independent AnkiConnect helper (localhost:8765, version 6).
#
# Shared by any repo that carries this harness: skills/commands call it as
# `bash .claude/hooks/anki.sh ...` relative to their own repo root. Contains
# NO repo-relative assumptions internally: the offline queue is written
# relative to the CALLING repo's $PWD. ANKI_URL is the only env-overridable
# config; DECK and MODEL below are hardcoded constants.
#
# Usage:
#   anki.sh capture <concept> <summary> <context> <source>
#   anki.sh concepts
#   anki.sh due
#   anki.sh info <cardId> [cardId ...]
#   anki.sh answer <cardId> <ease>
#   anki.sh flush
#   anki.sh version
#
# Guardrails: AnkiConnect only, never touch collection.anki2. Unreachable Anki
# never errors out of the caller's session — captures queue to
# .learning/queue.jsonl and this script exits 0.

set -euo pipefail

ANKI_URL="${ANKI_URL:-http://localhost:8765}"
DECK="Concepts"
MODEL="Concept"
QUEUE_FILE=".learning/queue.jsonl"

# anki_request <action> <params_json>
# POSTs {"action":..., "version":6, "params":...} to AnkiConnect and prints
# the .result JSON on success. Returns non-zero if the request fails or
# AnkiConnect reports a non-null error.
anki_request() {
  local action="$1"
  local params="${2:-}"
  [[ -n "$params" ]] || params='{}'
  local body response error
  body="$(jq -nc --arg action "$action" --argjson params "$params" \
    '{action: $action, version: 6, params: $params}')"
  if ! response="$(curl -s --max-time 5 "$ANKI_URL" -X POST -d "$body" 2>/dev/null)"; then
    return 1
  fi
  if [[ -z "$response" ]]; then
    return 1
  fi
  error="$(jq -r '.error' <<<"$response" 2>/dev/null || echo "parse_error")"
  if [[ "$error" != "null" ]]; then
    return 1
  fi
  jq -c '.result' <<<"$response"
}

# ensure_available
# Checks AnkiConnect; if unreachable, launches Anki in the background (no
# focus steal) and polls for ~15s. Prints a one-line Login Items nudge once
# per launch attempt. Returns non-zero if still unreachable — callers must
# fall back to the offline queue rather than erroring out.
ensure_available() {
  if anki_request version >/dev/null 2>&1; then
    return 0
  fi

  open -ga Anki 2>/dev/null || true
  echo "anki.sh: launching Anki (tip: add Anki to macOS Login Items so this isn't needed after reboot)" >&2

  local i
  for i in $(seq 1 15); do
    sleep 1
    if anki_request version >/dev/null 2>&1; then
      return 0
    fi
  done

  return 1
}

# ensure_deck_model
# Idempotently creates the Concepts deck and the Concept note model
# (fields Concept/Summary/Context/Source, one card: front={{Concept}},
# back={{Summary}}) so plain mobile Anki review still works as regurgitation.
ensure_deck_model() {
  anki_request createDeck "$(jq -nc --arg d "$DECK" '{deck: $d}')" >/dev/null

  local models
  models="$(anki_request modelNames)"
  if ! jq -e --arg m "$MODEL" 'index($m) != null' <<<"$models" >/dev/null 2>&1; then
    local params
    params="$(jq -nc --arg m "$MODEL" '{
      modelName: $m,
      inOrderFields: ["Concept", "Summary", "Context", "Source"],
      css: ".card { font-family: arial; font-size: 20px; text-align: center; }",
      cardTemplates: [{
        Name: "Card 1",
        Front: "{{Concept}}",
        Back: "{{Summary}}"
      }]
    }')"
    anki_request createModel "$params" >/dev/null
  fi
}

# escape_query_value <value>
# Escapes a value for embedding in a quoted Anki search-query field term
# (e.g. Concept:"<escaped>"). Anki's search syntax treats backslash, double
# quote, and the wildcards * and _ as special inside a quoted term; each must
# be backslash-escaped or an unusual concept name (embedded quote, or one
# that happens to contain * / _) either breaks the query or silently
# broadens the match to unrelated notes. Verified against live AnkiConnect
# with values containing spaces, parens, and an embedded double quote.
escape_query_value() {
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//\"/\\\"}"
  v="${v//\*/\\*}"
  v="${v//_/\\_}"
  printf '%s' "$v"
}

# queue_capture <concept> <summary> <context> <source>
# Appends a capture as a JSONL row to .learning/queue.jsonl relative to the
# CALLING repo's $PWD (not this script's location) — never loses a capture.
queue_capture() {
  local concept="$1" summary="$2" context="$3" source="$4"
  mkdir -p "$(dirname "$QUEUE_FILE")"
  jq -nc --arg concept "$concept" --arg summary "$summary" \
    --arg context "$context" --arg source "$source" \
    --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{concept: $concept, summary: $summary, context: $context, source: $source, ts: $ts}' \
    >>"$QUEUE_FILE"
}

# capture <concept> <summary> <context> <source>
# Dedupes on Concept: updates Context on an existing note, else creates one.
# The dedupe query field is escaped (see escape_query_value) so unusual
# concept names (spaces, parens, quotes, * / _) match correctly instead of
# breaking the query or silently matching the wrong note. Never errors the
# caller's session — falls back to the offline queue if Anki is unreachable
# or if any AnkiConnect call fails partway through (genuine unreachability
# or a mid-flow error), so a capture is never silently dropped.
capture() {
  local concept="$1" summary="$2" context="$3" source="$4"

  if ! ensure_available; then
    queue_capture "$concept" "$summary" "$context" "$source"
    echo "anki.sh: Anki unreachable, queued to $QUEUE_FILE" >&2
    return 0
  fi

  if ! _capture_live "$concept" "$summary" "$context" "$source"; then
    queue_capture "$concept" "$summary" "$context" "$source"
    echo "anki.sh: capture failed, queued to $QUEUE_FILE" >&2
    return 0
  fi
}

# _capture_live <concept> <summary> <context> <source>
# The AnkiConnect calls for a capture, assuming Anki is reachable. Returns
# non-zero on any AnkiConnect failure so the caller can queue instead.
_capture_live() {
  local concept="$1" summary="$2" context="$3" source="$4"

  ensure_deck_model || return 1
  flush_queue

  local query note_ids note_id fields
  query="deck:${DECK} Concept:\"$(escape_query_value "$concept")\""
  note_ids="$(anki_request findNotes "$(jq -nc --arg q "$query" '{query: $q}')")" || return 1
  note_id="$(jq -r '.[0] // empty' <<<"$note_ids")"

  if [[ -n "$note_id" ]]; then
    fields="$(jq -nc --arg ctx "$context" '{Context: $ctx}')"
    anki_request updateNoteFields \
      "$(jq -nc --argjson id "$note_id" --argjson fields "$fields" \
        '{note: {id: $id, fields: $fields}}')" >/dev/null || return 1
    echo "anki.sh: updated existing note for \"$concept\" in $DECK"
  else
    local note
    note="$(jq -nc --arg deck "$DECK" --arg model "$MODEL" \
      --arg concept "$concept" --arg summary "$summary" \
      --arg context "$context" --arg source "$source" \
      '{note: {
        deckName: $deck,
        modelName: $model,
        fields: {Concept: $concept, Summary: $summary, Context: $context, Source: $source},
        options: {allowDuplicate: false}
      }}')"
    anki_request addNote "$note" >/dev/null || return 1
    echo "anki.sh: captured \"$concept\" to $DECK"
  fi
}

# flush_queue
# If Anki is reachable and the queue file is non-empty, replays each queued
# row through capture, then truncates the queue. Safe to call whenever Anki
# is known reachable (e.g. at the start of capture). The queue is truncated
# up front and each row is individually re-queued by capture() if its own
# replay fails, so a per-row AnkiConnect failure keeps that row queued
# rather than losing it (not a guarantee against the process being killed
# mid-replay, e.g. via SIGKILL). A row that fails to parse as JSON (a torn
# line) is re-queued as-is rather than letting the parse failure raise under
# set -e — an abort after truncation but before replay would otherwise
# orphan the rest of the queue in the tmp copy. Re-entrancy-guarded: capture()
# during replay may itself call flush_queue (via _capture_live), which would
# otherwise re-replay rows already re-queued earlier in this same pass.
_flush_in_progress=0
flush_queue() {
  [[ "$_flush_in_progress" == "1" ]] && return 0
  [[ -s "$QUEUE_FILE" ]] || return 0

  if ! anki_request version >/dev/null 2>&1; then
    return 0
  fi

  ensure_deck_model

  local tmp
  tmp="$(mktemp)"
  cp "$QUEUE_FILE" "$tmp"
  : >"$QUEUE_FILE"

  _flush_in_progress=1
  local line concept summary context source
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    if ! jq -e . <<<"$line" >/dev/null 2>&1; then
      echo "anki.sh: unparseable queue row, re-queued as-is: $line" >&2
      printf '%s\n' "$line" >>"$QUEUE_FILE"
      continue
    fi
    concept="$(jq -r '.concept' <<<"$line")"
    summary="$(jq -r '.summary' <<<"$line")"
    context="$(jq -r '.context' <<<"$line")"
    source="$(jq -r '.source' <<<"$line")"
    capture "$concept" "$summary" "$context" "$source"
  done <"$tmp"
  _flush_in_progress=0

  rm -f "$tmp"
}

# concepts
# Read-only: lists all Concept-field values of every note in the Concepts
# deck, as a JSON array. Used by the /learned semantic-dedupe subagent to
# compare a proposed concept name against what already exists, without that
# subagent needing to know any AnkiConnect call shapes itself.
concepts() {
  ensure_available || { echo "anki.sh: Anki unreachable, cannot list concepts" >&2; return 1; }
  flush_queue
  local note_ids info
  note_ids="$(anki_request findNotes "$(jq -nc --arg q "deck:${DECK}" '{query: $q}')")" || {
    echo "anki.sh: findNotes failed" >&2
    return 1
  }
  info="$(anki_request notesInfo "$(jq -nc --argjson n "$note_ids" '{notes: $n}')")" || {
    echo "anki.sh: notesInfo failed" >&2
    return 1
  }
  jq -c '[.[].fields.Concept.value]' <<<"$info"
}

# due
# Lists due/new cards in the Concepts deck. Deliberately queries
# "(is:due OR is:new)" — plain is:due excludes never-reviewed cards, which
# would hide freshly captured concepts from /drill.
due() {
  ensure_available || { echo "anki.sh: Anki unreachable, cannot list due cards" >&2; return 1; }
  flush_queue
  anki_request findCards "$(jq -nc --arg q "deck:${DECK} (is:due OR is:new)" '{query: $q}')"
}

# info <cardId> [cardId ...]
# Prints cardsInfo for the given card ids.
info() {
  [[ $# -ge 1 ]] || { echo "anki.sh: info requires at least one cardId" >&2; return 1; }
  ensure_available || { echo "anki.sh: Anki unreachable, cannot fetch card info" >&2; return 1; }
  local ids
  ids="$(printf '%s\n' "$@" | jq -R 'tonumber' | jq -s '.')"
  anki_request cardsInfo "$(jq -nc --argjson cards "$ids" '{cards: $cards}')"
}

# answer <cardId> <ease>
# Writes a review grade back to Anki via answerCards. Ease: 1=Again, 2=Hard,
# 3=Good, 4=Easy. AnkiConnect reports a bad/unanswerable card id as
# error:null with result:[false], not as a request error, so anki_request's
# error check alone would miss it — assert the result explicitly.
answer() {
  [[ $# -eq 2 ]] || { echo "anki.sh: answer requires <cardId> <ease>" >&2; return 1; }
  local card_id="$1" ease="$2"
  ensure_available || { echo "anki.sh: Anki unreachable, cannot record answer" >&2; return 1; }
  flush_queue
  local result
  result="$(anki_request answerCards "$(jq -nc --argjson id "$card_id" --argjson ease "$ease" \
    '{answers: [{cardId: $id, ease: $ease}]}')")" || { echo "anki.sh: answerCards request failed" >&2; return 1; }
  if ! jq -e '.[0] == true' <<<"$result" >/dev/null 2>&1; then
    echo "anki.sh: answerCards did not confirm the grade for card $card_id (result: $result)" >&2
    return 1
  fi
  printf '%s\n' "$result"
}

version() {
  anki_request version
}

# flush (CLI command)
# Unlike flush_queue (the internal helper other commands call once already
# known-reachable), this is the only write-path CLI entry that skipped
# ensure_available — with Anki closed it silently exited 0 with no output
# and never attempted to launch Anki or flush anything.
flush_cmd() {
  ensure_available || { echo "anki.sh: Anki unreachable, queue left as-is (still queued)" >&2; return 1; }
  flush_queue
}

main() {
  local cmd="${1:-}"
  [[ $# -gt 0 ]] && shift || true

  case "$cmd" in
    capture)
      [[ $# -eq 4 ]] || { echo "usage: anki.sh capture <concept> <summary> <context> <source>" >&2; exit 1; }
      capture "$1" "$2" "$3" "$4"
      ;;
    concepts) concepts ;;
    due) due ;;
    info) info "$@" ;;
    answer) answer "$@" ;;
    flush) flush_cmd ;;
    version) version ;;
    *)
      echo "usage: anki.sh <capture|concepts|due|info|answer|flush|version> ..." >&2
      exit 1
      ;;
  esac
}

main "$@"
