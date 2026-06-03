"""coagula — local context manicuring funnel.

See SPEC.md for the full contract. M1 ships the data model and a zero-stage
runnable Funnel; the real stages and `default_funnel` land in M2/M3.
"""

from __future__ import annotations

from .stage import Chunk, Funnel, Stage, StageResult, Tier
from .tokens import count_tokens

__all__ = [
    "Chunk",
    "Funnel",
    "Stage",
    "StageResult",
    "Tier",
    "count_tokens",
    "default_funnel",
]


def default_funnel(
    max_tokens: int = 2000,
    keep: int = 5,
    embedder=None,
    llm=None,
) -> Funnel:
    """Build the fixed seven-stage default pipeline.

    Stub until M3. Will return a `Funnel` with stages in the order:
    normalize → dedup → prune → relevance → summarize → budget → assemble.
    """
    raise NotImplementedError("default_funnel lands in M3 — see SPEC §7")
