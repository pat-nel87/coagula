"""DeferredStore tests per SPEC §8."""

from __future__ import annotations

import time

from coagula import Chunk
from coagula.mcp import DeferredStore
from coagula.stages import chunk_id


def test_put_and_retrieve_roundtrip():
    s = DeferredStore()
    chunks = [
        Chunk(text="alpha", source="src/a"),
        Chunk(text="beta", source="src/b"),
    ]
    s.put("req-1", chunks)
    ids = [chunk_id(c) for c in chunks]
    got = s.retrieve("req-1", ids)
    assert {g.text for g in got} == {"alpha", "beta"}


def test_retrieve_unknown_ids_returns_empty():
    s = DeferredStore()
    s.put("req-1", [Chunk(text="x", source="s")])
    assert s.retrieve("req-1", ["bogus:00000000"]) == []


def test_retrieve_unknown_request_returns_empty():
    s = DeferredStore()
    assert s.retrieve("nonexistent", ["whatever"]) == []


def test_manifest_shape():
    s = DeferredStore()
    chunks = [
        Chunk(text="abc", kind="text", source="src/a"),
        Chunk(text='{"k": 1}', kind="json", source="src/b"),
    ]
    s.put("req-1", chunks)
    m = s.manifest("req-1")
    assert len(m) == 2
    for entry in m:
        assert set(entry.keys()) == {"id", "source", "kind", "tokens"}
        assert entry["tokens"] > 0


def test_ttl_eviction(monkeypatch):
    base = 1000.0
    now = [base]

    def fake_time():
        return now[0]

    monkeypatch.setattr(time, "time", fake_time)

    s = DeferredStore(ttl_seconds=60.0)
    s.put("req-1", [Chunk(text="x", source="s")])
    assert len(s.manifest("req-1")) == 1

    now[0] = base + 30  # within TTL
    assert len(s.manifest("req-1")) == 1

    now[0] = base + 120  # past TTL
    assert s.manifest("req-1") == []
    assert s.retrieve("req-1", [chunk_id(Chunk(text="x", source="s"))]) == []


def test_evict_expired_returns_count(monkeypatch):
    base = 1000.0
    now = [base]
    monkeypatch.setattr(time, "time", lambda: now[0])

    s = DeferredStore(ttl_seconds=60.0)
    s.put("req-1", [Chunk(text=f"c{i}", source="s") for i in range(3)])
    now[0] = base + 200
    assert s.evict_expired() == 3


def test_put_replaces_existing_id():
    """Same source + same text → same chunk_id → put twice should not double-count."""
    s = DeferredStore()
    c = Chunk(text="same", source="src/a")
    s.put("req-1", [c, c])
    # Two identical chunks → one unique id → manifest length 1.
    assert len(s.manifest("req-1")) == 1
