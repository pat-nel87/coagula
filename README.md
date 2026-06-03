# coagula

A local **context manicuring funnel**: trim, dedup, prune, rank, and compress
context *before* it reaches an expensive frontier LLM call, so most tokens are
killed by cheap deterministic logic and only what survives is spent on
inference.

Built primarily for noisy diagnostic payloads (kubectl JSON, crashloop logs,
Postgres stats, Azure ARM responses) but works on any context.

## Status

**v0.2.0 — M5 complete.** Seven-stage funnel, CLI, Ollama hooks, MCP adapter
library, and a standalone MCP server (`coagula-mcp`) usable from Claude Code,
Claude Desktop, and VSCode 1.99+ with GitHub Copilot. Runs on the standard
library alone; Ollama and MCP SDK are optional extras. ≥99% token reduction
on the SPEC §11 acceptance scenario with the FATAL signal always preserved.

## Install

```bash
# Library + CLI only:
pip install https://github.com/pat-nel87/coagula/releases/download/v0.2.0/coagula-0.2.0-py3-none-any.whl

# With MCP server:
pip install "coagula[mcp] @ https://github.com/pat-nel87/coagula/releases/download/v0.2.0/coagula-0.2.0-py3-none-any.whl"

# Development:
git clone https://github.com/pat-nel87/coagula.git && cd coagula
pip install -e ".[dev]"
pytest
```

## Use as a CLI

```bash
python -m coagula.cli \
  --query "why is the payments pod crashlooping" \
  --budget 800 --keep 4 --report \
  tests/fixtures/noisy_mixed.txt

# Pipe real noisy context:
kubectl get pod <name> -o json | coagula --query "why is this pod failing" --report
```

## Use as an MCP server (Claude Code / Desktop / VSCode Copilot)

The `coagula-mcp` console script speaks MCP over stdio. Register it once and
the LLM gets two tools: `manicure` (trim a payload) and `retrieve` (pull
deferred chunks back). Per-tool JSON pruning via `profile="k8s"|"postgres"|"azure"`.

### Claude Code

```bash
claude mcp add coagula coagula-mcp
```

### Claude Desktop

In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "coagula": {
      "command": "coagula-mcp"
    }
  }
}
```

### VSCode + GitHub Copilot (1.99+)

In your user `settings.json`:

```json
{
  "github.copilot.chat.mcp.servers": {
    "coagula": {
      "command": "coagula-mcp"
    }
  }
}
```

(Replace the key with whatever your Copilot version expects; the SDK transport
is stdio either way.)

### Automatic interception in Claude Code (the closest thing to a true interceptor)

Claude Code's `PreToolUse` hook can rewrite Bash commands *before* they run.
The `integrations/claude-code/` hook auto-pipes noisy commands (`kubectl`,
`psql`, `az`, `gcloud`, `journalctl`, etc.) through `coagula` so the model
never sees the raw output:

```bash
./integrations/claude-code/install.sh
# or, to also patch ~/.claude/settings.json automatically:
./integrations/claude-code/install.sh --auto-update-settings
```

See [integrations/claude-code/README.md](./integrations/claude-code/README.md)
for the full docs, env knobs, and limitations. No equivalent host-level hook
exists for VSCode Copilot or Copilot CLI today — use the `coagula-mcp`
server or instruction-level guidance there.

### Optional: enable Ollama for better ranking + abstractive summarization

```bash
# Install Ollama (macOS):
brew install --cask ollama-app
open -a Ollama
ollama pull nomic-embed-text llama3.2:3b

# The coagula-mcp server will auto-detect Ollama and wire it in.
# Override defaults with env vars:
#   OLLAMA_HOST=http://localhost:11434
#   COAGULA_EMBED_MODEL=nomic-embed-text
#   COAGULA_LLM_MODEL=llama3.2:3b
```

## Use as a library (embed in your own MCP tool)

```python
from coagula.mcp import coagula_payload, ChunkSpec

result = coagula_payload(
    [
        ChunkSpec(text=kubectl_json, kind="json", source="kubectl/pod"),
        ChunkSpec(text=crashloop, kind="log", source="logs/payments"),
        ChunkSpec(text=fatal_line, source="alerts", severity="FATAL"),
    ],
    query="why is this pod crashlooping",
    profile="k8s",
    max_tokens=2000,
    extra_critical_patterns=[r"payment_id=\d+"],
)
print(result.prompt)               # the cleaned context
print(result.deferred_manifest)    # what got demoted, retrievable by id
print(result.report)               # per-stage savings table
```

## Design

Stages run in fixed order, cheap-before-expensive:

```
normalize → dedup → prune → relevance → summarize → budget → assemble
```

Everything is **demote, not delete**: pruned chunks become `DEFERRED` and are
retrievable on demand via the `DeferredStore` / `retrieve` MCP tool.
`CRITICAL` chunks are sacrosanct — never demoted, never dropped. Severity
pinning at ingestion (`severity="FATAL"|"ERROR"` → `CRITICAL`) is the
correctness guarantee that compensates for an imperfect relevance ranker.

See `SPEC.md` for the full contract.

## What this isn't

- **Not an automatic interceptor.** MCP tools are LLM-invoked. coagula
  doesn't see context the LLM doesn't ask it to see. For true automatic
  interception you'd want a Claude Code hook (separate project).
- **Not a vector DB / RAG store.** The funnel is stateless per request
  except for the per-request deferred store.
- **No telemetry, no network egress on the default path.** Ollama is local;
  the MCP server is stdio.
