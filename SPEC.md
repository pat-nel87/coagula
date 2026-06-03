# `coagula` — Build Specification

> **Note on naming.** This spec was originally written for a package called
> `manicure`. The implementation in this repo uses the name `coagula` instead.
> Wherever the spec below says `manicure` (package dir, `python -m manicure.cli`,
> `manicure_payload`, etc.), read `coagula`. Internal module names
> (`tokens.py`, `stage.py`, `stages/`, `config.py`, `mcp/`, `models/`) are
> unchanged. The behavioral contract below is unchanged.

---

# `manicure` — Build Specification

A local **context manicuring funnel**: a pipeline that trims, deduplicates,
prunes, ranks, and compresses context *before* it reaches an expensive
frontier LLM call, so most tokens are killed by cheap deterministic logic and
only what survives is spent on inference. Built primarily for noisy diagnostic
payloads (kubectl JSON, crashloop logs, Postgres stats, Azure ARM responses)
but works on any context.

This document is the contract. Implement to the behavior and interfaces below;
acceptance tests in §11 define "done". Where this spec gives a code signature,
match it exactly so the CLI, adapters, and tests line up.

---

## 1. Goals & non-goals

**Goals**
- Reduce token count of a context payload by ≥90% on typical diagnostic input
  with near-zero loss of the signal needed to answer a query.
- Run fully **locally and offline**. No network call is required at inference
  time. Optional local models (Ollama) improve quality but are never mandatory.
- Be **embeddable as a library** inside an MCP tool, and additionally exposable
  as a thin sidecar (see §9, optional milestone).
- Make context loss **recoverable**: pruned content is demoted, not deleted,
  and is retrievable on demand (the RLM / `deep_analysis` seam, §8).

**Non-goals**
- Not a vector DB or a long-term RAG store. The funnel is stateless per request
  except for the optional deferred-chunk cache in §8.
- Not a replacement for the frontier model's own reasoning. It shapes input; it
  does not answer the query.
- No GPU requirement. No cloud inference. No telemetry/network egress.

---

## 2. Design principles (invariants — do not violate)

1. **Context is typed chunks, not one string.** Every stage operates on a list
   of `Chunk` objects carrying provenance and kind, which is what lets each
   stage apply the right logic.
2. **Demote, never delete.** Stages move chunks down a priority ladder
   (`CRITICAL > RELEVANT > SUMMARIZED > DEFERRED`). Deferred chunks leave the
   prompt but remain retrievable. Nothing is silently lost.
3. **`CRITICAL` is sacrosanct.** A chunk pinned `CRITICAL` must never be demoted
   by any stage, must never be dropped by the budget stage, and must always
   appear in the assembled prompt. This is the correctness guarantee that
   compensates for an imperfect relevance ranker.
4. **Cheap before expensive, lossless before lossy.** Stage order is fixed:
   normalize → dedup → prune → relevance → summarize → budget → assemble.
   Deterministic lossless stages run first; the only stage permitted a model
   call is `summarize`; `budget` is the hard backstop that guarantees fit.
5. **Graceful degradation.** Missing `tiktoken` → char-heuristic token count.
   Missing embedder → TF-IDF ranking. Missing `llm` → extractive summarization.
   The funnel always runs.
6. **Observability is mandatory.** The funnel records tokens in/out and chunk
   counts per stage so savings are attributable to a stage, not a black box.

---

## 3. Package layout

```
manicure/
  manicure/
    __init__.py        # public API + default_funnel()
    tokens.py          # token counting (tiktoken + fallback)
    stage.py           # Chunk, Tier, Stage, StageResult, Funnel
    config.py          # per-tool denylists & defaults (NEW)
    stages/
      __init__.py
      normalize.py
      dedup.py
      prune.py
      relevance.py
      summarize.py
      budget.py        # Budget + Assemble
    models/            # optional local-model adapters (NEW)
      __init__.py
      ollama.py        # embedder + llm callables backed by Ollama
    mcp/               # MCP integration (NEW)
      __init__.py
      adapter.py       # build Chunks from tool output; run; return result
      store.py         # deferred-chunk store + retrieve()
  tests/
    fixtures/
      kubectl_pod.json
      crashloop.log
      pg_stat.json
      noisy_mixed.txt
    test_tokens.py
    test_stages.py
    test_funnel.py
    test_mcp.py
  demo.py
  pyproject.toml
  README.md
```

---

## 4. Core data model (`stage.py`)

