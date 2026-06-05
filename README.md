# coagula

<p align="center">
  <img src=".github/assets/coagula-mascot.png" alt="A robed pixel-art character at a control console feeding noisy data through a funnel labeled TRIM / DEDUP / PRUNE / RANK / COMPRESS, with the tagline 'Less Noise. More Signal. Lower Costs.'" width="420">
</p>

**The local context-funnel for GitHub Copilot CLI.** Trims, dedups, prunes,
ranks, and compresses noisy tool output *before* the model sees it — so the
metered input tokens you pay for are the ones that carry signal. Plugs into
Copilot CLI's hook system for automatic, universal interception (Bash, file
reads, MCP tool blobs) with zero code changes to the model side.

Also usable as a CLI or library for processing arbitrary noisy payloads
outside Copilot CLI sessions.

---

## Quick start — GitHub Copilot CLI

Two commands. End state: every tool output above ~2 000 tokens is silently
funneled through `coagula` before Copilot CLI's model sees (and bills) it.

**macOS / Linux:**

```bash
pip install https://github.com/pat-nel87/coagula/releases/download/v0.5.0/coagula-0.5.0-py3-none-any.whl
curl -fsSL https://raw.githubusercontent.com/pat-nel87/coagula/main/integrations/copilot-cli/install.sh | bash
```

**Windows (PowerShell):**

```powershell
pip install https://github.com/pat-nel87/coagula/releases/download/v0.5.0/coagula-0.5.0-py3-none-any.whl
iwr -useb https://raw.githubusercontent.com/pat-nel87/coagula/main/integrations/copilot-cli/install.ps1 | iex
```

Verify in a `copilot` session:

```bash
copilot -p "Run 'cat tests/fixtures/crashloop.log' and tell me the dominant error pattern" \
  --allow-all-tools --allow-all-paths --no-color
```

