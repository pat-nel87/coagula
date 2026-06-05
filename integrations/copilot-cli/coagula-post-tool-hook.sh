#!/usr/bin/env bash
# GitHub Copilot CLI postToolUse universal interceptor.
#
# Catches the result of ANY tool call (bash, view, MCP tools, etc.). If the
# textResultForLlm exceeds COAGULA_THRESHOLD tokens, runs it through coagula
# and returns the cleaned version via `modifiedResult` — so the model never
# sees the noisy original.
#
# Copilot CLI is currently the only major host whose postToolUse supports
# `modifiedResult`, which makes true universal interception possible — the
# reason coagula's v0.5.0 line is Copilot-CLI-specific.
#
# Wire it up with ~/.copilot/hooks/coagula.json:
#
#   {
#     "version": 1,
#     "hooks": {
#       "postToolUse": [
#         {
#           "type": "command",
#           "bash": "/absolute/path/to/coagula-post-tool-hook.sh"
#         }
#       ]
#     }
#   }
#
# Env knobs (all optional):
#   COAGULA_QUERY           query string passed to coagula; if unset, derived
#                           from $COAGULA_TASK or a generic fallback.
#   COAGULA_BUDGET          token budget for the cleaned output (default 2000).
#   COAGULA_KEEP            top-K kept by relevance (default 5).
#   COAGULA_THRESHOLD       skip interception when the tool result is at most
#                           this many tokens (default 2000).
#   COAGULA_SKIP_TOOLS      extra comma-separated tool names to bypass.
#                           Always-skipped: report_intent, sql.
#   COAGULA_DISABLE         set to "1" to bypass the hook entirely.

set -euo pipefail

# Read stdin payload.
input=$(cat)

# Bail cleanly on disable / missing prerequisites.
[[ "${COAGULA_DISABLE:-0}" == "1" ]] && { echo '{}'; exit 0; }
command -v coagula >/dev/null 2>&1 || { echo '{}'; exit 0; }
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }

tool_name=$(printf '%s' "$input" | jq -r '.toolName // empty')
result_type=$(printf '%s' "$input" | jq -r '.toolResult.resultType // empty')
result_text=$(printf '%s' "$input" | jq -r '.toolResult.textResultForLlm // empty')

# Only intercept successful tool calls with actual text output.
[[ -z "$tool_name" || -z "$result_text" || "$result_type" != "success" ]] && { echo '{}'; exit 0; }

# Skip internal bookkeeping tools that produce tiny, structured output we
# can't meaningfully compress.
case "$tool_name" in
  report_intent|sql|todo_*|notification) echo '{}'; exit 0 ;;
esac

# User-configurable extra skips.
if [[ -n "${COAGULA_SKIP_TOOLS:-}" ]]; then
  IFS=',' read -ra extra_skips <<<"$COAGULA_SKIP_TOOLS"
  for s in "${extra_skips[@]}"; do
    [[ "$tool_name" == "${s// /}" ]] && { echo '{}'; exit 0; }
  done
fi

# Quick token estimate (char // 4 — matches coagula.tokens fallback).
threshold="${COAGULA_THRESHOLD:-2000}"
result_chars=${#result_text}
result_tokens=$(( result_chars / 4 ))
(( result_tokens < threshold )) && { echo '{}'; exit 0; }

# Don't intercept if the text already looks like coagula's own output
# (idempotency guard).
if printf '%s' "$result_text" | head -c 200 | grep -q '^### '; then
  echo '{}'; exit 0
fi

# Derive query — empty means lite mode (no Relevance/Summarize).
query="${COAGULA_QUERY:-${COAGULA_TASK:-}}"

# Auto-detect profile from the tool / command. Match the pre-tool
# hook's shell-family allowlist so Windows kubectl/az/psql output gets
# the right denylist instead of falling back to passthrough.
profile="passthrough"
case "$tool_name" in
  bash|shell|powershell)
    # toolArgs may be either a JSON string or an object. Normalize.
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

# Funnel the text. If coagula errors, fall through to no-op (keep original).
# Omit --query when empty so the CLI runs in lite mode and doesn't
# collapse output against a meaningless query string.
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
  echo '{}'; exit 0
fi

# Skip the swap if the cleaned version isn't actually smaller.
cleaned_chars=${#cleaned}
(( cleaned_chars >= result_chars )) && { echo '{}'; exit 0; }

cleaned_tokens=$(( cleaned_chars / 4 ))

# Prefix a tiny note so the model knows the transformation happened.
final="[coagula: ${result_tokens} → ${cleaned_tokens} tok | tool=${tool_name} profile=${profile}]
${cleaned}"

# Debug log — append-only, transform-only entries. Disable with
# COAGULA_DEBUG_LOG=off, override path with COAGULA_DEBUG_LOG=<path>.
log_path="${COAGULA_DEBUG_LOG:-$HOME/.copilot/coagula-debug.log}"
case "$log_path" in
  off|OFF|disabled|DISABLED|0) : ;;
  *)
    ts=$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s [post-tool] funneled (tool=%s profile=%s): %s -> %s tok\n' \
      "$ts" "$tool_name" "$profile" "$result_tokens" "$cleaned_tokens" \
      >>"$log_path" 2>/dev/null || true
    ;;
esac

# Return modifiedResult to replace what the model sees.
jq -nc \
  --arg text "$final" \
  '{
    modifiedResult: {
      resultType: "success",
      textResultForLlm: $text
    }
  }'
