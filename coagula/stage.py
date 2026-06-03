"""Core data model and Funnel runner.

Per SPEC §4. Stages operate on lists of `Chunk`. Each stage demotes (never
deletes). The Funnel records per-stage token/chunk accounting, counting only
*live* chunks (tier != DEFERRED) so the report reflects real prompt size.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Iterable

from .tokens import count_tokens


class Tier(IntEnum):
    CRITICAL = 0    # never dropped (severity-pinned signal: FATAL/ERROR, the query target)
    RELEVANT = 1    # survived relevance ranking
    SUMMARIZED = 2  # kept only as a compressed form
    DEFERRED = 3    # removed from prompt, retrievable on demand


@dataclass
class Chunk:
    text: str
    kind: str = "text"        # one of: text | log | json | code
    source: str = "unknown"
    tier: Tier = Tier.RELEVANT
    meta: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return count_tokens(self.text)

    def with_text(self, text: str) -> "Chunk":
        return replace(self, text=text, meta=dict(self.meta))


@dataclass
class StageResult:
    name: str
    tokens_in: int
    tokens_out: int
    chunks_in: int
    chunks_out: int

    @property
    def saved(self) -> int:
        return self.tokens_in - self.tokens_out

    @property
    def ratio(self) -> float:
        if self.tokens_in == 0:
            return 0.0
        return self.saved / self.tokens_in


class Stage:
    name: str = "stage"

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        raise NotImplementedError


def _live(chunks: Iterable[Chunk]) -> list[Chunk]:
    return [c for c in chunks if c.tier != Tier.DEFERRED]


def _measure(chunks: Iterable[Chunk]) -> tuple[int, int]:
    live = _live(chunks)
    return sum(c.tokens for c in live), len(live)


class Funnel:
    def __init__(self, stages: list[Stage]):
        self.stages = stages
        self.results: list[StageResult] = []

    def run(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        self.results = []
        current = list(chunks)
        for stage in self.stages:
            t_in, c_in = _measure(current)
            current = stage.process(current, query, budget)
            t_out, c_out = _measure(current)
            self.results.append(
                StageResult(
                    name=stage.name,
                    tokens_in=t_in,
                    tokens_out=t_out,
                    chunks_in=c_in,
                    chunks_out=c_out,
                )
            )
        return current

    def report(self) -> str:
        header = f"{'stage':<14} {'tok_in':>8} {'tok_out':>8} {'saved':>8} {'ratio':>7} {'ch_in':>6} {'ch_out':>6}"
        rows = [header, "-" * len(header)]
        total_in = self.results[0].tokens_in if self.results else 0
        total_out = self.results[-1].tokens_out if self.results else 0
        for r in self.results:
            rows.append(
                f"{r.name:<14} {r.tokens_in:>8} {r.tokens_out:>8} {r.saved:>8} {r.ratio:>7.1%} {r.chunks_in:>6} {r.chunks_out:>6}"
            )
        rows.append("-" * len(header))
        total_saved = total_in - total_out
        total_ratio = (total_saved / total_in) if total_in else 0.0
        rows.append(
            f"{'TOTAL':<14} {total_in:>8} {total_out:>8} {total_saved:>8} {total_ratio:>7.1%}"
        )
        return "\n".join(rows)
