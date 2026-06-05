# Integrations

Host hook glue that makes `coagula` automatic inside GitHub Copilot CLI.

## Why only GitHub Copilot CLI?

As of June 2026, Copilot CLI is the only major coding-agent host with a
`postToolUse.modifiedResult` hook that can replace the *output* of any
tool call (Bash, file read, MCP tool blob) before it reaches the model.
That's the integration coagula was designed for: universal, automatic,
zero model effort.

Other hosts (Claude Code, VSCode Copilot Chat) only support rewriting
Bash *input* (`updatedInput`). Earlier versions of coagula shipped hooks
for those too, plus a standalone `coagula-mcp` MCP server. Both were
removed in v0.5.0 to focus on the audience that benefits from
compression — Copilot CLI users billed on usage-based AI Credits.

Need a different host? `git checkout v0.4.0` for the Claude Code +
VSCode + MCP server code.

## Layout

| Directory | Purpose |
|---|---|
| [`copilot-cli/`](./copilot-cli/) | `preToolUse` Bash arg rewriter + `postToolUse` universal output funnel; bash + PowerShell ports |

Per-platform install:

```bash
./integrations/copilot-cli/install.sh        # macOS / Linux / Git Bash
.\integrations\copilot-cli\install.ps1       # Windows PowerShell
```

The installer writes `~/.copilot/hooks/coagula.json` with both `bash` and
`powershell` command fields — Copilot CLI picks the right one per platform.
PowerShell hooks auto-defer to Git Bash if it's on PATH, so a single
config works on macOS, Linux, Windows native, and Windows + Git Bash.

## Smoke test (Windows)

```powershell
.\integrations\windows-smoke.ps1                  # full check including live copilot session
.\integrations\windows-smoke.ps1 -SkipLiveSession # skip the premium-request live test
```

10 checks: Python, `coagula` on PATH, optional `jq`, funnel runs end-to-
end on a noisy log, PS hook scripts parse and produce expected JSON,
`coagula.json` exists with dual-field config, `copilot` on PATH, live
hook firing, optional Azure OpenAI ping.

No Linux/macOS equivalent yet — walk the same checks manually:
`coagula --help`, `jq --version`, `cat ~/.copilot/hooks/coagula.json`,
`tail -f ~/.copilot/logs/*.log` during a session.
