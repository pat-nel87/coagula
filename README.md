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
pip install https://github.com/pat-nel87/coagula/releases/download/v0.7.3/coagula-0.7.3-py3-none-any.whl
curl -fsSL https://raw.githubusercontent.com/pat-nel87/coagula/main/integrations/copilot-cli/install.sh | bash
```

**Windows (PowerShell):**

```powershell
pip install https://github.com/pat-nel87/coagula/releases/download/v0.7.3/coagula-0.7.3-py3-none-any.whl
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

### When coagula actually helps — measured against real Copilot CLI sessions

The compression engine works; the question is whether the **firing rate**
× **savings per firing** product is positive on your workload. Below are
results from counterbalanced 4-trial experiments (baseline-funneled-
baseline-funneled, cache held constant at ~26-87k cached tokens across
all trials so any delta is attributable to coagula, not prompt-cache
priming).

| Workload | Model | Baseline | Funneled | Δ |
|---|---|---|---|---|
| **bash dump** (force `cat large_file`, no pipes) | GPT-5 | 9.29 cr | **7.93 cr** | **−14.6%** |
| Paginated single-line views (20× `view file:N-N`) | GPT-5 | 8.11 cr | 8.20 cr | +1.0% (noise) |
| Paginated single-line views (20× `view file:N-N`) | Sonnet 4.6 | 8.14 cr | 8.18 cr | +0.5% (noise) |

**Two real-world conclusions from those numbers:**

1. **coagula provides ~15% credit savings on bash-dump diagnostic
   workflows.** This is the original design target — `kubectl get pods -o
   yaml`, `journalctl -u svc`, `psql ... select * from big_table`, etc.
   The model dispatches one big command, the postToolUse hook intercepts
   the multi-KB output, the funnel's lossless stages (Dedup + Prune)
   compress it 70-99%. Even when Copilot CLI spills the output to a
   temp file and the model views the spill, coagula's view-summary path
   catches it.

2. **coagula is essentially breakeven on paginated single-line view
   workloads regardless of model.** Both GPT-5 and Claude Sonnet 4.6
   ignored coagula's nudge + file-summary injection text and made all 20
   view calls anyway. v0.7.3 made the summary one-shot per file so the
   overhead is ~zero, but the model behavior doesn't change — there's no
   compression opportunity in a 1-line view output. **If your sessions
   are 100% surgical line-range reads with no bash dumps, this tool will
   neither help nor hurt; that's a structural limit of the postToolUse
   hook interface, not a bug.**

Workloads coagula provably **cannot** help:

| Workload | Why |
|---|---|
| `web_fetch` tool results | Doesn't dispatch postToolUse — [upstream issue](https://github.com/github/copilot-cli/issues/3665) |
| User pastes raw content into chat | Prompts don't go through postToolUse — only tool calls do |
| Sessions where the model `grep`/`uniq`-pre-filters everything | Hook fires but on already-summarized output (model did the work) |

