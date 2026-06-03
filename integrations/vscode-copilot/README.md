# VSCode + GitHub Copilot Chat (agent mode)

VSCode 1.99+ ships agent hooks for Copilot Chat in **Preview**. The event
set mirrors Claude Code's: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `PreCompact`, `SubagentStart/Stop`, `Stop`.

For coagula, only `PreToolUse` is useful — `PostToolUse` is read-only in
VSCode (it cannot replace tool output the way GitHub Copilot CLI's can).
Bash command rewriting via `PreToolUse.updatedInput` works the same as in
Claude Code.

## The shortcut: reuse the Claude Code hook

VSCode Copilot Chat reads hook config from **both**:
- `.github/hooks/*.json` in the workspace (project-scope)
- `.claude/settings.json` and `~/.claude/settings.json` (Claude-compat)

So if you already installed the Claude Code bridge (`integrations/claude-code/install.sh`),
**it already works in VSCode Copilot Chat too** — no extra step. Open a
workspace, ask Copilot Chat to run `kubectl get pod -o json`, and the same
hook fires.

## Project-scoped option

If you want the hook to live inside a specific repo (so cloning the repo
opts collaborators in), drop `integrations/vscode-copilot/coagula.json`
into `.github/hooks/coagula.json` in that repo and edit the command path:

```jsonc
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${HOME}/.claude/coagula-bash-hook.sh"
          }
        ]
      }
    ]
  }
}
```

The hook script itself (`coagula-bash-hook.sh`) is the same one used by the
Claude Code bridge — install it once via `integrations/claude-code/install.sh`
and both hosts pick it up.

## Verify it works

1. Open the workspace in VSCode.
2. Switch Copilot Chat to **agent mode**.
3. Ask: "Run `kubectl get pod -o json my-pod` and tell me what's wrong."
4. Watch the terminal — the command Copilot runs should be the rewritten
   `( kubectl … ) 2>&1 | coagula --query … --profile k8s …` form.

## Limitations vs GitHub Copilot CLI

| | Claude Code | VSCode Copilot Chat | GitHub Copilot CLI |
|---|---|---|---|
| `PreToolUse` modify input | ✅ `updatedInput` | ✅ `updatedInput` | ✅ `modifiedArgs` |
| `PostToolUse` modify output | ❌ read-only | ❌ read-only | ✅ **`modifiedResult`** |
| Universal (non-Bash) interception | ❌ | ❌ | ✅ |
| Hook status | Stable | **Preview** | **Preview** |

Bottom line: VSCode Copilot Chat gives you the same Bash-rewriting
capability as Claude Code today. For true *universal* interception (Read,
Edit, MCP tool results, anything large), use the
[Copilot CLI integration](../copilot-cli/) instead — it's the only host
that supports it right now.

If/when Microsoft adds `modifiedResult`-equivalent to VSCode's `PostToolUse`,
we'll port the universal interceptor from the Copilot CLI integration.
