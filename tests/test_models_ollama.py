"""Unit + opt-in integration tests for `coagula.models.ollama`.

Unit tests monkeypatch `urllib.request.urlopen` so they pass without Ollama.
Set `RUN_OLLAMA_TESTS=1` to additionally exercise the real Ollama daemon if
one is running locally.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request

import pytest

from coagula import Chunk, Tier, default_funnel
from coagula.models import OllamaError, make_embedder, make_llm, ping


def _fake_response(body: dict, status: int = 200):
    class FakeResp:
        def __init__(self):
            self.status = status
            self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

        def read(self):
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return FakeResp()


# ---------------------------------------------------------------------------
# Unit tests (mocked — always run)
# ---------------------------------------------------------------------------


def test_embedder_returns_vectors_in_order(monkeypatch):
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body)
        # Distinguish vectors by the prompt so we can assert ordering.
        return _fake_response({"embedding": [float(len(body["prompt"])), 0.0, 1.0]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    embed = make_embedder(model="dummy")
    vecs = embed(["a", "ab", "abc"])
    assert vecs == [[1.0, 0.0, 1.0], [2.0, 0.0, 1.0], [3.0, 0.0, 1.0]]
    assert [c["prompt"] for c in calls] == ["a", "ab", "abc"]
    assert all(c["model"] == "dummy" for c in calls)


def test_embedder_raises_without_fallback_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embed = make_embedder()
    with pytest.raises(OllamaError):
        embed(["hello"])


def test_embedder_uses_fallback_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("no service")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    fallback_calls: list[list[str]] = []

    def fallback(texts):
        fallback_calls.append(texts)
        return [[0.0] * 3 for _ in texts]

    embed = make_embedder(fallback=fallback)
    out = embed(["hello", "world"])
    assert out == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    assert fallback_calls == [["hello", "world"]]


def test_embedder_raises_on_malformed_response(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _fake_response({"oops": "no embedding key"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OllamaError):
        make_embedder()(["x"])


def test_llm_returns_response_field(monkeypatch):
    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0.1
        return _fake_response({"response": "  MOCK SUMMARY  "})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm = make_llm(model="dummy")
    assert llm("Compress this please") == "MOCK SUMMARY"


def test_llm_raises_without_fallback_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(OllamaError):
        make_llm()("prompt")


def test_llm_uses_fallback_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm = make_llm(fallback=lambda p: f"FALLBACK<{len(p)}>")
    assert llm("hello there") == "FALLBACK<11>"


def test_ping_true_on_200(monkeypatch):
    def fake_urlopen(url, timeout=None):
        return _fake_response({"models": []}, status=200)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ping() is True


def test_ping_false_on_failure(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ping() is False


def test_funnel_with_mocked_ollama_hooks(monkeypatch):
    """End-to-end: default_funnel(embedder=ollama, llm=ollama) should still work."""

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        if url.endswith("/api/embeddings"):
            # Make every embedding the same so ranking still works deterministically.
            return _fake_response({"embedding": [1.0, 0.0, 0.0]})
        if url.endswith("/api/generate"):
            return _fake_response({"response": "compressed: " + body["prompt"][:40]})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    chunks = [
        Chunk(text="FATAL pod down", source="alerts", tier=Tier.CRITICAL),
        Chunk(text="some other note " * 80, source="docs/1"),
        Chunk(text="irrelevant chatter " * 80, source="docs/2"),
    ]
    f = default_funnel(
        max_tokens=500,
        keep=2,
        embedder=make_embedder(model="dummy"),
        llm=make_llm(model="dummy"),
    )
    out = f.run(chunks, "pod down", budget=500)
    assembled = next(c for c in out if c.source == "assembled")
    assert "FATAL" in assembled.text


# ---------------------------------------------------------------------------
# Opt-in integration tests (RUN_OLLAMA_TESTS=1, requires a live daemon)
# ---------------------------------------------------------------------------

needs_ollama = pytest.mark.skipif(
    os.environ.get("RUN_OLLAMA_TESTS") != "1" or not ping(),
    reason="Ollama not available (set RUN_OLLAMA_TESTS=1 and run `ollama serve`)",
)


@needs_ollama
def test_real_ollama_embedder_returns_floats():
    embed = make_embedder()
    vecs = embed(["the database is unreachable", "the office holiday party"])
    assert len(vecs) == 2
    assert all(isinstance(v, list) and len(v) > 0 for v in vecs)
    assert all(isinstance(x, float) for x in vecs[0])


@needs_ollama
def test_real_ollama_llm_returns_string():
    llm = make_llm()
    out = llm("Summarize in one sentence: the payments service cannot reach postgres.")
    assert isinstance(out, str) and len(out) > 0


@needs_ollama
def test_real_ollama_funnel_e2e_still_reduces():
    """SPEC §11 noisy_mixed assertions must still hold with Ollama hooks wired."""
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures"
    blocks = [
        b for b in (fixtures / "noisy_mixed.txt").read_text().split("\n\n") if b.strip()
    ]
    chunks = [
        Chunk(text=blocks[0].strip(), source="alerts", tier=Tier.CRITICAL),
        Chunk(text=(fixtures / "kubectl_pod.json").read_text(), kind="json", source="kubectl/pod"),
        Chunk(text=(fixtures / "crashloop.log").read_text(), kind="log", source="logs/payments"),
        *[Chunk(text=b, source=f"docs/{i}") for i, b in enumerate(blocks[1:])],
    ]
    f = default_funnel(
        max_tokens=800,
        keep=4,
        embedder=make_embedder(),
        llm=make_llm(),
    )
    out = f.run(chunks, "why is the payments pod crashlooping", budget=800)
    assembled = next(c for c in out if c.source == "assembled")
    total_in = f.results[0].tokens_in
    total_out = f.results[-1].tokens_out
    ratio = (total_in - total_out) / total_in
    assert ratio >= 0.99, f"e2e reduction with Ollama was {ratio:.2%}"
    assert "FATAL" in assembled.text
