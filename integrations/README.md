# Integrations

Host-specific glue that makes `coagula` automatic, beyond the cross-host
`coagula-mcp` MCP server.

| Host | Mechanism | Seamlessness |
|---|---|---|
| **Claude Code** | [`claude-code/`](./claude-code/) — `PreToolUse` Bash hook | High — hook rewrites noisy commands before they run; model never sees the raw output |
| Claude Desktop | `coagula-mcp` (top-level README) | Medium — LLM-invoked tool, not automatic |
| VSCode + GitHub Copilot (1.99+) | `coagula-mcp` + `.github/copilot-instructions.md` rule | Low-medium — soft enforcement via system instruction |
| GitHub Copilot CLI (`gh copilot`) | Shell composition only | None — no tool/MCP support |

Pull requests welcome for additional hosts as they grow hook-equivalent APIs.