```python
class Tier(IntEnum):
    CRITICAL   = 0   # never dropped (severity-pinned signal: FATAL/ERROR, the query target)
    RELEVANT   = 1   # survived relevance ranking
    SUMMARIZED = 2   # kept only as a compressed form
    DEFERRED   = 3   # removed from prompt, retrievable on demand

@dataclass
class Chunk:
    text: str
    kind: str = "text"        # one of: text | log | json | code
    source: str = "unknown"   # provenance label, e.g. "kubectl/pod", "logs/payments"
    tier: Tier = Tier.RELEVANT
    meta: dict = field(default_factory=dict)   # stage annotations: relevance score, summarized flag, etc.

    @property
    def tokens(self) -> int: ...              # delegates to tokens.count_tokens
    def with_text(self, text: str) -> "Chunk": ...   # immutable copy with new text

@dataclass
class StageResult:
    name: str
    tokens_in: int; tokens_out: int
    chunks_in: int; chunks_out: int
    # properties: saved -> int, ratio -> float (fraction of tokens_in removed)

class Stage:
    name: str
    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]: ...

class Funnel:
    def __init__(self, stages: list[Stage]): ...
    def run(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]: ...
    def report(self) -> str: ...   # formatted per-stage + total table
    results: list[StageResult]      # populated by run()
```

**`Funnel.run` contract.** For each stage: measure live token count and live
chunk count, call `stage.process`, record a `StageResult`, pass output to the
next stage. "Live" = every chunk whose tier is not `DEFERRED`. Token accounting
must only count live chunks so the report reflects real prompt size, not the
deferred reserve.

---

## 5. Token counting (`tokens.py`)

```python
def count_tokens(text: str) -> int
```

- Use `tiktoken` `cl100k_base` if importable (a portable proxy; not Claude's
  tokenizer, but fine for *relative* budgeting).
- If `tiktoken` is absent, fall back to `max(1, len(text) // 4)`.
- Cache the encoder; never raise. A `count_real_tokens(text, model)` helper that
  calls the Anthropic token-count endpoint may be added but must be optional and
  network-gated — never on the default path.

---

## 6. Stage specifications

Stage order in `default_funnel` is fixed. Each stage only touches chunks of the
kinds/tiers it is responsible for and passes everything else through unchanged.

### 6.1 `Normalize`
- Strip ANSI escape sequences (`\x1b[...m`), carriage returns, collapse runs of
  intra-line whitespace, trim trailing whitespace, collapse 3+ blank lines to 2.
- Applies to all chunks. Lossless w.r.t. meaning. Idempotent (running twice ==
  once — this is a test).

### 6.2 `Dedup`  *(biggest win on logs)*
- Applies to `kind == "log"` only.
- Form a **template key** per line by masking volatile tokens: ISO-8601
  timestamps → `<TS>`, hex blobs (≥8 hex chars) → `<HEX>`, bare integers →
  `<N>`. Group consecutive-or-not identical templates, keep the first exemplar,
  collapse to `exemplar   (xN)` when `N > 1`. Preserve first-seen order.
- Must turn 4000 near-identical crashloop lines into 1 line + `(x4000)`.

### 6.3 `Prune`  *(biggest win on kubectl JSON)*
- Applies to `kind == "json"` only. Parse; if parse fails, pass through
  untouched (never corrupt input).
- Recursively drop keys in a configurable denylist (default = k8s set in §10).
- Truncate arrays longer than `MAX_ARRAY` (default 10), appending a
  `"<+K more items omitted>"` marker element.
- Re-serialize compactly (`indent=1`).

### 6.4 `Relevance`
- Rank `RELEVANT`-tier chunks against `query`; keep top `keep`, demote the rest
  to `DEFERRED`. Annotate every candidate with `meta["relevance"]`.
- Default scorer: **dependency-free TF-IDF cosine** (tokenize on
  `[a-z0-9_]+`, tf-idf vectors, cosine vs query vector).
- If an `embedder` callable is supplied (`list[str] -> list[vector]`), use it
  and rank by cosine in embedding space instead.
- **Known limitation to document in code:** TF-IDF mis-ranks on small corpora
  and short critical lines. It is a fallback. Correctness for must-keep content
  comes from `CRITICAL` pinning at ingestion (§8), not from this ranker. The
  stage must therefore never consider or demote `CRITICAL` chunks.
- If candidate count ≤ `keep`, no-op.

### 6.5 `Summarize`  *(only stage allowed a model call)*
- Targets `RELEVANT`-tier chunks with `tokens >= min_tokens` (default 120) and
  `kind in ("text", "log")`. Never summarize `json` or `code` (structure
  matters). On success, set tier to `SUMMARIZED` and `meta["summarized"]=True`.
- Default: **extractive** — split into sentences, score each by a 50/50 blend
  of TF-IDF relevance to the query and intra-doc centrality, keep top
  `keep_frac` (default 0.4), preserve original order.
