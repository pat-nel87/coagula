#!/usr/bin/env bash
# GitHub Copilot CLI postToolUse universal interceptor.
#
# Three responsibilities:
#
# 1. Funnel large tool outputs through coagula when they exceed per-tool
#    or cumulative thresholds (the core feature since v0.3).
# 2. Nudge model away from paginated `view_range` patterns toward bulk
#    bash commands that funnel automatically (v0.7+).
# 3. Cache + inject a coagula summary of viewed files when the file
#    content is dedup-friendly, so the model can stop paginating early
#    (v0.7+).
#
# Copilot CLI is currently the only major host whose postToolUse supports
# `modifiedResult`, which makes true universal interception possible —
# the reason coagula's v0.5+ line is Copilot-CLI-specific.
#
# Wire up with ~/.copilot/hooks/coagula.json — see install.sh.
#
# Env knobs (all optional):
#   COAGULA_QUERY                query string passed to coagula; if unset,
#                                derived from $COAGULA_TASK or empty (lite).
#   COAGULA_BUDGET               token budget for cleaned output (default 2000).
#   COAGULA_KEEP                 top-K kept by relevance (default 5).
#   COAGULA_THRESHOLD            override per-tool defaults with one global
#                                token threshold. Default: per-tool table
#                                (bash 2000, view/read 500, MCP 1000).
#   COAGULA_CUMULATIVE_THRESHOLD once a session's total tool-output tokens
#                                pass this, start funneling even sub-threshold
#                                calls. Default 8000. 0 disables.
#   COAGULA_VIEW_NUDGE_AFTER     fire the multi-view nudge after N view-like
#                                tool calls per session. Default 4. 0 disables.
#   COAGULA_SUMMARY_INJECT       set to "off" to disable file-summary
#                                injection on view tool calls. Default on.
#   COAGULA_SKIP_TOOLS           extra comma-separated tool names to bypass.
#   COAGULA_DISABLE              set to "1" to bypass the hook entirely.
#   COAGULA_DEBUG_LOG            path for the decision log; "off" disables.

# NOTE: deliberately NOT using `set -e`. The hook does many jq/file-state
# operations that can fail in interesting ways (concurrent writes, weird
# paths, jq parse hiccups on unexpected payloads). With -e, ANY non-zero
# exit propagates to Copilot CLI as a HookExitCodeError, which loses
# all the careful `|| true` defensive handling sprinkled below. -u and
# -o pipefail still catch unset-var bugs and pipeline failures.
set -uo pipefail

# Defensive backstop: if we somehow exit with non-zero anyway (signal,
# arithmetic-on-empty, etc.), emit a clean passthrough so Copilot never
# sees HookExitCodeError. The decision log captures the failure for
# post-hoc debugging.
_emit_clean_failure() {
  local rc=$?
  if (( rc != 0 )); then
    # _log may not be defined yet on early failures; ignore failures here.
    type _log >/dev/null 2>&1 && _log "unexpected-error (exit=$rc) — passing through unchanged"
    echo '{}'
    exit 0
  fi
}
trap _emit_clean_failure EXIT

input=$(cat)

# ---------------------------------------------------------------------------
# Logging — every decision exit goes through _log so firing rate is observable.
# ---------------------------------------------------------------------------
log_path="${COAGULA_DEBUG_LOG:-$HOME/.copilot/coagula-debug.log}"

_log() {
  case "$log_path" in
    off|OFF|disabled|DISABLED|0) return 0 ;;
  esac
  local ts
  ts=$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
  printf '%s [post-tool] %s\n' "$ts" "$*" >>"$log_path" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Per-tool threshold table. COAGULA_THRESHOLD env var overrides everything.
# ---------------------------------------------------------------------------
_threshold_for_tool() {
  if [[ -n "${COAGULA_THRESHOLD:-}" ]]; then
    echo "$COAGULA_THRESHOLD"
    return
  fi
  case "$1" in
    bash|shell|powershell|read_powershell) echo 2000 ;;
    view|read|read_file|str_replace_based_edit_tool) echo 500 ;;
    mcp:*|*__*)                            echo 1000 ;;
    *)                                     echo 1500 ;;
  esac
}

