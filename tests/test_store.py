"""DeferredStore tests per SPEC §8."""

from __future__ import annotations

import time

from coagula import Chunk, DeferredStore
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


# ---------------------------------------------------------------------------
# Workspace scoping (v0.3.11)
# ---------------------------------------------------------------------------


def test_cross_workspace_retrieve_returns_empty():
    """Chunks stored under workspace A must NOT be retrievable from workspace B
    even if the request_id and chunk_id happen to collide. Real-world setting:
    long-lived MCP server processing requests from multiple project dirs.
    """
    s = DeferredStore()
    c = Chunk(text="project-A secret", source="src/a")
    s.put("req-shared", [c], workspace_key="workspace-A")

    # Same request_id, different workspace — must see nothing.
    cid = chunk_id(c)
    assert s.retrieve("req-shared", [cid], workspace_key="workspace-B") == []
    assert s.manifest("req-shared", workspace_key="workspace-B") == []

    # Workspace A still sees its own chunk.
    got = s.retrieve("req-shared", [cid], workspace_key="workspace-A")
    assert len(got) == 1 and got[0].text == "project-A secret"


def test_same_request_id_different_workspaces_coexist():
    """Both workspaces can use the same request_id without colliding."""
    s = DeferredStore()
    chunk_a = Chunk(text="A content", source="src")
    chunk_b = Chunk(text="B content", source="src")
    s.put("req-x", [chunk_a], workspace_key="ws-a")
    s.put("req-x", [chunk_b], workspace_key="ws-b")

    a = s.retrieve("req-x", [chunk_id(chunk_a)], workspace_key="ws-a")
    b = s.retrieve("req-x", [chunk_id(chunk_b)], workspace_key="ws-b")
    assert len(a) == 1 and a[0].text == "A content"
    assert len(b) == 1 and b[0].text == "B content"


def test_default_workspace_key_is_empty_string_backward_compat():
    """Callers that don't pass workspace_key get the empty-string bucket
    (matches pre-v0.3.11 behavior). Verifies the migration didn't break
    callers that haven't updated."""
    s = DeferredStore()
    c = Chunk(text="legacy", source="src")
    s.put("req-legacy", [c])  # no workspace_key
    cid = chunk_id(c)
    # Retrieve without workspace_key works (default to "").
    assert len(s.retrieve("req-legacy", [cid])) == 1
    # Retrieve with explicit workspace_key="" also works.
    assert len(s.retrieve("req-legacy", [cid], workspace_key="")) == 1
    # Retrieve with a real workspace_key sees nothing.
    assert s.retrieve("req-legacy", [cid], workspace_key="other-workspace") == []


def test_evict_expired_sweeps_across_workspaces(monkeypatch):
    """Global eviction sweep must touch buckets in every workspace, not just
    the empty-string one."""
    base = 1000.0
    now = [base]
    monkeypatch.setattr(time, "time", lambda: now[0])

    s = DeferredStore(ttl_seconds=60.0)
    s.put("r", [Chunk(text="a", source="s")], workspace_key="ws-1")
    s.put("r", [Chunk(text="b", source="s")], workspace_key="ws-2")
    now[0] = base + 120
    removed = s.evict_expired()
    assert removed == 2
    assert s.manifest("r", workspace_key="ws-1") == []
    assert s.manifest("r", workspace_key="ws-2") == []
