"""coagula — local context manicuring funnel.

See SPEC.md for the full contract. M3 ships the seven-stage default funnel,
runnable end-to-end with stdlib-only dependencies. The ``embedder`` and
``llm`` hooks accept optional callables; the funnel works without them.
"""

from __future__ import annotations

from .stage import Chunk, Funnel, Stage, StageResult, Tier
from .stages import (
    Assemble,
    Budget,
    Dedup,
    Normalize,
    Prune,
    Relevance,
    Summarize,
    chunk_id,
)
from .tokens import count_tokens

__all__ = [
    "Assemble",
    "Budget",
    "Chunk",
    "ChunkSpec",
    "CoagulaResult",
    "DeferredStore",
    "Dedup",
    "Funnel",
    "Normalize",
    "Prune",
    "Relevance",
    "Stage",
    "StageResult",
    "StoredChunk",
    "Summarize",
    "Tier",
    "chunk_id",
    "coagula_payload",
    "count_tokens",
    "default_funnel",
]


def default_funnel(
    max_tokens: int = 2000,
    keep: int = 5,
    embedder=None,
    llm=None,
    json_denylist: set[str] | None = None,
    max_array: int = 10,
) -> Funnel:
    """Build the fixed seven-stage default pipeline (SPEC §7).

    Order: normalize → dedup → prune → relevance → summarize → budget → assemble.

    Stage order is fixed and deterministic. The ``embedder`` / ``llm`` hooks
    are optional — if absent, Relevance falls back to TF-IDF and Summarize
    falls back to extractive selection. ``json_denylist`` defaults to empty
    (per-tool denylists are an M5 concern).
    """
    return Funnel(
        [
            Normalize(),
            Dedup(),
            Prune(denylist=json_denylist or set(), max_array=max_array),
            Relevance(keep=keep, embedder=embedder),
            Summarize(llm=llm),
            Budget(max_tokens=max_tokens),
            Assemble(),
        ]
    )


# Library API re-exports. Defined after ``default_funnel`` because
# ``payload`` imports it at module-load time — keeps the import graph acyclic.
from .payload import ChunkSpec, CoagulaResult, coagula_payload  # noqa: E402
from .store import DeferredStore, StoredChunk  # noqa: E402
