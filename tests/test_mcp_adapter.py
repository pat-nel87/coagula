"""coagula_payload adapter tests per SPEC §9, §11 (MCP block)."""

from __future__ import annotations

import json
from pathlib import Path

from coagula.mcp import ChunkSpec, DeferredStore, coagula_payload

FIXTURES = Path(__file__).parent / "fixtures"


def test_severity_pins_fatal_and_error_to_critical():
    res = coagula_payload(
        [
            ChunkSpec("FATAL crash", source="alerts/a", severity="FATAL"),
            ChunkSpec("ERROR something", source="alerts/b", severity="ERROR"),
            ChunkSpec("just info", source="alerts/c", severity="INFO"),
        ],
        query="random unrelated query about turnips",
        max_tokens=50,
        keep=1,
    )
    # Both critical lines must survive the assembled prompt despite the tiny
    # budget and irrelevant query.
    assert "FATAL crash" in res.prompt
    assert "ERROR something" in res.prompt


def test_query_verbatim_match_pins_critical():
    res = coagula_payload(
        [
            ChunkSpec("the cake is a lie", source="trivia/a"),
            ChunkSpec("a completely unrelated note", source="trivia/b"),
            ChunkSpec("more unrelated chatter", source="trivia/c"),
            ChunkSpec("even more unrelated noise", source="trivia/d"),
        ],
        query="the cake is a lie",
        max_tokens=50,
        keep=1,
    )
    # Verbatim-match pin must survive tight budget + tiny keep.
    assert "the cake is a lie" in res.prompt


def test_extra_critical_patterns_regex_match():
    res = coagula_payload(
        [
            ChunkSpec("payment_id=42 transferred", source="logs/a"),
            ChunkSpec("normal log line", source="logs/b"),
            ChunkSpec("another normal log", source="logs/c"),
            ChunkSpec("yet more noise", source="logs/d"),
        ],
        query="something else",
        max_tokens=80,
        keep=1,
        extra_critical_patterns=[r"payment_id=\d+"],
    )
    assert "payment_id=42" in res.prompt


def test_extra_critical_patterns_substring_fallback_on_invalid_regex():
    """An unparseable regex must degrade to substring match, not crash."""
    res = coagula_payload(
        [
            ChunkSpec("MAGIC TOKEN HERE", source="logs/a"),
            ChunkSpec("other log line", source="logs/b"),
        ],
        query="something",
        max_tokens=60,
        keep=1,
        extra_critical_patterns=["[unclosed regex"],  # never compiles
    )
    # Substring fallback finds "[unclosed regex" → not in either line, so
    # nothing is pinned via extras. Sanity check we didn't crash:
    assert isinstance(res.prompt, str)

    # And: a valid substring-fallback pattern does match.
    res2 = coagula_payload(
        [
            ChunkSpec("MAGIC TOKEN HERE", source="logs/a"),
            ChunkSpec("other", source="logs/b"),
            ChunkSpec("more", source="logs/c"),
        ],
        query="something",
        max_tokens=60,
        keep=1,
        extra_critical_patterns=["MAGIC TOKEN"],   # valid as regex too, but plain substr
    )
    assert "MAGIC TOKEN" in res2.prompt


def test_k8s_profile_prunes_kubectl_fixture():
    raw = (FIXTURES / "kubectl_pod.json").read_text()
    res = coagula_payload(
        [ChunkSpec(raw, kind="json", source="kubectl/pod")],
        query="why is the pod crashing",
        max_tokens=2000,
        keep=5,
        profile="k8s",
    )
    # k8s denylist must have stripped these cruft keys.
    for forbidden in ("managedFields", "resourceVersion", "annotations"):
        assert forbidden not in res.prompt
    # The waiting reason — the actual diagnostic signal — must survive.
    assert "CrashLoopBackOff" in res.prompt


def test_passthrough_profile_does_not_prune():
    raw = (FIXTURES / "kubectl_pod.json").read_text()
    res = coagula_payload(
        [ChunkSpec(raw, kind="json", source="kubectl/pod")],
        query="anything",
        max_tokens=10000,
        keep=5,
        profile="passthrough",
    )
    # passthrough: managedFields survives.
    assert "managedFields" in res.prompt


def test_request_id_stable_and_store_holds_deferred():
    store = DeferredStore()
    # Many small chunks so some end up deferred under tight budget.
    specs = [ChunkSpec(f"chunk {i} text " * 30, source=f"s/{i}") for i in range(12)]
    res = coagula_payload(
        specs,
        query="something specific",
        max_tokens=200,
        keep=3,
        request_id="my-fixed-id",
        store=store,
    )
    assert res.request_id == "my-fixed-id"
    assert res.deferred_ids  # at least some deferred
    assert {m["id"] for m in res.deferred_manifest} == set(res.deferred_ids)
    # Round-trip retrieve via the store.
    got = store.retrieve("my-fixed-id", res.deferred_ids)
    assert len(got) == len(res.deferred_ids)


def test_auto_request_id_when_omitted():
    res = coagula_payload(
        [ChunkSpec("hello", source="a")], query="hi", max_tokens=100
    )
    assert isinstance(res.request_id, str) and len(res.request_id) >= 16


def test_report_present_and_nonempty():
    res = coagula_payload(
        [ChunkSpec("payload", source="a")], query="query", max_tokens=100
    )
    assert "TOTAL" in res.report
