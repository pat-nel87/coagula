"""Relevance stage — SPEC §6.4.

Ranks RELEVANT-tier chunks against the query. Keeps top ``keep``; demotes the
rest to DEFERRED. Annotates every candidate with ``meta["relevance"]``.

Default scorer is dependency-free TF-IDF cosine. An optional ``embedder``
callable (``list[str] -> list[vector]``) replaces it.

Known limitation (documented in SPEC §6.4): TF-IDF mis-ranks on small corpora
and short critical lines. The correctness guarantee for must-keep content
comes from CRITICAL pinning at ingestion, not from this ranker — this stage
must therefore NEVER consider or demote CRITICAL chunks.
"""

from __future__ import annotations

import math
from typing import Callable

from ..stage import Chunk, Stage, Tier
from ._tfidf import rank_against_query


def _cosine_vec(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class Relevance(Stage):
    name = "relevance"

    def __init__(
        self,
        keep: int = 5,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
    ):
        self.keep = keep
        self.embedder = embedder

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        candidates = [c for c in chunks if c.tier == Tier.RELEVANT]
        if not candidates or len(candidates) <= self.keep:
            return chunks

        if self.embedder is not None:
            vecs = self.embedder([c.text for c in candidates] + [query])
            qv = vecs[-1]
            scores = [_cosine_vec(v, qv) for v in vecs[:-1]]
        else:
            scores = rank_against_query([c.text for c in candidates], query)

        for c, s in zip(candidates, scores):
            c.meta["relevance"] = float(s)

        # Demote losers to DEFERRED. Preserve original order; tier change is
        # the only mutation.
        ranked = sorted(
            range(len(candidates)), key=lambda i: scores[i], reverse=True
        )
        keep_ids = {id(candidates[i]) for i in ranked[: self.keep]}
        result: list[Chunk] = []
        for c in chunks:
            if c.tier == Tier.RELEVANT and id(c) not in keep_ids:
                result.append(
                    Chunk(text=c.text, kind=c.kind, source=c.source, tier=Tier.DEFERRED, meta=c.meta)
                )
            else:
                result.append(c)
        return result