- If an `llm` callable is supplied (`str -> str`), use it for abstractive
  compression with a terse prompt that names the query. Backed by a small local
  model (Ollama `llama3.2:3b` / `qwen2.5:3b` / `phi3`).

### 6.6 `Budget`  *(hard backstop)*
- Guarantee the live prompt fits `max_tokens`. Always keep all `CRITICAL`
  chunks first (they are exempt from the cap; if criticals alone exceed budget,
  keep them anyway and surface a warning in `meta`). Then greedily fill from
  `RELEVANT`/`SUMMARIZED` ordered by `meta["relevance"]` descending; demote
  whatever doesn't fit to `DEFERRED`.

### 6.7 `Assemble`
- Collapse surviving (non-deferred) chunks into a single output `Chunk`
  (`source="assembled"`, `tier=CRITICAL`). Group body by `source` with
  `### {source}` headers.
- Append a **deferred footer**: `### deferred (N chunks)` listing the distinct
  sources omitted and a note that they are retrievable on request. Record
  `meta["deferred"]` = count and `meta["deferred_ids"]` = list of chunk ids
  (see §8). The footer is the human/agent-visible half of the RLM seam.

---

## 7. Public API (`__init__.py`)

```python
def default_funnel(max_tokens: int = 2000, keep: int = 5,
                   embedder=None, llm=None) -> Funnel
```

Builds the fixed seven-stage pipeline. Exports: `Chunk, Tier, Stage,
StageResult, Funnel`, all stage classes, `default_funnel`.

CLI (`python -m manicure.cli`): reads a file arg or stdin, chunks by
blank-line-separated blocks with kind auto-detection (json/log/code/text),
runs `default_funnel`, prints the assembled prompt to stdout and (with
`--report`) the stage table to stderr. Flags: `--query` (required),
`--budget`, `--keep`, `--report`.

---

## 8. Deferred tier & retrieval contract (`mcp/store.py`) — the RLM seam

This is what makes the funnel safe for diagnostics: a relevance miss or a tight
budget never destroys data, because deferred chunks are recoverable.

- Every chunk gets a stable `id` (e.g. `f"{source}:{sha1(text)[:8]}"`) assigned
  at ingestion. `Assemble` records deferred ids in `meta["deferred_ids"]`.
- `DeferredStore` holds deferred chunks keyed by a per-request `request_id`:
  ```python
  class DeferredStore:
      def put(self, request_id: str, chunks: list[Chunk]) -> None
      def retrieve(self, request_id: str, ids: list[str]) -> list[Chunk]
      def manifest(self, request_id: str) -> list[dict]   # id, source, kind, tokens
  ```
  Default backend: in-memory dict with TTL eviction. Implementation must allow a
  swap-in shared backend (e.g. Redis) for the sidecar deployment without
  changing the interface.
- This backs a `deep_analysis` opt-in: when the frontier model (or the user)
  requests more detail, an MCP tool calls `retrieve(request_id, ids)` to pull
  specific deferred chunks back, optionally re-running a lightweight funnel over
  just those. The top-level prompt stays lean; detail is fetched on demand.

---

## 9. MCP integration (`mcp/adapter.py`)

Each diagnostic tool (kube-doctor, pg-doctor, Azure MCP server) builds chunks
from its own structured output rather than relying on CLI auto-detection,
because the tool *knows* the kind, source, and severity at parse time.

```python
def manicure_payload(
    sources: list[ChunkSpec],      # (text, kind, source, severity) tuples
    query: str,
    max_tokens: int = 2000,
    keep: int = 5,
    embedder=None, llm=None,
    request_id: str | None = None,
    store: DeferredStore | None = None,
) -> ManicureResult:               # .prompt, .request_id, .deferred_manifest, .report
```

**Severity pinning (critical for correctness).** The adapter maps log severity
to tier at ingestion: `FATAL`/`ERROR` and the explicit query target → `CRITICAL`;
everything else → `RELEVANT`. This is more reliable than any ranker and is the
intended primary mechanism for guaranteeing the root-cause signal survives.

**Deployment.** Default is **library-embedded**: each tool imports `manicure`
and calls `manicure_payload` before returning context to the model. An optional
later milestone wraps the same function in a thin local HTTP server (the AKS
sidecar) sharing one `DeferredStore` and one set of denylist configs across all
tools. The library API is identical in both modes; the sidecar is a transport,
not a rewrite. *(Open decision left to Patrick: embedded vs sidecar. Build
library-first; the sidecar is additive.)*

---

## 10. Per-tool configuration (`config.py`)

Denylists are config, not code. Ship three; make it trivial to add more.