_is_view_tool() {
  case "$1" in
    view|read|read_file|str_replace_based_edit_tool) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Session state — single JSON file per PPID with all per-session fields.
# Fields: updated_at, total_tokens, view_count, nudge_sent,
#         summaries: {<filepath>: <coagula-summary-text>}
# ---------------------------------------------------------------------------
session_dir="$HOME/.copilot/coagula-session-state"
session_file="${session_dir}/${PPID}.json"
cumulative_threshold="${COAGULA_CUMULATIVE_THRESHOLD:-8000}"
view_nudge_after="${COAGULA_VIEW_NUDGE_AFTER:-4}"
summary_inject="${COAGULA_SUMMARY_INJECT:-on}"

# Generic read: load a field from state. Returns the default if the file
# is missing, stale (>1h), or the field is null.
_state_get() {
  local jq_path="$1" default="${2:-}"
  if [[ ! -f "$session_file" ]]; then
    printf '%s' "$default"
    return
  fi
  local now
  now=$(date +%s)
  local val
  val=$(jq -r --argjson now "$now" "
    if ((.updated_at // 0) | tonumber) < (\$now - 3600)
    then \"\" else (${jq_path} // \"\")
    end" "$session_file" 2>/dev/null) || val=""
  if [[ -z "$val" ]]; then
    printf '%s' "$default"
  else
    printf '%s' "$val"
  fi
}

# Generic update: apply a jq expression to the (possibly empty) state
# object and write it atomically. The expression operates on `.` which
# is the current state object (or {} if fresh / stale).
_state_update() {
  local jq_expr="$1" now tmp current
  mkdir -p "$session_dir" 2>/dev/null || true
  now=$(date +%s)
  if [[ -f "$session_file" ]]; then
    current=$(jq --argjson now "$now" \
      'if ((.updated_at // 0) | tonumber) < ($now - 3600) then {} else . end' \
      "$session_file" 2>/dev/null) || current="{}"
  else
    current="{}"
  fi
  tmp="${session_file}.tmp.$$"
  if printf '%s' "$current" \
       | jq --argjson now "$now" "${jq_expr} | .updated_at = \$now" >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$session_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
  fi
}

_load_session_total() { _state_get '.total_tokens' '0'; }

_bump_session_total() {
  local add="$1"
  _state_update ".total_tokens = ((.total_tokens // 0) + ${add})"
  _load_session_total
}

_load_view_count() { _state_get '.view_count' '0'; }

_bump_view_count() {
  _state_update ".view_count = ((.view_count // 0) + 1)"
  _load_view_count
}

_nudge_already_sent() {
  [[ "$(_state_get '.nudge_sent' '')" == "true" ]]
}

_mark_nudge_sent() {
  _state_update '.nudge_sent = true'
}

# File summary cache: returns cached summary text for a path, or empty.
# Lookup via --arg to avoid path characters breaking the jq query.
_get_cached_summary() {
  local path="$1"
  [[ -f "$session_file" ]] || { echo ""; return; }
  local now
  now=$(date +%s)
  local val
  val=$(jq -r --argjson now "$now" --arg path "$path" '
    if ((.updated_at // 0) | tonumber) < ($now - 3600)
    then "" else (.summaries[$path] // "")
    end' "$session_file" 2>/dev/null) || val=""
  printf '%s' "$val"
}

# Atomic write of summary cache entry. --arg path/summary keeps the jq
# query free of shell-injection / quoting hazards from file paths.
_set_cached_summary() {
  local path="$1" summary="$2" now tmp current
  mkdir -p "$session_dir" 2>/dev/null || true
  now=$(date +%s)
  if [[ -f "$session_file" ]]; then
    current=$(jq --argjson now "$now" \
      'if ((.updated_at // 0) | tonumber) < ($now - 3600) then {} else . end' \
      "$session_file" 2>/dev/null) || current="{}"
  else
    current="{}"
  fi
  tmp="${session_file}.tmp.$$"
  if printf '%s' "$current" \
       | jq --argjson now "$now" --arg path "$path" --arg summary "$summary" '
         .summaries = (.summaries // {})
         | .summaries[$path] = $summary
         | .updated_at = $now' >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$session_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  else
    rm -f "$tmp" 2>/dev/null
  fi
}

# Best-effort cleanup of stale session files (~2% of hook fires).
if (( RANDOM % 50 == 0 )) && [[ -d "$session_dir" ]]; then
  find "$session_dir" -name '*.json' -mmin +60 -delete 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Defensive file-path extraction for view-like tools.
# Copilot CLI's view tool's toolArgs schema isn't fully documented; try
# common field names. Returns empty if none match.
# ---------------------------------------------------------------------------
_extract_file_path() {
  # Copilot CLI passes toolArgs as a JSON STRING for some tools (view,
  # bash) and as an object for others. Handle both shapes, then try
  # common field names since the schema isn't fully documented.
  printf '%s' "$input" | jq -r '
    (if (.toolArgs | type) == "string"
     then (.toolArgs | fromjson? // {})
     else (.toolArgs // {})
     end)
    | (.path // .filename // .file // .target_file // .target // empty)
  ' 2>/dev/null
}

# Try to compute a coagula summary of the given file path. Returns:
# - the summary text if compression > 50% saved
# - empty if file unreadable, not enough savings, or coagula failed
_try_compute_summary() {
  local path="$1"
  [[ -r "$path" ]] || { echo ""; return; }
  # Bound file size — 1MB cap to avoid runaway costs.
  local file_size
  file_size=$(wc -c <"$path" 2>/dev/null | tr -d ' ')
  if [[ -z "$file_size" ]] || (( file_size > 1048576 )); then
    echo ""; return
  fi
  # Require the file to be at least 1500 chars (~375 tokens) — small files
  # don't need a summary; just let view return them directly.
  if (( file_size < 1500 )); then
    echo ""; return
  fi
  # Tight budget so the summary stays cheap to inject.
  local summary
  summary=$(coagula --profile passthrough --budget 200 --keep 3 <"$path" 2>/dev/null || true)
  [[ -z "$summary" ]] && { echo ""; return; }

  # Two-condition dedup test:
  #   (a) absolute: summary < 500 chars (~125 tokens — small enough to inject)
  #   (b) relative: summary < 10% of original (rules out budget-stage truncation
  #       masquerading as dedup — a unique-content file truncated to fit the
  #       budget would pass condition (a) but fail (b))
  local summary_size=${#summary}
  if (( summary_size >= 500 )); then
    echo ""; return
  fi
  if (( summary_size * 10 >= file_size )); then
    echo ""; return
  fi
  printf '%s' "$summary"
}

# ---------------------------------------------------------------------------
# Early-exit guards
# ---------------------------------------------------------------------------
if [[ "${COAGULA_DISABLE:-0}" == "1" ]]; then
  _log "disabled (COAGULA_DISABLE=1)"
  echo '{}'; exit 0
fi
if ! command -v coagula >/dev/null 2>&1; then
  _log "coagula-missing (not on PATH — hooks are no-ops, install coagula globally to fix)"
  echo '{}'; exit 0
fi
if ! command -v jq >/dev/null 2>&1; then
  echo '{}'; exit 0
fi

tool_name=$(printf '%s' "$input" | jq -r '.toolName // empty')
result_type=$(printf '%s' "$input" | jq -r '.toolResult.resultType // empty')
result_text=$(printf '%s' "$input" | jq -r '.toolResult.textResultForLlm // empty')


# Silent passthrough for non-tool / unsuccessful events.
if [[ -z "$tool_name" || -z "$result_text" || "$result_type" != "success" ]]; then
  echo '{}'; exit 0
fi

# Always-on skip list.
case "$tool_name" in
  report_intent|sql|todo_*|notification)
    _log "skipped (tool=$tool_name reason=internal-bookkeeping)"
    echo '{}'; exit 0 ;;
esac

# User-configured extra skips.
if [[ -n "${COAGULA_SKIP_TOOLS:-}" ]]; then
  IFS=',' read -ra extra_skips <<<"$COAGULA_SKIP_TOOLS"
  for s in "${extra_skips[@]}"; do
    if [[ "$tool_name" == "${s// /}" ]]; then
      _log "skipped (tool=$tool_name reason=user-skip-list)"
      echo '{}'; exit 0
    fi
  done
fi

result_chars=${#result_text}
result_tokens=$(( result_chars / 4 ))
per_call_threshold=$(_threshold_for_tool "$tool_name")

# Idempotency: don't refunnel coagula's own output.
if printf '%s' "$result_text" | head -c 200 | grep -q '^### \|^\[coagula'; then
  _log "skipped (tool=$tool_name reason=already-funneled)"
  echo '{}'; exit 0
fi

# ---------------------------------------------------------------------------
# Pre-compute injection text for view-like tools.
# Two independent injections, both prepended to the response:
#   - Multi-view nudge: ONE-TIME per session after N view calls
#   - File summary: cached per (session, file) when content compresses well
# ---------------------------------------------------------------------------
nudge_text=""
summary_text=""

if _is_view_tool "$tool_name"; then
  view_count=$(_bump_view_count)

  # A.1: multi-view nudge
  if (( view_nudge_after > 0 )) && (( view_count >= view_nudge_after )) && ! _nudge_already_sent; then
    nudge_text="[coagula tip: ${view_count} small reads this session. For bulk pattern analysis, prefer bash like \`grep -A 5 PATTERN file\` or \`cat file | head -N\` — bash outputs auto-funnel via coagula (often 90%+ savings on logs).]"
    _mark_nudge_sent
    _log "nudge-injected (tool=$tool_name view_count=$view_count)"
  fi

  # B.3: cached file summary — inject ONLY on the FIRST view of each file
  # per session. v0.7.2 injected on every call which added ~180 tokens of
  # overhead per view; on 20-view paginated workloads that was 3.6k extra
  # tokens with no behavior change (counterbalanced n=4 test showed +15%
  # credits). Injecting once gives the model the pattern up-front; it
  # either heeds it (savings) or ignores it (~one-time cost).
  if [[ "$summary_inject" != "off" ]]; then
    file_path=$(_extract_file_path)
    if [[ -n "$file_path" ]]; then
      cached=$(_get_cached_summary "$file_path")
      if [[ -n "$cached" ]]; then
        # Already injected once — passthrough, no re-injection.
        _log "summary-already-shown (tool=$tool_name file=$file_path)"
      else
        # First view of this file — try to compute + inject.
        computed=$(_try_compute_summary "$file_path")
        if [[ -n "$computed" ]]; then
          _set_cached_summary "$file_path" "$computed"
          summary_text="[coagula summary of ${file_path} (file is dedup-able; consider asking for the pattern rather than line-by-line reads):
${computed}
--- requested slice below ---]"
          _log "summary-computed (tool=$tool_name file=$file_path summary_chars=${#computed})"
        else
          # Mark as seen-but-not-summarizable so we don't recompute every call.
          _set_cached_summary "$file_path" "(not dedupable)"
          _log "summary-skipped (tool=$tool_name file=$file_path reason=not-dedupable-or-too-large)"
        fi
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Threshold decision: per-call OR cumulative-trigger.
# ---------------------------------------------------------------------------
session_total=$(_load_session_total)
trigger_reason=""

if (( result_tokens >= per_call_threshold )); then
  trigger_reason="per-call(${result_tokens}>=${per_call_threshold})"
elif (( cumulative_threshold > 0 )) && (( session_total + result_tokens >= cumulative_threshold )); then
  trigger_reason="cumulative(${session_total}+${result_tokens}>=${cumulative_threshold})"
else
  # Under both thresholds — track bytes-sent. If we have nudge or summary
  # to inject, return modifiedResult with injection + original content.
  new_total=$(_bump_session_total "$result_tokens")
  if [[ -n "$nudge_text" || -n "$summary_text" ]]; then
    injection=""
    [[ -n "$nudge_text" ]] && injection="${nudge_text}"
    [[ -n "$summary_text" ]] && injection="${injection}${injection:+

}${summary_text}"
    final="${injection}

${result_text}"
    _log "injection-only (tool=$tool_name nudge=$([[ -n "$nudge_text" ]] && echo 1 || echo 0) summary=$([[ -n "$summary_text" ]] && echo 1 || echo 0) extra_chars=$((${#final} - result_chars)))"
    jq -nc --arg text "$final" \
      '{modifiedResult: {resultType: "success", textResultForLlm: $text}}'
    exit 0
  fi
  _log "under-threshold (tool=$tool_name tokens=$result_tokens per_call=$per_call_threshold cumulative=${new_total}/${cumulative_threshold})"
  echo '{}'; exit 0
fi

# ---------------------------------------------------------------------------
# Run the funnel
# ---------------------------------------------------------------------------
query="${COAGULA_QUERY:-${COAGULA_TASK:-}}"

profile="passthrough"
case "$tool_name" in
  bash|shell|powershell)
    command_str=$(printf '%s' "$input" \
      | jq -r 'if (.toolArgs | type) == "string"
               then (.toolArgs | fromjson | .command // "")
               else .toolArgs.command // ""
               end')
    case "$command_str" in
      kubectl*|oc*|helm*)        profile="k8s" ;;
      psql*|mysql*|sqlite3*)     profile="postgres" ;;
      az*|gcloud*|aws*)          profile="azure" ;;
    esac
    ;;
esac

budget="${COAGULA_BUDGET:-2000}"
keep="${COAGULA_KEEP:-5}"

if [[ -n "$query" ]]; then
  cleaned=$(printf '%s' "$result_text" \
    | coagula --query "$query" --profile "$profile" --budget "$budget" --keep "$keep" 2>/dev/null \
    || true)
else
  cleaned=$(printf '%s' "$result_text" \
    | coagula --profile "$profile" --budget "$budget" --keep "$keep" 2>/dev/null \
    || true)
fi

if [[ -z "$cleaned" ]]; then
  _log "funnel-empty (tool=$tool_name) — passing through unchanged"
  echo '{}'; exit 0
fi

cleaned_chars=${#cleaned}
if (( cleaned_chars >= result_chars )); then
  _log "funnel-noop (tool=$tool_name reason=no-shrink in=${result_chars}c out=${cleaned_chars}c)"
  echo '{}'; exit 0
fi

cleaned_tokens=$(( cleaned_chars / 4 ))
new_total=$(_bump_session_total "$cleaned_tokens")

# Build final response, prepending any nudge/summary injections.
injection_combined=""
[[ -n "$nudge_text" ]] && injection_combined="${nudge_text}"
[[ -n "$summary_text" ]] && injection_combined="${injection_combined}${injection_combined:+

}${summary_text}"

if [[ -n "$injection_combined" ]]; then
  final="${injection_combined}

[coagula: ${result_tokens} → ${cleaned_tokens} tok | tool=${tool_name} profile=${profile}]
${cleaned}"
else
  final="[coagula: ${result_tokens} → ${cleaned_tokens} tok | tool=${tool_name} profile=${profile}]
${cleaned}"
fi

_log "fired (tool=$tool_name profile=$profile in=${result_tokens} out=${cleaned_tokens} reason=${trigger_reason} cumulative=${new_total})"

jq -nc \
  --arg text "$final" \
  '{
    modifiedResult: {
      resultType: "success",
      textResultForLlm: $text
    }
  }'
