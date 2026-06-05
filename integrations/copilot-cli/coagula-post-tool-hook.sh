#!/usr/bin/env bash
# GitHub Copilot CLI postToolUse universal interceptor.
#
# Catches the result of any tool call. If the result exceeds the per-tool
# threshold (or the session cumulative threshold), runs it through coagula
# and returns the cleaned version via `modifiedResult` so the model only
# sees the funneled version.
#
# Copilot CLI is currently the only major host whose postToolUse supports
# `modifiedResult`, which makes true universal interception possible — the
# reason coagula's v0.5+ line is Copilot-CLI-specific.
#
# Wire it up with ~/.copilot/hooks/coagula.json — see install.sh.
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
#                                calls (catches paginated view-range patterns).
#                                Default: 8000. Set to 0 to disable.
#   COAGULA_SKIP_TOOLS           extra comma-separated tool names to bypass.
#                                Always-skipped: report_intent, sql, todo_*,
#                                notification.
#   COAGULA_DISABLE              set to "1" to bypass the hook entirely.
#   COAGULA_DEBUG_LOG            path for the decision log; "off" disables.
#                                Default: $HOME/.copilot/coagula-debug.log.

set -euo pipefail

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
# Per-tool threshold table. The single-global COAGULA_THRESHOLD env var
# overrides everything; otherwise pick based on tool name.
#
# Defaults are tuned to firing-rate, not just compression efficiency:
# - bash/shell/powershell stay at 2000 — most small bash outputs are
#   genuinely small (file existence checks, version probes, ls).
# - view/read/read_file drop to 500 — paginated view_range chunks are
#   typically 5-15KB of repetitive content that dedups 60-80%.
# - MCP tools (heuristic: name contains "mcp:" or "__") at 1000 — middle
#   ground; MCP returns are often structured JSON that prunes well.
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

# ---------------------------------------------------------------------------
# Session state — per-PPID cumulative token counter so we can funnel
# paginated reads whose individual calls slip under the per-call threshold.
#
# State lives in ~/.copilot/coagula-session-state/<PPID>.json. Keyed by the
# parent process (i.e. the copilot CLI process), so each interactive
# session gets its own counter. TTL: entries older than 1h are pruned.
# Atomic writes via tmp+mv to avoid torn reads on concurrent hook fires.
# ---------------------------------------------------------------------------
session_dir="$HOME/.copilot/coagula-session-state"
session_file="${session_dir}/${PPID}.json"
cumulative_threshold="${COAGULA_CUMULATIVE_THRESHOLD:-8000}"

_load_session_total() {
  [[ -f "$session_file" ]] || { echo 0; return; }
  local now total
  now=$(date +%s)
  total=$(jq -r --argjson now "$now" '
    if ((.updated_at // 0) | tonumber) < ($now - 3600)
    then 0 else (.total_tokens // 0)
    end' "$session_file" 2>/dev/null) || total=0
  echo "${total:-0}"
}

_bump_session_total() {
  local add="$1" now prev total tmp
  mkdir -p "$session_dir" 2>/dev/null || true
  now=$(date +%s)
  prev=$(_load_session_total)
  total=$(( prev + add ))
  tmp="${session_file}.tmp.$$"
  if jq -nc --argjson now "$now" --argjson total "$total" \
       '{updated_at: $now, total_tokens: $total}' >"$tmp" 2>/dev/null; then
    mv -f "$tmp" "$session_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null
  fi
  echo "$total"
}

# Best-effort cleanup of stale session files (~2% of hook fires).
if (( RANDOM % 50 == 0 )) && [[ -d "$session_dir" ]]; then
  find "$session_dir" -name '*.json' -mmin +60 -delete 2>/dev/null || true
fi

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
  # Don't log — jq is needed to log anyway.
  echo '{}'; exit 0
fi

tool_name=$(printf '%s' "$input" | jq -r '.toolName // empty')
result_type=$(printf '%s' "$input" | jq -r '.toolResult.resultType // empty')
result_text=$(printf '%s' "$input" | jq -r '.toolResult.textResultForLlm // empty')

# Silent passthrough for non-tool / unsuccessful events — too noisy to log.
if [[ -z "$tool_name" || -z "$result_text" || "$result_type" != "success" ]]; then
  echo '{}'; exit 0
fi

# Always-on skip list (internal bookkeeping tools — outputs are tiny + structured).
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
if printf '%s' "$result_text" | head -c 200 | grep -q '^### '; then
  _log "skipped (tool=$tool_name reason=already-funneled)"
  echo '{}'; exit 0
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
  # Under both thresholds — track the bytes-sent but don't transform.
  new_total=$(_bump_session_total "$result_tokens")
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
  _log "funnel-empty (tool=$tool_name) — falling through unchanged"
  echo '{}'; exit 0
fi

cleaned_chars=${#cleaned}
if (( cleaned_chars >= result_chars )); then
  _log "funnel-noop (tool=$tool_name reason=no-shrink in=${result_chars}c out=${cleaned_chars}c)"
  echo '{}'; exit 0
fi

cleaned_tokens=$(( cleaned_chars / 4 ))
new_total=$(_bump_session_total "$cleaned_tokens")

final="[coagula: ${result_tokens} → ${cleaned_tokens} tok | tool=${tool_name} profile=${profile}]
${cleaned}"

_log "fired (tool=$tool_name profile=$profile in=${result_tokens} out=${cleaned_tokens} reason=${trigger_reason} cumulative=${new_total})"

jq -nc \
  --arg text "$final" \
  '{
    modifiedResult: {
      resultType: "success",
      textResultForLlm: $text
    }
  }'
