#!/usr/bin/env bash
# Install the coagula Claude Code Bash PreToolUse hook.
#
# - Copies coagula-bash-hook.sh to ~/.claude/coagula-bash-hook.sh
# - Prints the settings.json snippet to add to ~/.claude/settings.json
# - Refuses to modify settings.json automatically (you should see the diff first)
#
# Usage:
#   ./integrations/claude-code/install.sh
#   ./integrations/claude-code/install.sh --auto-update-settings   # opt-in

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SRC="$SCRIPT_DIR/coagula-bash-hook.sh"
DEST_DIR="$HOME/.claude"
DEST="$DEST_DIR/coagula-bash-hook.sh"
SETTINGS="$DEST_DIR/settings.json"

auto_update=0
if [[ "${1:-}" == "--auto-update-settings" ]]; then
  auto_update=1
fi

# Sanity checks.
if [[ ! -f "$SRC" ]]; then
  echo "Hook script not found at $SRC" >&2
  exit 1
fi
if ! command -v coagula >/dev/null 2>&1; then
  echo "WARNING: 'coagula' not on PATH. Install with: pip install coagula" >&2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "WARNING: 'jq' not on PATH. The hook needs it. brew install jq" >&2
fi

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST"
chmod +x "$DEST"
echo "Installed hook → $DEST"

snippet=$(cat <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "$DEST"
          }
        ]
      }
    ]
  }
}
EOF
)

if [[ "$auto_update" -eq 1 ]]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo "jq required for --auto-update-settings; bailing." >&2
    exit 1
  fi
  if [[ -f "$SETTINGS" ]]; then
    backup="${SETTINGS}.bak.$(date +%s)"
    cp "$SETTINGS" "$backup"
    echo "Backed up existing settings → $backup"
  else
    echo '{}' > "$SETTINGS"
  fi
  # Merge: preserve existing hooks, add Bash matcher only if not already present.
  tmp=$(mktemp)
  jq --arg cmd "$DEST" '
    .hooks //= {} |
    .hooks.PreToolUse //= [] |
    .hooks.PreToolUse |= (
      if any(.matcher == "Bash") then
        map(if .matcher == "Bash"
            then .hooks += [{type: "command", command: $cmd}] | .hooks |= unique_by(.command)
            else . end)
      else
        . + [{matcher: "Bash", hooks: [{type: "command", command: $cmd}]}]
      end
    )
  ' "$SETTINGS" > "$tmp" && mv "$tmp" "$SETTINGS"
  echo "Updated $SETTINGS — hook wired to the Bash matcher."
else
  echo
  echo "Add the following to $SETTINGS (or merge with your existing hooks)."
  echo "Re-run with --auto-update-settings to do this automatically."
  echo
  echo "$snippet"
fi

echo
echo "Test by starting a Claude Code session and asking Claude to run something"
echo "like 'kubectl get pod -o json' — the hook will rewrite it to pipe through coagula."
