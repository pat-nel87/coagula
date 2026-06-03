#!/usr/bin/env bash
# GitHub Copilot CLI postToolUse universal interceptor.
#
# Catches the result of ANY tool call (bash, view, MCP tools, etc.). If the
# textResultForLlm exceeds COAGULA_THRESHOLD tokens, runs it through coagula
# and returns the cleaned version via `modifiedResult` — so the model never
# sees the noisy original.
#
# This is *strictly more powerful* than the equivalent in Claude Code, whose
# PostToolUse is read-only. Only Copilot CLI's postToolUse supports
# `modifiedResult`, which makes true universal interception possible.
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

# Derive query.
query="${COAGULA_QUERY:-${COAGULA_TASK:-general diagnostic query}}"

# Auto-detect profile from the tool / command.
profile="passthrough"
if [[ "$tool_name" == "bash" ]]; then
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
fi

budget="${COAGULA_BUDGET:-2000}"
keep="${COAGULA_KEEP:-5}"

# Funnel the text. If coagula errors, fall through to no-op (keep original).
cleaned=$(printf '%s' "$result_text" \
  | coagula --query "$query" --profile "$profile" --budget "$budget" --keep "$keep" 2>/dev/null \
  || true)

if [[ -z "$cleaned" ]]; then
  echo '{}'; exit 0
fi

# Skip the swap if the cleaned version isn't actually smaller.
cleaned_chars=${#cleaned}
(( cleaned_chars >= result_chars )) && { echo '{}'; exit 0; }

# Prefix a tiny note so the model knows the transformation happened.
final="[coagula: ${result_tokens} → $(( cleaned_chars / 4 )) tok | tool=${tool_name} profile=${profile}]
${cleaned}"

# Return modifiedResult to replace what the model sees.
jq -nc \
  --arg text "$final" \
  '{
    modifiedResult: {
      resultType: "success",
      textResultForLlm: $text
    }
  }'
