# GitHub Copilot CLI hook bridge

Two hooks that, together, give you **true universal interception** in
GitHub Copilot CLI — strictly more capable than the Claude Code integration
because Copilot CLI's `postToolUse` event can return `modifiedResult` to
replace any tool's output.

| Hook | Event | What it does | Why |
|---|---|---|---|
| `coagula-pre-bash-hook.sh` | `preToolUse` | Rewrites noisy `bash` commands (`kubectl`, `psql`, `az`, `gcloud`, `curl`, `Invoke-WebRequest`, etc.) to pipe through `coagula` before they run | Trim noise at the source — smaller pipe-through cost |
| `coagula-post-tool-hook.sh` | `postToolUse` | Catches the result of *any* tool above N tokens (`bash`, `view`, MCP tools, …) and replaces it with a coagula-funneled version via `modifiedResult` | True universal interception — the model never sees the raw noise |

Use both together. The pre hook gives a tight loop for known noise sources;
the post hook backstops *everything else*.

## Install

Requires: `copilot` (GitHub Copilot CLI ≥ 1.0), `coagula`, `jq`, `bash`.

```bash
./integrations/copilot-cli/install.sh
# Or, to overwrite an existing ~/.copilot/hooks/coagula.json:
./integrations/copilot-cli/install.sh --force
```

Then run a `copilot` session normally. To verify the hooks fire:

```bash
copilot -p "Run 'cat /path/to/big/file' and tell me the dominant pattern" \
  --allow-all-tools --allow-all-paths --no-color
```

The model's input tokens should drop dramatically and you'll see a
`[coagula: <orig> → <after> tok | tool=… profile=…]` prefix on the funneled
result.

## Config (env vars)

Set before running `copilot`. All optional.

| Var | Default | What |
|---|---|---|
| `COAGULA_QUERY` | derived from `COAGULA_TASK` or generic | Query string for relevance ranking. |
| `COAGULA_BUDGET` | `2000` | Token budget for funneled output. |
| `COAGULA_KEEP` | `5` | Top-K kept by relevance. |
| `COAGULA_THRESHOLD` | `2000` | postToolUse skips outputs under this token count. |
| `COAGULA_NOISY_PATTERNS` | empty | Extra extended-regex alternation for preToolUse (e.g. `helm\|terraform`). |
| `COAGULA_SKIP_TOOLS` | empty | Extra comma-separated tool names for postToolUse to bypass. Always-skipped: `report_intent`, `sql`, `todo_*`, `notification`. |
| `COAGULA_DISABLE` | `0` | Set to `1` to bypass both hooks. |

## How the hooks interact

If you have both installed (recommended):

1. Model emits `kubectl get pod -o json`.
2. **preToolUse** matches → rewrites to `( kubectl … ) 2>&1 | coagula …`.
3. Bash runs the rewritten command — output is already funneled.
4. **postToolUse** sees the (small) output → under threshold → no-op.

If you have only the post hook:

1. Model emits a `view` on a 200 KB log file.
2. View tool returns the full file content.
3. **postToolUse** matches → token count over threshold → funnels via
   `modifiedResult`. Model sees the cleaned version, not the original.

If you have only the pre hook: same as the Claude Code bridge — Bash-only.

## What gets the universal treatment (post hook)

The post hook runs on the result of nearly every tool Copilot CLI
dispatches `postToolUse` for, except the always-skipped bookkeeping set:

```
report_intent  sql  todo_*  notification
```

`bash` / `powershell` / `view` / `read_powershell` / MCP tool results,
GitHub MCP server results, etc. — all eligible for funneling if they
exceed `COAGULA_THRESHOLD`.

Profile auto-selection (post hook): for shell-family tools, the leading
command picks `--profile k8s|postgres|azure` per the same table as the
pre hook. For other tools, profile is `passthrough` (denylist disabled;
only dedup/relevance/summarize/budget apply).

### Known coverage gap — `web_fetch`

As of Copilot CLI 1.0.59, the `web_fetch` tool's results do **not**
trigger `postToolUse` dispatch. Confirmed empirically: in a captured
`--log-level debug` session with 2 `web_fetch` calls and 33 other tool
calls, exactly 33 post-tool hook executions appeared. The hook process
is never spawned for `web_fetch` results — independent of any
filtering coagula does internally.

This means HTTP responses fetched via the `web_fetch` tool — often the
largest single category by bytes in real diagnostic sessions — bypass
the funnel entirely. coagula will compress them if you prompt the
model to fetch via `Invoke-RestMethod` / `Invoke-WebRequest` / `curl`
through the `powershell` tool instead, since those commands match the
default noisy pattern (v0.3.3+) and pre-bash rewrites them in-pipeline.

Tracking: the architectural fix lives upstream in `github/copilot-cli`
— a hook-dispatch issue, not a coagula bug. We'll link the upstream
issue here when it's filed.

## Idempotency

The post hook detects output that already looks like coagula's own
`### {source}` formatting and skips re-funneling. Safe to chain.

## Uninstall

```bash
rm ~/.copilot/hooks/coagula.json
rm -rf ~/.copilot/hooks-bin
```

## Limitations / sharp edges

- **`toolArgs` shape inconsistency in Copilot CLI:** preToolUse delivers it
  as a JSON string; postToolUse as an object. The hooks normalize both
  forms via `jq`, but watch out if you patch them.
- **`additionalContext` is not surfaced to the model in current Copilot CLI
  builds** for some tool types — there's an open issue
  ([github/copilot-cli#2980](https://github.com/github/copilot-cli/issues/2980))
  about this. The hooks still set it for future compatibility; the real
  signal travels in `modifiedResult.textResultForLlm`.
- **Spill files:** Copilot CLI persists the original (pre-modified) tool
  output to a temp file (`copilot-tool-output-*.txt`). The model can still
  read those if it wants the full original. That's a feature — it's the
  same "deferred chunks are retrievable" property coagula's own MCP server
  exposes, just at the CLI layer.
- **Hooks are Preview-grade in Copilot CLI** — config format may change.
  Pin to a specific Copilot CLI version in CI/prod scripts.
