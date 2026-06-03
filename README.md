# coagula

A local **context manicuring funnel**: trim, dedup, prune, rank, and compress
context *before* it reaches an expensive frontier LLM call, so most tokens are
killed by cheap deterministic logic and only what survives is spent on
inference.

Built primarily for noisy diagnostic payloads (kubectl JSON, crashloop logs,
Postgres stats, Azure ARM responses) but works on any context.

## Status

**M1 — core scaffold only.** Data model and zero-stage funnel runner exist.
Real stages and the CLI land in M2/M3. See `SPEC.md` for the full contract and
`/Users/patnel87/.claude/plans/sharded-sauteeing-axolotl.md` for the build
plan.

## Quick start (once M3 lands)

```bash
pip install -e .
python -m coagula.cli --query "why is the pod crashlooping" --report \
  < tests/fixtures/noisy_mixed.txt
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
