"""Unit + opt-in integration tests for `coagula.models.azure_openai`.

Unit tests monkeypatch `urllib.request.urlopen` so they pass without Azure.
Set ``RUN_AZURE_TESTS=1`` and provide ``AZURE_OPENAI_ENDPOINT``,
``AZURE_OPENAI_API_KEY``, ``AZURE_OPENAI_LLM_DEPLOYMENT``,
``AZURE_OPENAI_EMBED_DEPLOYMENT`` to additionally exercise a real Azure
resource.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request

import pytest

from coagula.models.azure_openai import (
    AzureOpenAIError,
    make_embedder,
    make_llm,
    ping,
)

ENDPOINT = "https://my-resource.openai.azure.com"
API_KEY = "test-key"


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


def _http_error(code: int, body: str = "{}"):
    fp = io.BytesIO(body.encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://x/embeddings",
        code=code,
        msg="error",
        hdrs={},  # type: ignore[arg-type]
        fp=fp,
    )


# ---------------------------------------------------------------------------
# Unit (always run)
# ---------------------------------------------------------------------------


def test_embedder_returns_vectors_in_request_order(monkeypatch):
    """Azure echoes ``index`` per embedding — we must restore request order
    even if Azure returns them out of order."""

    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append(body)
        # Return embeddings in REVERSE order with index hints, to test sorting.
        return _fake_response(
            {
                "data": [
                    {"index": 2, "embedding": [3.0, 3.0]},
                    {"index": 0, "embedding": [1.0, 1.0]},
                    {"index": 1, "embedding": [2.0, 2.0]},
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    embed = make_embedder(deployment="embed-dep", endpoint=ENDPOINT, api_key=API_KEY)
    vecs = embed(["a", "ab", "abc"])
    assert vecs == [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]]
    assert captured[0]["input"] == ["a", "ab", "abc"]


def test_embedder_sends_api_key_header(monkeypatch):
    captured_headers: dict = {}

    def fake_urlopen(req, timeout=None):
        captured_headers.update(req.headers)
        return _fake_response({"data": [{"index": 0, "embedding": [0.0]}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    make_embedder("d", ENDPOINT, "secret123")(["x"])
    # urllib normalizes header case to titlecase.
    assert captured_headers.get("Api-key") == "secret123"
    assert captured_headers.get("Content-type") == "application/json"


def test_embedder_url_includes_api_version_and_deployment(monkeypatch):
    seen_url = {"value": ""}

    def fake_urlopen(req, timeout=None):
        seen_url["value"] = req.full_url
        return _fake_response({"data": [{"index": 0, "embedding": [0.0]}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    make_embedder("my-embed", ENDPOINT, API_KEY, api_version="2024-10-21")(["x"])
    url = seen_url["value"]
    assert "/openai/deployments/my-embed/embeddings" in url
    assert "api-version=2024-10-21" in url


def test_embedder_empty_input_skips_call(monkeypatch):
    called = {"count": 0}

    def fake_urlopen(req, timeout=None):
        called["count"] += 1
        return _fake_response({"data": []})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert make_embedder("d", ENDPOINT, API_KEY)([]) == []
    assert called["count"] == 0


def test_embedder_raises_without_fallback_on_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(401, '{"error":{"message":"unauthorized"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AzureOpenAIError, match="401"):
        make_embedder("d", ENDPOINT, API_KEY)(["hi"])


def test_embedder_uses_fallback_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    seen: list[list[str]] = []

    def fb(texts):
        seen.append(texts)
        return [[0.0] for _ in texts]

    out = make_embedder("d", ENDPOINT, API_KEY, fallback=fb)(["a", "b"])
    assert out == [[0.0], [0.0]]
    assert seen == [["a", "b"]]


def test_embedder_endpoint_trailing_slash_normalized(monkeypatch):
    seen_url = {"value": ""}

    def fake_urlopen(req, timeout=None):
        seen_url["value"] = req.full_url
        return _fake_response({"data": [{"index": 0, "embedding": [0.0]}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    make_embedder("d", ENDPOINT + "////", API_KEY)(["x"])
    # No double slashes between endpoint and /openai.
    assert "azure.com/openai" in seen_url["value"]
    assert "azure.com//" not in seen_url["value"]


def test_llm_returns_message_content(monkeypatch):
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _fake_response(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "  cleaned text  "}}
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = make_llm("gpt-nano", ENDPOINT, API_KEY, temperature=0.0, max_tokens=200)(
        "Compress: foo bar"
    )
    assert out == "cleaned text"
    sent = captured[0]
    assert sent["messages"][0]["content"] == "Compress: foo bar"
    assert sent["temperature"] == 0.0
    assert sent["max_tokens"] == 200


def test_llm_uses_fallback_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    llm = make_llm("d", ENDPOINT, API_KEY, fallback=lambda p: f"FALLBACK<{len(p)}>")
    assert llm("hi") == "FALLBACK<2>"


def test_llm_raises_on_malformed_response(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _fake_response({"choices": [{"message": {}}]})  # no content

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(AzureOpenAIError, match="content"):
        make_llm("d", ENDPOINT, API_KEY)("x")


def test_ping_true_on_success(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ping(ENDPOINT, API_KEY, deployment="x") is True


def test_ping_false_on_failure(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(404)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ping(ENDPOINT, API_KEY, deployment="missing") is False


def test_funnel_with_mocked_azure_hooks(monkeypatch):
    """End-to-end: default_funnel(embedder=azure, llm=azure) should still work."""
    from coagula import Chunk, Tier, default_funnel

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        body = json.loads(req.data.decode("utf-8"))
        if "/embeddings" in url:
            n = len(body.get("input", []))
            return _fake_response(
                {"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]}
            )
        if "/chat/completions" in url:
            prompt = body["messages"][0]["content"]
            return _fake_response(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "compressed: " + prompt[:50],
                            }
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    embed = make_embedder("embed-dep", ENDPOINT, API_KEY)
    llm = make_llm("nano-dep", ENDPOINT, API_KEY)

    chunks = [
        Chunk(text="FATAL pod down", source="alerts", tier=Tier.CRITICAL),
        Chunk(text="filler " * 200, source="docs/0"),
        Chunk(text="more filler " * 200, source="docs/1"),
    ]
    f = default_funnel(max_tokens=500, keep=2, embedder=embed, llm=llm)
    out = f.run(chunks, "pod down", budget=500)
    assembled = next(c for c in out if c.source == "assembled")
    assert "FATAL" in assembled.text


# ---------------------------------------------------------------------------
# Opt-in integration tests
# ---------------------------------------------------------------------------

_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
_api_key = os.environ.get("AZURE_OPENAI_API_KEY")
_llm_dep = os.environ.get("AZURE_OPENAI_LLM_DEPLOYMENT")
_embed_dep = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")

needs_azure = pytest.mark.skipif(
    os.environ.get("RUN_AZURE_TESTS") != "1"
    or not (_endpoint and _api_key and _llm_dep and _embed_dep),
    reason=(
        "Azure OpenAI integration tests require RUN_AZURE_TESTS=1 plus "
        "AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, "
        "AZURE_OPENAI_LLM_DEPLOYMENT, AZURE_OPENAI_EMBED_DEPLOYMENT."
    ),
)


@needs_azure
def test_real_azure_embedder_returns_floats():
    embed = make_embedder(_embed_dep, _endpoint, _api_key)
    vecs = embed(["the database is unreachable", "the office holiday party"])
    assert len(vecs) == 2
    assert all(isinstance(v, list) and len(v) > 0 for v in vecs)
    assert all(isinstance(x, float) for x in vecs[0])


@needs_azure
def test_real_azure_llm_returns_string():
    llm = make_llm(_llm_dep, _endpoint, _api_key, max_tokens=64)
    out = llm("Summarize in one sentence: the payments service cannot reach postgres.")
    assert isinstance(out, str) and len(out) > 0
