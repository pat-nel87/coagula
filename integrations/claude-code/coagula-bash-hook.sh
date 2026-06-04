#!/usr/bin/env bash
# Claude Code PreToolUse hook for Bash — automatically pipes the output of
# noisy diagnostic commands (kubectl, psql, az, gcloud, journalctl, etc.)
# through `coagula` before Claude sees it.
#
# Wire it up by adding this to ~/.claude/settings.json:
#
#   {
#     "hooks": {
#       "PreToolUse": [{
#         "matcher": "Bash",
#         "hooks": [{
#           "type": "command",
#           "command": "/absolute/path/to/coagula-bash-hook.sh"
#         }]
#       }]
#     }
#   }
#
# Env knobs (all optional):
#   COAGULA_QUERY            — query string passed to coagula; if unset, the
#                              hook tries to extract the last user message
#                              from the session transcript.
#   COAGULA_BUDGET           — token budget for the cleaned output (default 2000).
#   COAGULA_KEEP             — top-K relevance keep (default 5).
#   COAGULA_NOISY_PATTERNS   — extra extended-regex alternation appended to the
#                              built-in pattern (e.g. "helm|terraform").
#   COAGULA_DISABLE          — set to "1" to bypass the hook entirely.

set -euo pipefail

# Read the entire stdin payload from Claude Code.
input=$(cat)

# Bail cleanly if disabled.
if [[ "${COAGULA_DISABLE:-0}" == "1" ]]; then
  exit 0
fi

# Bail cleanly if coagula isn't installed.
if ! command -v coagula >/dev/null 2>&1; then
  exit 0
fi

# Need jq to parse the hook input JSON.
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')
[[ -z "$command" ]] && exit 0

# Built-in list of commands whose output is typically token-bloat.
# Match the command at the start of a pipeline, plus the common subcommand
# forms (`kubectl get ... -o json`, `az resource show`, etc.).
default_pattern='^(kubectl|oc|helm|psql|mysql|sqlite3|az|gcloud|aws|curl|wget|Invoke-WebRequest|Invoke-RestMethod|iwr|irm|gh api|journalctl|dmesg|ps |netstat|lsof|iptables|systemctl|docker (ps|inspect|logs)|terraform (show|plan)) '

# User-supplied extra patterns.
extra_pattern="${COAGULA_NOISY_PATTERNS:-}"

# Already piped through coagula? Don't double-wrap.
if printf '%s' "$command" | grep -qE '\| ?coagula( |$)'; then
  exit 0
fi

# Don't re-funnel reads of coagula's own spill files (Claude Code +
# Copilot CLI both persist original tool output to copilot-tool-output-*).
if printf '%s' "$command" | grep -qE 'copilot-tool-output-[A-Za-z0-9_-]+\.txt'; then
  exit 0
fi

# Match? Strip leading whitespace for the regex test.
stripped=$(printf '%s' "$command" | sed 's/^[[:space:]]*//')
matched=0
if printf '%s' "$stripped" | grep -qiE "$default_pattern"; then
  matched=1
elif [[ -n "$extra_pattern" ]] && printf '%s' "$stripped" | grep -qiE "^($extra_pattern) "; then
  matched=1
fi

if [[ "$matched" -eq 0 ]]; then
  exit 0
fi

# Determine the query.
query="${COAGULA_QUERY:-}"
if [[ -z "$query" ]]; then
  transcript_path=$(printf '%s' "$input" | jq -r '.transcript_path // empty')
  if [[ -n "$transcript_path" && -f "$transcript_path" ]]; then
    # JSONL transcript: last "user" message's text content.
    query=$(jq -r 'select(.role == "user") | .content[0].text? // .content? // empty' \
            "$transcript_path" 2>/dev/null \
            | tail -n 1 | tr '\n' ' ' | cut -c1-200)
  fi
fi
# No fallback string — when query is empty, omit --query so the CLI
# runs in lite mode rather than ranking against a meaningless string.

budget="${COAGULA_BUDGET:-2000}"
keep="${COAGULA_KEEP:-5}"

# Auto-detect the right per-tool pruning profile from the leading command.
profile="passthrough"
case "$stripped" in
  kubectl*|oc*|helm*)            profile="k8s" ;;
  psql*|mysql*|sqlite3*)         profile="postgres" ;;
  az*|gcloud*|aws*)              profile="azure" ;;
esac

# Rewrite the command. Wrap in a subshell so existing redirections/pipes
# are preserved, then pipe the combined output through coagula.
if [[ -n "$query" ]]; then
  quoted_query=$(printf '%q' "$query")
  rewritten="( $command ) 2>&1 | coagula --query $quoted_query --profile $profile --budget $budget --keep $keep"
else
  rewritten="( $command ) 2>&1 | coagula --profile $profile --budget $budget --keep $keep"
fi

# Debug log — append-only, transform-only.
log_path="${COAGULA_DEBUG_LOG:-$HOME/.copilot/coagula-debug.log}"
case "$log_path" in
  off|OFF|disabled|DISABLED|0) : ;;
  *)
    ts=$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s [claude-pre-bash] rewrote (profile=%s): %s\n' "$ts" "$profile" "$command" \
      >>"$log_path" 2>/dev/null || true
    ;;
esac

# Emit PreToolUse response with updatedInput.command.
jq -nc \
  --arg cmd "$rewritten" \
  --arg prof "$profile" \
  '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      updatedInput: { command: $cmd },
      additionalContext: ("[coagula:pre-bash:" + $prof + "]")
    }
  }'