- **k8s** (default for `kube-doctor`): `managedFields, resourceVersion, uid,
  generation, creationTimestamp, selfLink, ownerReferences, finalizers,
  annotations, labels`, plus `status.conditions[].lastTransitionTime`.
- **postgres** (`pg-doctor`): drop high-cardinality bookkeeping columns from
  `pg_stat_*` dumps (e.g. per-backend pids, transient timestamps) while keeping
  rate/aggregate fields; cap row arrays.
- **azure** (Azure MCP server): strip ARM envelope cruft — `systemData`, `etag`,
  `provisioningState` history, resource `id` GUIDs where a name exists.

`Prune` takes a denylist set; `manicure_payload` selects the set by a `profile`
argument (`"k8s" | "postgres" | "azure"`), defaulting to a passthrough profile.

---

## 11. Acceptance criteria (build `tests/` to assert these)

**Tokens**
- `count_tokens` returns >0 for non-empty text; falls back without `tiktoken`
  installed (test by monkeypatching the import to fail).

**Stages**
- *Normalize* is idempotent: `normalize(normalize(x)) == normalize(x)`.
- *Dedup* collapses a fixture of N identical-modulo-timestamp log lines to a
  single line carrying `(xN)`; reduction on `crashloop.log` fixture ≥ 95%.
- *Prune* removes every denylisted key at any depth; arrays > `MAX_ARRAY` are
  truncated with a marker; invalid JSON passes through byte-identical.
- *Relevance* never changes a `CRITICAL` chunk's tier; with `keep < candidates`,
  exactly `candidates - keep` chunks become `DEFERRED`.
- *Summarize* leaves `json`/`code` chunks untouched; reduces a long text chunk's
  token count; sets `SUMMARIZED` tier.
- *Budget* postcondition: sum of live `RELEVANT|SUMMARIZED` tokens ≤ `max_tokens`
  (criticals exempt). No `CRITICAL` chunk is ever demoted.

**Funnel**
- Report reconciliation: for adjacent stages, `results[i].tokens_out ==
  results[i+1].tokens_in`.
- End-to-end on `noisy_mixed.txt` fixture (mirrors the demo: bloated kubectl
  JSON + 4000-line crashloop + a short FATAL line pinned CRITICAL + irrelevant
  noise + a verbose runbook): total reduction ≥ 99%, and the FATAL signal string
  is present in the assembled prompt regardless of ranker behavior.
- `Assemble` returns exactly one chunk; deferred footer lists all deferred
  sources; `meta["deferred_ids"]` matches what `DeferredStore` holds.

**MCP**
- `manicure_payload` pins `ERROR`/`FATAL` sources to `CRITICAL`.
- `store.retrieve(request_id, ids)` returns the exact deferred chunks;
  `manifest` lists id/source/kind/tokens for each.

---

## 12. Build milestones (suggested order — each independently testable)

1. **M1 Core.** `tokens.py`, `stage.py` (`Chunk`/`Tier`/`Stage`/`StageResult`/
   `Funnel`), `__init__` skeleton. Funnel runs with zero stages; report works.
2. **M2 Lossless stages.** `Normalize`, `Dedup`, `Prune` + their tests. These
   carry ~all of the token savings; get them solid first.
3. **M3 Full default funnel.** `Relevance` (TF-IDF), `Summarize` (extractive),
   `Budget`, `Assemble`, CLI. End-to-end reduction test passes.
4. **M4 Local models.** `models/ollama.py` embedder + llm callables; wire the
   `embedder`/`llm` hooks. Funnel must still pass all M3 tests with them absent.
5. **M5 MCP.** `mcp/store.py` (`DeferredStore`), `mcp/adapter.py`
   (`manicure_payload`, severity pinning, profiles), `config.py` denylists.
6. **M6 Packaging & sidecar (optional).** `pyproject.toml`, README, and the thin
   HTTP wrapper sharing one store + configs.

---

## 13. Dependencies & constraints

- **Runtime:** Python ≥ 3.10, standard library only on the default path.
- **Optional:** `tiktoken` (token accuracy); an Ollama install + client for
  `models/ollama.py` (embeddings/summarization). Both optional; absence triggers
  documented fallbacks.
- **Hard constraints:** no network egress on the default funnel path; no browser
  storage; deterministic output given the same input and no model hooks; stages
  must be cheap relative to the frontier call they protect.
- **Style:** type hints throughout; dataclasses for data; each stage in its own
  module under `stages/`; docstrings stating each stage's responsibility,
  applicable kinds/tiers, and any known limitation.

---

## 14. Out of scope (do not build now)

- Vector database / persistent embedding index.
- Cross-request learning or caching beyond the deferred store.
- A UI. Observability is the `report()` table and the deferred manifest only.
- Real Anthropic-tokenizer counting on the default path (optional helper only).
