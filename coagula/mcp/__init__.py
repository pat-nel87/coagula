"""MCP integration: adapter library + deferred store + standalone server.

See SPEC §8 (deferred tier / retrieval contract) and §9 (MCP integration).
The standalone server lives in `coagula.mcp.server` and is gated behind the
`mcp` optional extra (`pip install coagula[mcp]`).
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
