"""Tests for the standalone `coagula-mcp` server.

Exercises the FastMCP server's tool handlers in-process by calling them via
`call_tool` — no stdio transport needed. Avoids any dependency on a live
Ollama daemon so the suite stays portable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

# Skip cleanly if the optional `mcp` extra isn't installed.
pytest.importorskip("mcp")

import coagula.mcp.server as server_mod

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _disable_ollama_autowire(monkeypatch):
    """Force the server to construct without trying to talk to Ollama."""
    monkeypatch.setattr(server_mod, "_build_hooks", lambda: (None, None))
    # Each test gets a fresh store so request_ids don't bleed across tests.
    from coagula.mcp.store import DeferredStore

    monkeypatch.setattr(server_mod, "_STORE", DeferredStore())
    yield


def _call(app, name, args):
    result = asyncio.run(app.call_tool(name, args))
    # FastMCP's call_tool returns (contents, structured) under recent SDKs.
    if isinstance(result, tuple):
        contents, structured = result
        # Newer SDK returns the structured dict directly if the tool returned dict.
        if isinstance(structured, dict):
            return structured
        # Otherwise pull JSON from the first content item.
        text = contents[0].text if contents else "{}"
        return json.loads(text)
    # Fallback for older SDK shapes: list[TextContent].
    text = result[0].text if isinstance(result, list) and result else "{}"
    return json.loads(text)


def test_server_registers_both_tools():
    app = server_mod.build_app()
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == {"manicure", "retrieve"}


def test_manicure_tool_reduces_and_keeps_fatal():
    app = server_mod.build_app()
    raw = (FIXTURES / "noisy_mixed.txt").read_text()
    res = _call(
        app,
        "manicure",
        {
            "text": raw,
            "query": "why is the payments pod crashlooping",
            "max_tokens": 800,
            "keep": 4,
        },
    )
    assert "prompt" in res and "request_id" in res
    assert "FATAL" in res["prompt"]
    assert isinstance(res["deferred_manifest"], list)
    assert isinstance(res["report"], str) and "TOTAL" in res["report"]


def test_manicure_then_retrieve_roundtrip():
    app = server_mod.build_app()
    # Push many small chunks so something definitely gets deferred.
    text = "\n\n".join(f"chunk number {i} with filler " * 30 for i in range(15))
    res = _call(
        app,
        "manicure",
        {
            "text": text,
            "query": "specific exact query unlikely to match anything",
            "max_tokens": 200,
            "keep": 3,
        },
    )
    assert res["deferred_ids"], "expected some deferred ids under tight budget"

    retrieved = _call(
        app,
        "retrieve",
        {"request_id": res["request_id"], "ids": res["deferred_ids"][:2]},
    )
    assert len(retrieved["chunks"]) == 2
    for ch in retrieved["chunks"]:
        assert set(ch.keys()) == {"text", "source", "kind", "tokens"}
        assert ch["tokens"] > 0


def test_retrieve_unknown_request_returns_empty():
    app = server_mod.build_app()
    res = _call(
        app,
        "retrieve",
        {"request_id": "nonexistent", "ids": ["x:00000000"]},
    )
    assert res["chunks"] == []
    assert res["missing"] == ["x:00000000"]


def test_manicure_with_k8s_profile_prunes_kubectl_blob():
    app = server_mod.build_app()
    kubectl = (FIXTURES / "kubectl_pod.json").read_text()
    res = _call(
        app,
        "manicure",
        {
            "text": kubectl,
            "query": "why is the pod failing",
            "profile": "k8s",
            "max_tokens": 2000,
            "keep": 5,
        },
    )
    assert "managedFields" not in res["prompt"]
    assert "CrashLoopBackOff" in res["prompt"]


def test_manicure_with_extra_critical_patterns():
    app = server_mod.build_app()
    text = (
        "MAGIC_TOKEN payment_id=42 transferred\n\n"
        + "\n\n".join(f"irrelevant filler text number {i}" * 5 for i in range(8))
    )
    res = _call(
        app,
        "manicure",
        {
            "text": text,
            "query": "something else",
            "max_tokens": 80,
            "keep": 1,
            "extra_critical_patterns": [r"payment_id=\d+"],
        },
    )
    assert "payment_id=42" in res["prompt"]