The compression-percentages-on-fixtures table above is real, but it
measures only the second multiplier (savings per firing). Real session
deltas depend on what fraction of your tool calls are bash dumps vs
surgical reads.

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
pip install https://github.com/pat-nel87/coagula/releases/download/v0.7.3/coagula-0.7.3-py3-none-any.whl
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
export COAGULA_NOISY_PATTERNS="helm|terraform"   # extra preToolUse commands to intercept
export COAGULA_SKIP_TOOLS="my_internal_tool"     # extra postToolUse tools to bypass
export COAGULA_DISABLE=1                    # kill switch
```

#### Thresholds (v0.6.0+)

Two layers control when the postToolUse hook actually fires.

**Per-tool defaults** (don't usually need to change):

| Tool type | Default token threshold |
|---|---|
| `bash` / `shell` / `powershell` | 2000 |
| `view` / `read` / `read_file` | **500** |
| MCP tools (`mcp:*` / `*__*`) | 1000 |
| Other tools | 1500 |

Why per-tool: a 1KB `bash` output is usually a real answer (version probes,
ls), but a 1KB `view` output is often paginated noise that compresses 60-80%.
The lower view threshold was empirically validated on real Windows Copilot
CLI sessions before being promoted.

Override globally with `COAGULA_THRESHOLD=<n>` (replaces every per-tool
default with that single number — useful for A/B testing).

**Cumulative session tracking** catches the death-by-a-thousand-cuts case
where a model makes many sub-threshold `view_range` or `grep` calls that
individually slip through:

```bash
export COAGULA_CUMULATIVE_THRESHOLD=8000   # default; 0 disables
```

State lives in `~/.copilot/coagula-session-state/<ppid>.json` (per copilot
process). Once the session total crosses the threshold, subsequent
sub-threshold calls get funneled too. Entries older than 1h are pruned.

**Lite mode** (default — no `COAGULA_QUERY` / `COAGULA_TASK` set): only the
lossless stages run (Normalize → Dedup → Prune → Budget → Assemble). Still
gets 95%+ reduction on log-shaped output via dedup alone. Skips Relevance +
Summarize entirely so no Azure / Ollama call happens by default.

#### Reading the debug log

v0.6.0+ logs every postToolUse decision (not just successful funnelings).
Tail it to see your actual firing rate:

```bash
tail -f ~/.copilot/coagula-debug.log
```

Each line categorizes the verdict:

| Tag | Meaning |
|---|---|
| `fired` | Compression happened; line shows `in=X out=Y reason=...` |
| `under-threshold` | Call was below the threshold (per-call AND cumulative) — passed through |
| `skipped` | Tool was on the skip list (internal bookkeeping or `COAGULA_SKIP_TOOLS`) |
| `funnel-noop` | Funnel ran but couldn't shrink — original passed through |
| `coagula-missing` | `coagula` not on PATH; hooks are no-ops. **Install coagula globally to fix.** |
| `disabled` | `COAGULA_DISABLE=1` set |

If you see lots of `under-threshold` and few `fired`, your workload is
mostly small-tool-output and per-call thresholds don't catch it. The
cumulative-trigger should kick in eventually; if not, lower
`COAGULA_CUMULATIVE_THRESHOLD`. If you see `coagula-missing`, the hooks
are running but doing nothing — install coagula in a Python that's on the
shell's PATH (not just in a venv).

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
  policy. There's no workaround inside Copilot CLI itself; either pre-funnel
  payloads manually with the `coagula` CLI before feeding them in
  (`kubectl … | coagula --query "…" | pbcopy`), or `git checkout v0.4.0`
  for the LLM-invoked `coagula-mcp` server, which isn't hook-policy gated.
- **Tool repeatedly re-reads the spill file.** Copilot CLI persists original
  output at `/tmp/copilot-tool-output-*.txt` (Windows: `%TEMP%\copilot-tool-output-*.txt`);
  the model may go fetch the raw blob if it doesn't trust the funneled
  version. Tune `COAGULA_QUERY` to be specific so the funneled result
  actually contains the signal the model is after.

### What's verified end-to-end (honest status)

- **macOS:** Library, CLI, Ollama, bash hooks in live Copilot CLI session
  — all verified locally. Counterbalanced n=4 bash-dump test shows
  −14.6% credits (see "When coagula actually helps"). Counterbalanced
  paginated-view tests on both GPT-5 and Sonnet 4.6 show ~0% delta
  (within noise) — the nudge + summary features fire correctly but
  current models ignore the injected hint text.
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
| `COAGULA_THRESHOLD` | Override per-tool default thresholds with one global number. Unset: per-tool defaults (bash 2000, view 500, MCP 1000, other 1500). | (per-tool table) |
| `COAGULA_CUMULATIVE_THRESHOLD` | Session-level token total above which sub-threshold calls also get funneled. Catches paginated `view_range`/`grep` patterns. 0 disables. | 8000 |
| `COAGULA_VIEW_NUDGE_AFTER` | After N view-tool calls per session, inject a one-time hint encouraging bash alternatives that compress (v0.7.0+). 0 disables. **Currently dormant**: tested models (GPT-5, Sonnet 4.6) ignore the hint. Costs nothing — kept for future model compatibility. | 4 |
| `COAGULA_SUMMARY_INJECT` | On first view of a file, if file content compresses well, prepend a coagula summary to that one response (v0.7.3+: one-shot, not every call). Set to `off` to disable. **Currently dormant** for the same reason as the nudge. | on |
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
from coagula import coagula_payload, ChunkSpec

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

**v0.7.3** (current) — Hardens the v0.7 view-mitigation features:

- **One-shot summary injection.** v0.7.0/v0.7.2 injected the cached
  file summary on *every* view response of the same file, adding ~180
  tokens × N calls. n=4 counterbalanced testing showed this *cost* +15%
  credits without changing model behavior. v0.7.3 injects the summary
  only on the first view of each file. Re-tested counterbalanced: now
  ~0% delta (within noise).
- **Defensive hook robustness.** Dropped `set -e` in favor of an EXIT
  trap that converts any unexpected exit to a clean `{}` passthrough +
  decision-log entry. Fixed an empirically-discovered jq-on-real-payload
  bug that caused 19/20 hook invocations to silently fail in real
  Copilot CLI sessions on v0.7.0/v0.7.1.
- **Schema-correct file-path extraction.** Copilot CLI passes the view
  tool's `toolArgs` as a JSON string (not an object); v0.7.2 fixed the
  extractor to handle both shapes so the summary path actually fires
  in production sessions.

**Honest read on the v0.7 mitigation story:** the nudge + summary
features ship and fire correctly, but **neither GPT-5 nor Claude
Sonnet 4.6 changes behavior in response to the injected hint text.**
They're dormant — costless after v0.7.3, but also savingless. They
remain in the codebase as kindling for when a model that heeds
`additionalContext`-style hints comes along, but provide no real value
today. **The actual measured value lives entirely in the v0.3-era
bash-dump compression path** (~15% credit savings on counterbalanced
real-session tests; see "When coagula actually helps" above).

Native PowerShell users get v0.6.0 features (per-tool thresholds +
cumulative tracking + decision logging); v0.7 injection features
require Git Bash (auto-detected by the PS hook when present).

21 hook integration tests validate the bash hook end-to-end with
synthetic Copilot CLI payloads, including the v0.7.2 string-shaped
toolArgs regression check and the v0.7.3 one-shot summary injection.

**v0.6.0** — Per-tool thresholds (bash 2000, view 500, MCP 1000) replace
the single global default — increases firing rate on paginated `view_range`
and small-tool-output patterns without harming response quality. Cumulative
session tracking catches death-by-a-thousand-cuts: once total tool-output
tokens cross `COAGULA_CUMULATIVE_THRESHOLD` (default 8000), subsequent
sub-threshold calls also funnel. Every postToolUse decision now writes a
debug-log line categorizing the verdict (`fired`/`under-threshold`/`skipped`
/etc.), so actual firing rate is finally observable — see "Reading the debug
log" above. 12 new integration tests validate the bash hook end-to-end
with synthetic Copilot CLI payloads.

**v0.5.x** scoped coagula exclusively to GitHub Copilot CLI via host hooks
(PowerShell on Windows, Bash on macOS/Linux/Git Bash). The seven-stage
funnel runs on stdlib alone; Azure OpenAI and Ollama are optional drop-in
backends. Claude Code / VSCode Copilot Chat hook integrations and the
`coagula-mcp` MCP server were removed to reduce maintenance surface for a
target audience that didn't benefit from them. The accuracy-preservation
eval harness and cache-stable mode shipped in v0.4.0 carry over.

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
