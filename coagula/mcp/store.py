"""DeferredStore — per-request store of demoted chunks. See SPEC §8.

The funnel's correctness guarantee is "demote, never delete": any chunk that
falls out of the assembled prompt is retrievable later via this store. The
default backend is an in-memory dict with lazy TTL eviction. The interface
deliberately matches what a Redis-backed swap-in would expose so the sidecar
deployment (M6) can share storage without changing callers.

Workspace scoping (v0.3.11): every put/retrieve/manifest call accepts an
optional ``workspace_key`` to scope storage by project / session identity.
Defaults to ``""`` for backward compat with callers that don't supply one.
The MCP server resolves the workspace from ``COAGULA_WORKSPACE_KEY`` env
or the process CWD at startup, so any long-lived server process serving
multiple workspaces won't cross-leak chunks via colliding request_ids.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..stage import Chunk
from ..stages import chunk_id


@dataclass
class StoredChunk:
    chunk: Chunk
    stored_at: float


# Internal storage key: (workspace_key, request_id). Tuple ensures cross-
# workspace request_id collisions can't reach each other's buckets.
_StoreKey = tuple[str, str]


class DeferredStore:
    """In-memory ``(workspace_key, request_id) -> {chunk_id: StoredChunk}`` with TTL eviction.

    Eviction is lazy: every public method first sweeps expired entries for
    the (workspace_key, request_id) it's working with. No background thread.

    ``workspace_key`` defaults to ``""`` so existing callers (single-process,
    single-workspace MCP server users) work without modification. Multi-
    workspace deployments should set it explicitly per call.
    """

    def __init__(self, ttl_seconds: float = 3600.0):
        self.ttl_seconds = ttl_seconds
        self._data: dict[_StoreKey, dict[str, StoredChunk]] = {}

    def put(
        self,
        request_id: str,
        chunks: list[Chunk],
        *,
        workspace_key: str = "",
    ) -> None:
        key = (workspace_key, request_id)
        self._evict(key)
        bucket = self._data.setdefault(key, {})
        now = time.time()
        for c in chunks:
            bucket[chunk_id(c)] = StoredChunk(chunk=c, stored_at=now)

    def retrieve(
        self,
        request_id: str,
        ids: list[str],
        *,
        workspace_key: str = "",
    ) -> list[Chunk]:
        key = (workspace_key, request_id)
        self._evict(key)
        bucket = self._data.get(key)
        if not bucket:
            return []
        return [bucket[i].chunk for i in ids if i in bucket]

    def manifest(
        self,
        request_id: str,
        *,
        workspace_key: str = "",
    ) -> list[dict]:
        key = (workspace_key, request_id)
        self._evict(key)
        bucket = self._data.get(key, {})
        return [
            {
                "id": cid,
                "source": stored.chunk.source,
                "kind": stored.chunk.kind,
                "tokens": stored.chunk.tokens,
            }
            for cid, stored in bucket.items()
        ]

    def evict_expired(self) -> int:
        """Force a full eviction sweep across all workspace/request buckets.

        Returns count of removed chunks.
        """
        now = time.time()
        removed = 0
        for store_key in list(self._data.keys()):
            bucket = self._data[store_key]
            stale = [
                cid for cid, s in bucket.items() if now - s.stored_at > self.ttl_seconds
            ]
            for cid in stale:
                del bucket[cid]
            removed += len(stale)
            if not bucket:
                del self._data[store_key]
        return removed

    def _evict(self, store_key: _StoreKey) -> None:
        bucket = self._data.get(store_key)
        if not bucket:
            return
        now = time.time()
        stale = [
            cid for cid, s in bucket.items() if now - s.stored_at > self.ttl_seconds
        ]
        for cid in stale:
            del bucket[cid]
        if not bucket:
            del self._data[store_key]
