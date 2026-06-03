#!/usr/bin/env bash
# GitHub Copilot CLI preToolUse hook (Bash-only).
#
# Mirror of the Claude Code preToolUse hook, adapted to Copilot CLI's payload
# shape (camelCase fields, `toolArgs` is a JSON string in preToolUse, output
# uses `modifiedArgs`).
#
# Use this as a belt-and-suspenders companion to coagula-post-tool-hook.sh:
# - preToolUse rewrites noisy commands so they pipe through coagula *before*
#   they run. Tight feedback loop, smaller pipe-through cost.
# - postToolUse catches anything large that slipped through (e.g., a
#   misclassified noisy command, or a `view` of a huge file).
#
# Wire it up in ~/.copilot/hooks/coagula.json:
#
#   {
#     "version": 1,
#     "hooks": {
#       "preToolUse": [
#         {
#           "type": "command",
#           "bash": "/absolute/path/to/coagula-pre-bash-hook.sh"
#         }
#       ]
#     }
#   }
#
# Env knobs match coagula-post-tool-hook.sh: COAGULA_QUERY, COAGULA_BUDGET,
# COAGULA_KEEP, COAGULA_NOISY_PATTERNS, COAGULA_DISABLE.

set -euo pipefail

input=$(cat)

# Default "allow unchanged" reply for bail paths.
allow_passthrough='{"permissionDecision":"allow"}'

[[ "${COAGULA_DISABLE:-0}" == "1" ]] && { echo "$allow_passthrough"; exit 0; }
command -v coagula >/dev/null 2>&1 || { echo "$allow_passthrough"; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "$allow_passthrough"; exit 0; }

tool_name=$(printf '%s' "$input" | jq -r '.toolName // empty')
[[ "$tool_name" != "bash" ]] && { echo "$allow_passthrough"; exit 0; }

# Pull the command from toolArgs. In Copilot CLI preToolUse, toolArgs is a
# JSON string; in postToolUse it's an object. Handle both.
parsed_args=$(printf '%s' "$input" \
  | jq -c 'if (.toolArgs | type) == "string"
           then (.toolArgs | fromjson)
           else .toolArgs end')
command=$(printf '%s' "$parsed_args" | jq -r '.command // empty')
[[ -z "$command" ]] && { echo "$allow_passthrough"; exit 0; }

# Don't double-wrap.
if printf '%s' "$command" | grep -qE '\| ?coagula( |$)'; then
  echo "$allow_passthrough"; exit 0
fi

# Built-in noisy-command pattern (matches the Claude Code hook).
default_pattern='^(kubectl|oc|helm|psql|mysql|sqlite3|az|gcloud|aws|curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|gh api|journalctl|dmesg|ps |netstat|lsof|iptables|systemctl|docker (ps|inspect|logs)|terraform (show|plan)) '
extra_pattern="${COAGULA_NOISY_PATTERNS:-}"

stripped=$(printf '%s' "$command" | sed 's/^[[:space:]]*//')
matched=0
if printf '%s' "$stripped" | grep -qiE "$default_pattern"; then
  matched=1
elif [[ -n "$extra_pattern" ]] && printf '%s' "$stripped" | grep -qiE "^($extra_pattern) "; then
  matched=1
fi
(( matched == 0 )) && { echo "$allow_passthrough"; exit 0; }

# Profile selection.
profile="passthrough"
case "$stripped" in
  kubectl*|oc*|helm*)     profile="k8s" ;;
  psql*|mysql*|sqlite3*)  profile="postgres" ;;
  az*|gcloud*|aws*)       profile="azure" ;;
esac

# Query derivation (env first, generic fallback).
query="${COAGULA_QUERY:-${COAGULA_TASK:-general diagnostic query}}"

budget="${COAGULA_BUDGET:-2000}"
keep="${COAGULA_KEEP:-5}"

quoted_query=$(printf '%q' "$query")
rewritten="( $command ) 2>&1 | coagula --query $quoted_query --profile $profile --budget $budget --keep $keep"

# Merge new command into the original args object so other fields (timeout,
# description, initial_wait, ...) are preserved.
new_args=$(printf '%s' "$parsed_args" | jq -c --arg cmd "$rewritten" '.command = $cmd')

jq -nc \
  --argjson args "$new_args" \
  --arg orig "$command" \
  '{
    permissionDecision: "allow",
    modifiedArgs: $args,
    additionalContext: ("[coagula] funneled output of: " + $orig)
  }'
