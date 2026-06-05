"""Library API for embedding coagula in your own tools.

Provides ``coagula_payload(ChunkSpec, query, ...)`` plus the deferred-chunk
store. Named ``mcp`` for historical reasons — the standalone MCP server
that previously lived here was removed in v0.5.0. See SPEC §8 (deferred
tier / retrieval contract) and §9 (adapter contract).
"""

from __future__ import annotations

from .adapter import ChunkSpec, CoagulaResult, coagula_payload
from .store import DeferredStore, StoredChunk

__all__ = [
    "ChunkSpec",
    "CoagulaResult",
    "DeferredStore",
    "StoredChunk",
    "coagula_payload",
]
