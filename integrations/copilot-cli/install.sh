#!/usr/bin/env bash
# Install the coagula hooks for GitHub Copilot CLI.
#
# - Copies the two hook scripts to ~/.copilot/hooks-bin/
# - Writes ~/.copilot/hooks/coagula.json pointing at them
# - Refuses to clobber existing coagula.json unless --force
#
# Usage:
#   ./integrations/copilot-cli/install.sh
#   ./integrations/copilot-cli/install.sh --force

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PRE="$SCRIPT_DIR/coagula-pre-bash-hook.sh"
POST="$SCRIPT_DIR/coagula-post-tool-hook.sh"
PRE_PS="$SCRIPT_DIR/coagula-pre-bash-hook.ps1"
POST_PS="$SCRIPT_DIR/coagula-post-tool-hook.ps1"

HOOK_DIR="$HOME/.copilot/hooks"
BIN_DIR="$HOME/.copilot/hooks-bin"
CONFIG="$HOOK_DIR/coagula.json"

force=0
[[ "${1:-}" == "--force" ]] && force=1

if [[ ! -f "$PRE" || ! -f "$POST" ]]; then
  echo "Hook source scripts not found in $SCRIPT_DIR" >&2
  exit 1
fi
if ! command -v copilot >/dev/null 2>&1; then
  echo "WARNING: 'copilot' (GitHub Copilot CLI) not on PATH." >&2
fi
if ! command -v coagula >/dev/null 2>&1; then
  echo "WARNING: 'coagula' not on PATH. Install with: pip install coagula" >&2
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: 'jq' required. brew install jq" >&2
  exit 1
fi

mkdir -p "$HOOK_DIR" "$BIN_DIR"
cp "$PRE" "$POST" "$BIN_DIR/"
# Also bundle the .ps1 ports for users whose configs roam to Windows
# or who use Git Bash + a Windows-launched Copilot CLI.
[[ -f "$PRE_PS" ]] && cp "$PRE_PS" "$BIN_DIR/"
[[ -f "$POST_PS" ]] && cp "$POST_PS" "$BIN_DIR/"
chmod +x "$BIN_DIR"/coagula-*.sh
echo "Hooks → $BIN_DIR"

# Write a config with BOTH `bash` and `powershell` fields. The Copilot CLI
# host picks per platform (bash on Linux/macOS, powershell on Windows),
# and the .ps1 itself further auto-defers to Git Bash when available.
ps_invocation='powershell -NoProfile -ExecutionPolicy Bypass -File'

if [[ -f "$CONFIG" && "$force" -ne 1 ]]; then
  echo
  echo "Existing $CONFIG found. Re-run with --force to overwrite, or merge"
  echo "the following snippet into it manually:"
  echo
fi

config_body=$(cat <<EOF
{
  "version": 1,
  "hooks": {
    "preToolUse": [
      {
        "type": "command",
        "bash": "$BIN_DIR/coagula-pre-bash-hook.sh",
        "powershell": "$ps_invocation \"$BIN_DIR/coagula-pre-bash-hook.ps1\""
      }
    ],
    "postToolUse": [
      {
        "type": "command",
        "bash": "$BIN_DIR/coagula-post-tool-hook.sh",
        "powershell": "$ps_invocation \"$BIN_DIR/coagula-post-tool-hook.ps1\""
      }
    ]
  }
}
EOF
)

if [[ -f "$CONFIG" && "$force" -ne 1 ]]; then
  echo "$config_body"
else
  printf '%s\n' "$config_body" > "$CONFIG"
  echo "Config → $CONFIG"
fi

cat <<'NOTE'

Optional env knobs (set in your shell rc before running copilot):
  COAGULA_QUERY      — query for relevance ranking
  COAGULA_BUDGET     — token budget for cleaned output (default 2000)
  COAGULA_KEEP       — top-K kept by relevance (default 5)
  COAGULA_THRESHOLD  — postToolUse skips outputs under this token count
                       (default 2000)
  COAGULA_SKIP_TOOLS — extra comma-separated tool names to bypass
  COAGULA_DISABLE    — set to 1 to bypass entirely

Verify with: copilot -p "cat <some-big-file>" --allow-all-tools --allow-all-paths
You should see the model receive a `[coagula: N → M tok …]`-prefixed result.
NOTE
