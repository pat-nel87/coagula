"""Budget + Assemble stages — SPEC §6.6 and §6.7.

Budget is the hard backstop: keep all CRITICAL chunks first (exempt from the
cap; surface a warning if criticals alone overflow), then greedily fill from
RELEVANT/SUMMARIZED by ``meta["relevance"]`` desc, demoting overflow to
DEFERRED.

Assemble collapses surviving (non-deferred) chunks into a single output
Chunk grouped by ``### {source}`` headers, then appends a deferred footer
listing distinct deferred sources and ids — the human/agent-visible half of
the RLM seam.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from ..stage import Chunk, Stage, Tier


def chunk_id(chunk: Chunk) -> str:
    """Stable id per SPEC §8: ``f"{source}:{sha1(text)[:8]}"``."""
    h = hashlib.sha1(chunk.text.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{chunk.source}:{h}"


class Budget(Stage):
    name = "budget"

    def __init__(self, max_tokens: int = 2000):
        self.max_tokens = max_tokens

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        cap = budget if budget else self.max_tokens

        criticals = [c for c in chunks if c.tier == Tier.CRITICAL]
        fillable = [
            c for c in chunks if c.tier in (Tier.RELEVANT, Tier.SUMMARIZED)
        ]
        # Pre-deferred chunks pass through.
        already_deferred = [c for c in chunks if c.tier == Tier.DEFERRED]

        critical_tokens = sum(c.tokens for c in criticals)
        if critical_tokens > cap:
            for c in criticals:
                c.meta["critical_overflow"] = True

        remaining = max(0, cap - critical_tokens)
        fillable_sorted = sorted(
            fillable,
            key=lambda c: c.meta.get("relevance", 0.0),
            reverse=True,
        )
        # Minimum room (in tokens) where it's still worth keeping a
        # head-of-content slice rather than dropping the chunk entirely.
        # Below this, the truncated text is too small to be informative,
        # so fall back to whole-demote.
        MIN_TRUNCATE_TOKENS = 100

        kept: list[Chunk] = []
        # Maps id(original) -> truncated replacement. Used to swap in the
        # reassembly loop below, which iterates over original chunks.
        truncation_replacements: dict[int, Chunk] = {}

        for c in fillable_sorted:
            if c.tokens <= remaining:
                kept.append(c)
                remaining -= c.tokens
            elif remaining >= MIN_TRUNCATE_TOKENS:
                # Truncate to fit instead of dropping whole. This is the
                # common case for single-chunk tool outputs (a file read, a
                # grep result) that don't have blank-line separators and
                # thus arrive as one giant chunk. Pre-fix, those collapsed
                # to just the deferred footer; the model saw nothing.
                # Approximate: chars/4 token estimate matches coagula.tokens
                # fallback. Conservative — we'd rather under-fill than blow
                # the budget by a few percent.
                max_chars = remaining * 4
                truncated_text = c.text[:max_chars].rstrip()
                omitted_tokens = c.tokens - remaining
                cid = chunk_id(c)
                truncated_text += (
                    f"\n\n[... +{omitted_tokens} tokens truncated. "
                    f"Full source retrievable as {cid}.]"
                )
                truncated = Chunk(
                    text=truncated_text,
                    kind=c.kind,
                    source=c.source,
                    tier=c.tier,
                    meta={
                        **c.meta,
                        "truncated": True,
                        "original_tokens": c.tokens,
                        "kept_tokens": remaining,
                    },
                )
                kept.append(truncated)
                truncation_replacements[id(c)] = truncated
                remaining = 0
                # Stash the original (untruncated) in the deferred set so
                # `retrieve(request_id, [cid])` returns the full text.
                already_deferred.append(
                    Chunk(
                        text=c.text,
                        kind=c.kind,
                        source=c.source,
                        tier=Tier.DEFERRED,
                        meta=dict(c.meta),
                    )
                )
            else:
                # Truly out of room — demote whole.
                already_deferred.append(
                    Chunk(
                        text=c.text,
                        kind=c.kind,
                        source=c.source,
                        tier=Tier.DEFERRED,
                        meta=dict(c.meta),
                    )
                )
        kept_ids = {id(c) for c in kept}

        # Reassemble in original order. Truncated fillables substitute in
        # for their originals at the same position so Assemble's
        # `### {source}` grouping reflects the input order.
        out: list[Chunk] = []
        for c in chunks:
            if c.tier == Tier.CRITICAL or c.tier == Tier.DEFERRED:
                out.append(c)
            elif id(c) in truncation_replacements:
                out.append(truncation_replacements[id(c)])
            elif id(c) in kept_ids:
                out.append(c)
            # else: it was whole-demoted to DEFERRED — added below.
        # Append the newly-deferred copies at the end so they stay live for
        # the assemble footer / store but don't affect grouping order.
        for c in already_deferred:
            if c not in chunks:
                out.append(c)
        return out


class Assemble(Stage):
    name = "assemble"

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        live = [c for c in chunks if c.tier != Tier.DEFERRED]
        deferred = [c for c in chunks if c.tier == Tier.DEFERRED]

        # Group live chunks by source, preserving first-seen order.
        groups: OrderedDict[str, list[Chunk]] = OrderedDict()
        for c in live:
            groups.setdefault(c.source, []).append(c)

        body_parts: list[str] = []
        for source, members in groups.items():
            body_parts.append(f"### {source}")
            for c in members:
                body_parts.append(c.text)

        body = "\n\n".join(body_parts)

        deferred_ids = [chunk_id(c) for c in deferred]
        deferred_sources = sorted({c.source for c in deferred})
        if deferred:
            footer = (
                f"\n\n### deferred ({len(deferred)} chunks)\n"
                f"Sources omitted: {', '.join(deferred_sources)}.\n"
                f"Retrievable on request by id."
            )
            body = body + footer

        meta = {
            "deferred": len(deferred),
            "deferred_ids": deferred_ids,
        }
        # Surface any critical-overflow warnings on the assembled chunk.
        if any(c.meta.get("critical_overflow") for c in live if c.tier == Tier.CRITICAL):
            meta["critical_overflow"] = True

        assembled = Chunk(
            text=body,
            kind="text",
            source="assembled",
            tier=Tier.CRITICAL,
            meta=meta,
        )
        return [assembled] + deferred
