# Integrations

Host-specific glue that makes `coagula` automatic, beyond the cross-host
`coagula-mcp` MCP server.

## Capability matrix (June 2026)

| Host | PreToolUse modify input | PostToolUse modify output | Status | Coverage |
|---|:-:|:-:|:-:|---|
| **GitHub Copilot CLI** ≥ 1.0 | ✅ `modifiedArgs` | ✅ **`modifiedResult`** | Preview | **Universal interception** — any tool |
| **Claude Code** | ✅ `updatedInput` | ❌ read-only | Stable | Bash only |
| **VSCode + Copilot Chat** (agent mode) | ✅ `updatedInput` | ❌ read-only | Preview | Bash only |
| Claude Desktop / VSCode (non-Copilot) | ➖ no host hooks | ➖ | — | MCP server only (LLM-invoked) |
| GitHub Copilot CLI (legacy `gh copilot`) | ➖ no hooks/MCP | ➖ | — | Shell composition only |

**GitHub Copilot CLI is the most powerful host today** — its `postToolUse`
hook can replace the result of *any* tool call (Bash, view, MCP tools)
via `modifiedResult`. That's true universal interception. Neither Claude
Code nor VSCode Copilot allows this — they only let you rewrite the input.

## Integrations shipped here

| Dir | Host | What |
|---|---|---|
| [`copilot-cli/`](./copilot-cli/) | GitHub Copilot CLI | `preToolUse` Bash rewriter **+** `postToolUse` universal interceptor (the highest-leverage integration) |
| [`claude-code/`](./claude-code/) | Claude Code | `PreToolUse` Bash rewriter |
| [`vscode-copilot/`](./vscode-copilot/) | VSCode + Copilot Chat | Reuses the Claude Code hook (VSCode reads `.claude/settings.json`); project-scoped `.github/hooks/coagula.json` template included |

## What I had wrong in earlier docs

Earlier versions of this README claimed GitHub Copilot CLI had "no
tool/MCP/plugin support" and that VSCode Copilot Chat had "no hook
equivalent." Both were wrong:

- GitHub Copilot CLI 1.0 (GA February 2026) ships built-in MCP server
  support and an 11-event hook system, including the only `modifiedResult`
  capability in the market today.
- VSCode Copilot Chat shipped agent hooks in Preview with the same eight
  events as Claude Code, and deliberately reads `.claude/settings.json` so
  Claude Code hooks transfer for free.

Both pieces of info post-dated the model's training cutoff; the corrected
matrix above is now built and tested.

## Recommended setup

For maximum coverage:

1. Install the Claude Code bridge → covers Claude Code Bash + VSCode
   Copilot Chat Bash (same hook script, both hosts pick it up):
   ```bash
   ./integrations/claude-code/install.sh --auto-update-settings
   ```

2. Install the Copilot CLI bridge → adds true universal interception for
   Copilot CLI sessions:
   ```bash
   ./integrations/copilot-cli/install.sh
   ```

3. Cross-host MCP fallback → `pip install "coagula[mcp]"` then
   `claude mcp add coagula coagula-mcp` (Claude Code) /
   `coagula-mcp` in Claude Desktop or VSCode MCP config. LLM-invoked but
   works everywhere.

Pull requests welcome for additional hosts as they grow hook-equivalent
APIs (Cursor, JetBrains AI, etc.).
