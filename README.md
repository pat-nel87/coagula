# coagula

A local **context manicuring funnel**: trim, dedup, prune, rank, and compress
context *before* it reaches an expensive frontier LLM call, so most tokens are
killed by cheap deterministic logic and only what survives is spent on
inference.

Built primarily for noisy diagnostic payloads (kubectl JSON, crashloop logs,
Postgres stats, Azure ARM responses) but works on any context.

## Status

**M3 — full default funnel runnable end-to-end on stdlib only.** Seven stages
wired, CLI works, e2e test on the `noisy_mixed` fixture hits ≥99% token
reduction with the FATAL signal preserved. Deferred follow-ups: Ollama hooks
(M4), MCP adapter + deferred store (M5), sidecar/packaging polish (M6). See
`SPEC.md` for the full contract.

## Quick start

```bash
pip install -e ".[dev]"
pytest
python demo.py
python -m coagula.cli --query "why is the pod crashlooping" --report \
  --budget 800 --keep 4 tests/fixtures/noisy_mixed.txt
```

## Design

Stages run in fixed order, cheap-before-expensive:

```
normalize → dedup → prune → relevance → summarize → budget → assemble
```

Everything is **demote, not delete**: pruned chunks become `DEFERRED` and are
retrievable on demand. `CRITICAL` chunks are sacrosanct — never demoted, never
dropped.

See `SPEC.md` for the full design.