You should see the bash output prefixed with
`[coagula: 134715 → 37 tok | tool=bash profile=passthrough]` and the model
should still answer correctly. If you instead see the raw 4 000 lines, jump
to the [Copilot CLI walkthrough](#copilot-cli-walkthrough) — it covers
prerequisites (`gh`, `jq`), tuning env vars, and troubleshooting.

Kill switch: `export COAGULA_DISABLE=1` (PowerShell: `$env:COAGULA_DISABLE=1`).

---

## Why this matters now

GitHub flipped Copilot to **usage-based billing on June 1, 2026**. Every
Copilot Chat / Copilot CLI premium request now draws from a monthly AI
Credit budget at the underlying model's API rate — input + output + cached
tokens, all metered. Heavy diagnostic-tool sessions ("investigate this
incident", "explain this cluster's state") burn proportionally more.

coagula trims the *input* side of those sessions before the tokens are
billed. **What gets trimmed, and by how much, depends on payload shape:**

| Payload shape (measured on test fixtures) | Reduction | Why |
|---|---|---|
| Crashloop / journalctl-style logs with repeated templates | ~99% | Dedup collapses N identical-modulo-timestamp lines to 1 + `(xN)` |
| Bloated kubectl JSON (`-o json` of a healthy pod) | ~70% | Prune strips `managedFields`, `resourceVersion`, `annotations`, etc. |
| Structured Postgres / Azure ARM JSON | ~50-80% | Profile-specific denylist + array truncation |
| Mixed prose / code output (e.g. `cargo build`) | ~10-30% | Limited dedup; Relevance + Summarize need a query set |
| Pure code, well-structured text | ~5-15% | coagula barely helps — there's not much to compress |

Reductions are real and reproducible from `tests/fixtures/`. Percentages
**do not directly equal dollar savings** — that depends on which model your
Copilot session uses, how often you run noisy-tool investigations, and your
plan tier. **Real-session dollar benchmarks are TBD.**

For flat-fee plans (Claude Pro, Cursor, Windsurf) the dollar impact is
zero. You still get faster responses, less context-window pressure, and
cleaner inputs to the model.

References:
- [GitHub Copilot is moving to usage-based billing — GitHub Blog](https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/)
- [Models and pricing for GitHub Copilot — GitHub Docs](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing)
- ["What a joke": GitHub Copilot's new token-based billing — TechCrunch](https://techcrunch.com/2026/05/30/what-a-joke-github-copilots-new-token-based-billing-spurs-consternation-among-devs/)

---

## Does compression hurt the answer?

The honest version of "30-99% reduction" is paired with a regression check.
coagula ships an in-repo eval harness that measures the answer-accuracy
delta — does the model still answer correctly when fed the compressed
context vs the original?

```bash
python -m coagula.evals
```

Output:

```
CASE                      COMPRESSION   RAW  FUNNELED  DELTA
------------------------------------------------------------
crashloop-fatal                 99.9%    OK        OK  =
k8s-crashloop                   93.8%    OK        OK  =

raw accuracy: 2/2   funneled accuracy: 2/2   regressions: 0
```

By default the suite uses a deterministic substring-overlap "judge" — good
enough to catch regressions where compression destroyed the signal-bearing
line, not a substitute for real-model evaluation. Set `RUN_EVALS=1` (with
Azure or Ollama configured) to route through a real LLM. Add your own cases
via `EvalRunner.add_case`.

CI gate: `python -m coagula.evals --fail-on-regression` exits non-zero if
any case had a funneled-vs-raw accuracy regression.

---

## Copilot CLI walkthrough

Step-by-step for setting up automatic context funneling in `copilot` on a
fresh machine. Skip the steps you've already done.

### 1. Install the GitHub Copilot CLI

**macOS / Linux:**

```bash
brew install gh
gh auth login                            # GitHub auth + Copilot subscription
gh extension install github/copilot-cli  # or: npm i -g @github/copilot-cli
copilot --version                        # confirm ≥ 1.0
```

**Windows** (PowerShell):

```powershell
winget install GitHub.cli                # or: scoop install gh
gh auth login
gh extension install github/copilot-cli
copilot --version
```

If `copilot` isn't on your PATH after `npm` install, add `$(npm prefix -g)/bin`
to `PATH`. If you're inside an org that disabled hooks, see Troubleshooting.

### 2. Install `coagula` (plus `jq` if you'll use the bash hook)

```bash
pip install https://github.com/pat-nel87/coagula/releases/download/v0.5.0/coagula-0.5.0-py3-none-any.whl
coagula --help                  # verify on PATH
```

`jq` is needed for the bash hook to parse the JSON Copilot CLI streams in;
PowerShell native users don't need it:

```bash
brew install jq            # macOS
sudo apt install jq        # Debian/Ubuntu
winget install jqlang.jq   # Windows (only if using Git Bash)
```

### 3. Install the hook

**macOS / Linux / Git Bash:**

```bash
./integrations/copilot-cli/install.sh
```

**Windows PowerShell:**

```powershell
.\integrations\copilot-cli\install.ps1
```

Either installer copies the hook scripts to `~/.copilot/hooks-bin/` and
writes `~/.copilot/hooks/coagula.json` with **both** `bash` and `powershell`
command fields — Copilot CLI auto-picks per platform, and the PowerShell
hooks themselves further auto-defer to Git Bash if it's on PATH. So a
single config works on macOS, Linux, Windows native, and Windows + Git
Bash. Re-run with `--force` (or `-Force`) to overwrite an existing config.

### 4. Verify the hooks fire

```bash
copilot -p "Run 'cat tests/fixtures/crashloop.log' and tell me the dominant error pattern" \
  --allow-all-tools --allow-all-paths --no-color
```

What you should see:

- Bash call output prefixed with
  `[coagula: 134715 → 37 tok | tool=bash profile=passthrough]` instead of
  130 KB of log lines.
- Total `in` tokens in the session footer ~60 k, not ~150 k.
- The answer ("connection refused: upstream postgres unreachable") still
  correct.

If you instead see the raw 4 000 lines, jump to Troubleshooting below.

### 5. (Optional) Tune for your workflow

Set in `~/.zshrc` / `~/.bashrc` (macOS/Linux) or `$PROFILE` (Windows):

```bash
export COAGULA_QUERY="default query"        # overrides per-session inference
export COAGULA_BUDGET=2000                  # funneled output token cap
export COAGULA_KEEP=5                       # top-K chunks kept by relevance
export COAGULA_THRESHOLD=2000               # postToolUse skips outputs under this
export COAGULA_NOISY_PATTERNS="helm|terraform"   # extra preToolUse commands to intercept
export COAGULA_SKIP_TOOLS="my_internal_tool"     # extra postToolUse tools to bypass
export COAGULA_DISABLE=1                    # kill switch
```

Set `COAGULA_QUERY` per session to whatever you're trying to answer — that
unlocks the Relevance ranker.

**Lite mode** (default — no `COAGULA_QUERY` / `COAGULA_TASK` set): only the
lossless stages run (Normalize → Dedup → Prune → Budget → Assemble). Still
gets 95%+ reduction on log-shaped output via dedup alone. Skips Relevance +
Summarize entirely so no Azure / Ollama call happens by default.

**Debug log** — every real transform appends one line to
`~/.copilot/coagula-debug.log` (Windows: `$env:USERPROFILE\.copilot\coagula-debug.log`).
Passthroughs don't log, so the file is signal-dense. Tail with `tail -f`
(or `Get-Content -Wait` on Windows). Disable with `COAGULA_DEBUG_LOG=off`.

### Troubleshooting

**First step on any problem (Windows):** run the smoke-test diagnostic. It
walks 10 checks and tells you exactly what's missing:

```powershell
.\integrations\windows-smoke.ps1
.\integrations\windows-smoke.ps1 -SkipLiveSession   # don't burn a premium request
```

No Linux/macOS equivalent script yet — walk the same checks manually:
`coagula --help`, `jq --version`, `cat ~/.copilot/hooks/coagula.json`,
`tail -f ~/.copilot/logs/*.log` during a session.

Common specifics:

- **Hook doesn't fire.** Check `~/.copilot/logs/` (Windows: `%USERPROFILE%\.copilot\logs\`)
  for `preToolUse` / `postToolUse` lines. Usually `jq` missing (bash path) or
  `coagula` not on PATH for the shell `copilot` launched (use absolute paths
  in `coagula.json` if your shell rc isn't sourced for non-interactive bash).
- **"Permission denied" on the hook script.** macOS/Linux: `chmod +x ~/.copilot/hooks-bin/*.sh`.
  Windows: usually an ExecutionPolicy issue — the installer writes
  `powershell -ExecutionPolicy Bypass` which should sidestep it, but corporate
  AppLocker can override. Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
  or ask IT to allowlist the script path.
- **Funneled output too aggressive / signal lost.** Raise `COAGULA_BUDGET` and
  `COAGULA_KEEP`, or set `COAGULA_THRESHOLD=10000` so only enormous outputs
  get intercepted.
- **Org disabled hooks.** Some GitHub orgs disable Copilot CLI hooks via
  policy. Workaround: use the MCP server path below — it's LLM-invoked, not
  policy-restricted.
- **Tool repeatedly re-reads the spill file.** Copilot CLI persists original
  output at `/tmp/copilot-tool-output-*.txt` (Windows: `%TEMP%\copilot-tool-output-*.txt`);
  the model may go fetch the raw blob if it doesn't trust the funneled
  version. Tune `COAGULA_QUERY` to be specific so the funneled result
  actually contains the signal the model is after.

### What's verified end-to-end (honest status)

- **macOS:** Library, CLI, MCP server, Ollama, bash hooks in live Copilot
  CLI session — all verified locally.
- **Windows + GitHub Copilot CLI:** Verified end-to-end on Windows 11
  Enterprise + Copilot CLI 1.0.59 + Python 3.13. Hooks fire in live
  sessions; Azure OpenAI route succeeds when env vars are set; PowerShell
  native fallback engages without Git Bash present. CI matrix on
  `windows-latest` covers the same surface continuously.
- **Azure OpenAI:** Verified end-to-end against a real tenant (gpt-5.4 +
  gpt-5.4-nano, API version `2024-10-21`). Mocked unit tests run on every
  push; opt-in integration tests gate on `RUN_AZURE_TESTS=1`.
- **Ollama:** Verified end-to-end on macOS with `nomic-embed-text` +
  `llama3.2:3b`. Not yet exercised on Windows.

---

## Configuration

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `COAGULA_QUERY` | Per-session query — unlocks Relevance + Summarize stages | (unset → lite mode) |
| `COAGULA_BUDGET` | Funneled output token cap | 2000 |
| `COAGULA_KEEP` | Top-K chunks kept by Relevance | 5 |
| `COAGULA_THRESHOLD` | `postToolUse` skips outputs under this token count | 2000 |
| `COAGULA_NOISY_PATTERNS` | Extra `preToolUse` Bash commands to intercept (regex) | (built-in list) |
| `COAGULA_SKIP_TOOLS` | Extra `postToolUse` tools to bypass | (built-in skiplist) |
| `COAGULA_DISABLE` | Kill switch — hooks no-op | (off) |
| `COAGULA_BACKEND` | Force backend: `azure`, `ollama`, `fallback` | (auto) |
| `COAGULA_CACHE_STABLE` | Force `temperature=0` on LLM hooks so output is byte-stable across identical inputs — required for upstream provider prompt caches (Anthropic ~90% / OpenAI ~50% discount on cached input tokens) to hit. Truthy: `on`, `1`, `yes`, `true`, `enabled`. | off |
| `COAGULA_WORKSPACE_KEY` | DeferredStore scoping key for embedded library users running coagula across multiple project dirs in one process | process CWD |
| `COAGULA_DEBUG_LOG` | Override path, or `off` to disable | `~/.copilot/coagula-debug.log` |
| `OLLAMA_HOST` / `COAGULA_EMBED_MODEL` / `COAGULA_LLM_MODEL` | Ollama overrides | see below |
| `AZURE_OPENAI_*` | Azure deployment selection | see below |

### Optional: route Relevance + Summarize through a cheap LLM

By default `Relevance` uses TF-IDF and `Summarize` is extractive — both
stdlib, no model call. For better quality on noisy diagnostic payloads,
route those two stages through a cheaper-than-frontier LLM. The `coagula`
CLI auto-detects at startup with this priority: **`COAGULA_BACKEND`
override → Azure OpenAI → Ollama → stdlib fallback**.

**Azure OpenAI:**

```powershell
$env:AZURE_OPENAI_ENDPOINT         = "https://my-resource.openai.azure.com"
$env:AZURE_OPENAI_API_KEY          = "..."
$env:AZURE_OPENAI_LLM_DEPLOYMENT   = "gpt-5.4-nano"           # or gpt-4o-mini
$env:AZURE_OPENAI_EMBED_DEPLOYMENT = "text-embedding-3-small" # optional
$env:AZURE_OPENAI_API_VERSION      = "2024-10-21"             # optional
```

`LLM_DEPLOYMENT` is required to wire Azure; `EMBED_DEPLOYMENT` is optional
(Relevance keeps TF-IDF without it). Both factories accept a `fallback`
callable that fires on Azure errors so transient outages degrade to TF-IDF
rather than crashing.

**Ollama** (local + offline):

```bash
brew install --cask ollama-app && open -a Ollama
ollama pull nomic-embed-text llama3.2:3b
# Override defaults via OLLAMA_HOST, COAGULA_EMBED_MODEL, COAGULA_LLM_MODEL.
```

---

## Use as a CLI

```bash
python -m coagula.cli \
  --query "why is the payments pod crashlooping" \
  --budget 800 --keep 4 --report \
  tests/fixtures/noisy_mixed.txt

# Pipe real noisy context:
kubectl get pod <name> -o json | coagula --query "why is this pod failing" --report
```

## Use as a library

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

Or wire the backends directly:

```python
import os
from coagula import default_funnel
from coagula.models.azure_openai import make_embedder, make_llm

embed = make_embedder("text-embedding-3-small",
                      endpoint="https://my-resource.openai.azure.com",
                      api_key=os.environ["AZURE_OPENAI_API_KEY"])
llm   = make_llm("gpt-5.4-nano",
                 endpoint="https://my-resource.openai.azure.com",
                 api_key=os.environ["AZURE_OPENAI_API_KEY"])
funnel = default_funnel(embedder=embed, llm=llm, max_tokens=2000, keep=5)
```

---

## Design

Stages run in fixed order, cheap-before-expensive:

```
normalize → dedup → prune → relevance → summarize → budget → assemble
```

Everything is **demote, not delete**: pruned chunks become `DEFERRED` and
are retrievable on demand via `DeferredStore`. `CRITICAL` chunks are
sacrosanct — never demoted, never dropped. Severity pinning at ingestion
(`severity="FATAL"|"ERROR"` → `CRITICAL`) is the correctness guarantee
that compensates for an imperfect relevance ranker.

See `SPEC.md` for the full contract.

## What this isn't

- **Not a coding-agent universal tool.** v0.5.0 is Copilot-CLI-specific.
  Claude Code and VSCode Copilot Chat use flat-fee plans with large
  context windows, so the dollar/window-pressure motivation for compression
  doesn't apply. Earlier versions shipped Claude Code / VSCode hooks and
  an MCP server; both were removed in v0.5.0 to focus on the one host that
  actually benefits — see `v0.4.0` if you need those.
- **Not a vector DB / RAG store.** The funnel is stateless per request
  except for the per-request deferred store.
- **No telemetry, no network egress on the default path.** Ollama is local;
  hooks invoke the local `coagula` binary over stdio.

## Status

**v0.5.0** — Scoped exclusively to **GitHub Copilot CLI** via host hooks
(PowerShell on Windows, Bash on macOS/Linux/Git Bash). The seven-stage
funnel runs on stdlib alone; Azure OpenAI and Ollama are optional drop-in
backends. The accuracy-preservation eval harness and cache-stable mode
shipped in v0.4.0 carry over. Claude Code / VSCode Copilot Chat hook
integrations and the `coagula-mcp` MCP server were removed in this release
to reduce maintenance surface for a target audience that didn't benefit
from them.

## License

`coagula` is licensed under the [Apache License, Version 2.0](./LICENSE).
See [NOTICE](./NOTICE) for attribution requirements.

- Free for any use — personal, commercial, hosted, embedded.
- Modify and redistribute freely; keep the copyright + NOTICE.
- Patent grant from contributors; patent retaliation if you sue.
- Provided **as-is**, no warranty.

For vulnerability reports see [SECURITY.md](./SECURITY.md). To contribute,
see [CONTRIBUTING.md](./CONTRIBUTING.md) — every commit must be DCO-signed
(`git commit -s`).
