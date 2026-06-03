"""DeferredStore — per-request store of demoted chunks. See SPEC §8.

The funnel's correctness guarantee is "demote, never delete": any chunk that
falls out of the assembled prompt is retrievable later via this store. The
default backend is an in-memory dict with lazy TTL eviction. The interface
deliberately matches what a Redis-backed swap-in would expose so the sidecar
deployment (M6) can share storage without changing callers.
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


class DeferredStore:
    """In-memory ``request_id -> {chunk_id: StoredChunk}`` with TTL eviction.

    Eviction is lazy: every public method first sweeps expired entries for
    the request_id it's working with. No background thread.
    """

    def __init__(self, ttl_seconds: float = 3600.0):
        self.ttl_seconds = ttl_seconds
        self._data: dict[str, dict[str, StoredChunk]] = {}

    def put(self, request_id: str, chunks: list[Chunk]) -> None:
        self._evict(request_id)
        bucket = self._data.setdefault(request_id, {})
        now = time.time()
        for c in chunks:
            bucket[chunk_id(c)] = StoredChunk(chunk=c, stored_at=now)

    def retrieve(self, request_id: str, ids: list[str]) -> list[Chunk]:
        self._evict(request_id)
        bucket = self._data.get(request_id)
        if not bucket:
            return []
        return [bucket[i].chunk for i in ids if i in bucket]

    def manifest(self, request_id: str) -> list[dict]:
        self._evict(request_id)
        bucket = self._data.get(request_id, {})
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
        """Force a full eviction sweep. Returns count of removed chunks."""
        now = time.time()
        removed = 0
        for rid in list(self._data.keys()):
            bucket = self._data[rid]
            stale = [
                cid for cid, s in bucket.items() if now - s.stored_at > self.ttl_seconds
            ]
            for cid in stale:
                del bucket[cid]
            removed += len(stale)
            if not bucket:
                del self._data[rid]
        return removed

    def _evict(self, request_id: str) -> None:
        bucket = self._data.get(request_id)
        if not bucket:
            return
        now = time.time()
        stale = [
            cid for cid, s in bucket.items() if now - s.stored_at > self.ttl_seconds
        ]
        for cid in stale:
            del bucket[cid]
        if not bucket:
            del self._data[request_id]
