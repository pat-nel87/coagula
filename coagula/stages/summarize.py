"""Summarize stage — SPEC §6.5.

The only stage permitted a model call. Default is *extractive*: split text
into sentences, score each by a 50/50 blend of TF-IDF relevance to the query
and intra-doc centrality, keep top ``keep_frac`` (preserve original order).

Skips ``json``/``code`` chunks (structure matters) and chunks below
``min_tokens``. On success, sets ``tier = SUMMARIZED`` and
``meta["summarized"] = True``.

If an ``llm`` callable (``str -> str``) is supplied, use it for abstractive
compression instead. The funnel never requires it.
"""

from __future__ import annotations

import re
from typing import Callable

from ..stage import Chunk, Stage, Tier
from ._tfidf import centrality_scores, rank_against_query

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|\n+")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(text)]
    return [p for p in parts if p]


def _extractive(text: str, query: str, keep_frac: float) -> str:
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return text
    rel = rank_against_query(sentences, query)
    cen = centrality_scores(sentences)
    scores = [0.5 * r + 0.5 * c for r, c in zip(rel, cen)]
    keep_n = max(1, int(round(len(sentences) * keep_frac)))
    top_idx = sorted(
        sorted(range(len(sentences)), key=lambda i: scores[i], reverse=True)[:keep_n]
    )
    return " ".join(sentences[i] for i in top_idx)


class Summarize(Stage):
    name = "summarize"

    def __init__(
        self,
        min_tokens: int = 120,
        keep_frac: float = 0.4,
        llm: Callable[[str], str] | None = None,
    ):
        self.min_tokens = min_tokens
        self.keep_frac = keep_frac
        self.llm = llm

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        result: list[Chunk] = []
        for c in chunks:
            if (
                c.tier != Tier.RELEVANT
                or c.kind not in ("text", "log")
                or c.tokens < self.min_tokens
            ):
                result.append(c)
                continue
            if self.llm is not None:
                prompt = (
                    f"Compress the following text to its bare-minimum relevant "
                    f"facts for the query: {query!r}. Preserve numeric values, "
                    f"error codes, and identifiers verbatim.\n\n{c.text}"
                )
                summary = self.llm(prompt)
            else:
                summary = _extractive(c.text, query, self.keep_frac)
            new_meta = dict(c.meta)
            new_meta["summarized"] = True
            result.append(
                Chunk(
                    text=summary,
                    kind=c.kind,
                    source=c.source,
                    tier=Tier.SUMMARIZED,
                    meta=new_meta,
                )
            )
        return result
