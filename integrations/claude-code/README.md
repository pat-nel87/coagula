# Claude Code hook bridge

A `PreToolUse` hook that automatically pipes noisy diagnostic Bash output
through `coagula` *before* it lands in Claude's context.

Claude Code's hook system can only modify tool *input* (via `updatedInput`)
— `PostToolUse` is read-only. So this hook covers Bash command rewriting
but not Read/Edit/MCP-result interception. If you want true universal
interception, GitHub Copilot CLI is currently the only host that supports
it (see [`integrations/copilot-cli/`](../copilot-cli/)). This Claude Code
hook is the best available there until / unless the spec adds a
`modifiedResult`-equivalent.

## What it does

When Claude is about to run a Bash command like `kubectl get pod -o json`, the
hook intercepts it and rewrites the command to:

```bash
( kubectl get pod -o json ) 2>&1 | coagula --query "<inferred>" --profile k8s --budget 2000 --keep 5
```

The model never sees the raw 130k-token blob — only the funneled prompt. A
short `additionalContext` note tells the model "this output was funneled" so
it understands the transformation happened.

## What gets intercepted

Built-in noisy-command list:

```
kubectl  oc  helm
psql     mysql  sqlite3
az       gcloud  aws
gh api   journalctl  dmesg
ps       netstat  lsof  iptables  systemctl
docker (ps|inspect|logs)
terraform (show|plan)
```

Profile auto-selection from the leading command:

| Command | `--profile` |
|---|---|
| `kubectl`, `oc`, `helm` | `k8s` |
| `psql`, `mysql`, `sqlite3` | `postgres` |
| `az`, `gcloud`, `aws` | `azure` |
| everything else in the list | `passthrough` |

Commands NOT in the list (e.g. `echo`, `ls`, `cat`, `make`, `git`) pass
through untouched — the hook only acts on real noise sources.

## Install

Requires: `coagula` on PATH, `jq` installed, `bash`.

```bash
# From the coagula repo:
./integrations/claude-code/install.sh
# This copies the hook to ~/.claude/coagula-bash-hook.sh and prints the
# settings.json snippet to add manually.

# Or let it patch settings.json for you (creates a .bak first):
./integrations/claude-code/install.sh --auto-update-settings
```

After install, start a new Claude Code session. The hook fires on every Bash
call; you'll see funneled output for noisy commands and the original output
for everything else.

## Config (env vars)

All optional. Set in your shell rc or per-session.

| Var | Default | What |
|---|---|---|
| `COAGULA_QUERY` | extracted from transcript | The query string passed to coagula for relevance ranking. |
| `COAGULA_BUDGET` | `2000` | Token budget for the funneled output. |
| `COAGULA_KEEP` | `5` | Top-K kept by relevance. |
| `COAGULA_NOISY_PATTERNS` | empty | Extra extended-regex alternation to add to the noisy-command match. Example: `helm\|terraform` (extends the built-in list). |
| `COAGULA_DISABLE` | `0` | Set to `1` to bypass the hook entirely without uninstalling. |

## Limitations

- **Bash only.** Read/Edit/Grep/Glob can't be intercepted this way — they take
  file paths or patterns, not pipe-able commands. The model still sees their
  raw output.
- **Query inference is best-effort.** If `COAGULA_QUERY` isn't set, the hook
  reads the last user message from the session transcript. Falls back to a
  generic query string if neither is available, which reduces relevance
  ranking quality but doesn't hurt dedup/prune.
- **The model sees the rewritten command.** Bash output shows the wrapped
  command (`( foo ) 2>&1 | coagula ...`), which is mildly verbose but harmless.
- **Hook fires per Bash call, not per session.** No state is preserved across
  calls. Use the `coagula-mcp` server's `retrieve` tool if you need access to
  deferred chunks.

## Why not Read / Edit?

Claude Code's `Read` tool takes a file path and returns the file content
directly — there's no command to rewrite. The honest options are:

1. Add a project `CLAUDE.md` rule telling the model "for files > N lines, use
   `Bash: cat <file> | coagula ...` instead of `Read`." Soft enforcement.
2. Wait for an `OutputTransform` / tool-output rewrite hook in a future Claude
   Code release (none exists as of June 2026).

## Uninstall

```bash
rm ~/.claude/coagula-bash-hook.sh
# Edit ~/.claude/settings.json and remove the hooks.PreToolUse entry that
# references coagula-bash-hook.sh.
```

## Comparable hooks in other hosts

| Host | PreToolUse modify input | PostToolUse modify output | Coverage |
|---|:-:|:-:|---|
| Claude Code (here) | ✅ `updatedInput` | ❌ | Bash only |
| VSCode + Copilot Chat (agent mode, Preview) | ✅ `updatedInput` (reuses this hook via `.claude/settings.json`) | ❌ | Bash only |
| **GitHub Copilot CLI ≥ 1.0 (Preview)** | ✅ `modifiedArgs` | ✅ **`modifiedResult`** | **Universal (any tool)** |

If you want interception on Read / Edit / MCP-tool results too, install the
Copilot CLI bridge instead/in addition: see
[`integrations/copilot-cli/`](../copilot-cli/). Earlier docs claimed no
equivalents existed in those hosts — that was wrong; the corrected matrix
above is what's actually shipped and verified locally.
