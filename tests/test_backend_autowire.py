"""Tests for ``coagula.models.build_hooks_from_env`` — the shared backend
autowire logic used by both the standalone CLI and the MCP server."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from coagula.models import (
    _try_azure_openai,
    _try_ollama,
    build_hooks_from_env,
)


@pytest.fixture(autouse=True)
def _clear_backend_env(monkeypatch):
    """Each test starts from a known-empty backend-env state."""
    for var in (
        "COAGULA_BACKEND",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_LLM_DEPLOYMENT",
        "AZURE_OPENAI_EMBED_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION",
        "OLLAMA_HOST",
        "COAGULA_EMBED_MODEL",
        "COAGULA_LLM_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


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


def _mock_azure_reachable(monkeypatch):
    """Make every Azure HTTPS POST succeed with a chat-completions-shaped body."""
    def fake_urlopen(req, timeout=None):
        return _fake_response(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _mock_azure_unreachable(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _mock_ollama_reachable(monkeypatch):
    """Both ping (GET /api/tags) and any subsequent call succeed."""
    def fake_urlopen(req, timeout=None):
        return _fake_response({"models": []})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _mock_ollama_unreachable(monkeypatch):
    def fake_urlopen(*a, **kw):
        raise urllib.error.URLError("ollama down")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


# ---------------------------------------------------------------------------
# Priority — auto mode (no COAGULA_BACKEND set)
# ---------------------------------------------------------------------------


def test_auto_picks_azure_when_configured_and_reachable(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embed")
    _mock_azure_reachable(monkeypatch)

    embedder, llm = build_hooks_from_env()
    assert embedder is not None and llm is not None


def test_auto_falls_to_ollama_when_azure_unconfigured(monkeypatch):
    # No AZURE_* env. Ollama reachable.
    _mock_ollama_reachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    assert embedder is not None and llm is not None


def test_auto_falls_to_stdlib_when_nothing_reachable(monkeypatch):
    _mock_ollama_unreachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    assert embedder is None and llm is None


def test_auto_falls_to_ollama_when_azure_ping_fails(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")

    azure_calls = {"n": 0}
    ollama_calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "openai.azure.com" in url:
            azure_calls["n"] += 1
            raise urllib.error.URLError("azure timeout")
        if "11434" in url:
            ollama_calls["n"] += 1
            return _fake_response({"models": []})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    embedder, llm = build_hooks_from_env()
    # Azure was tried (and ping failed); Ollama was tried next and succeeded.
    assert azure_calls["n"] >= 1
    assert ollama_calls["n"] >= 1
    assert embedder is not None and llm is not None


# ---------------------------------------------------------------------------
# Explicit COAGULA_BACKEND override
# ---------------------------------------------------------------------------


def test_backend_fallback_forces_stdlib_even_when_azure_present(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("COAGULA_BACKEND", "fallback")
    _mock_azure_reachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    assert embedder is None and llm is None


def test_backend_azure_forces_azure_even_when_ollama_reachable(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("COAGULA_BACKEND", "azure")
    _mock_azure_reachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    assert llm is not None  # Azure llm wired


def test_backend_ollama_forces_ollama(monkeypatch):
    monkeypatch.setenv("COAGULA_BACKEND", "ollama")
    _mock_ollama_reachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    assert embedder is not None and llm is not None


def test_backend_azure_returns_none_when_azure_unreachable(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("COAGULA_BACKEND", "azure")
    _mock_azure_unreachable(monkeypatch)
    embedder, llm = build_hooks_from_env()
    # Forced Azure that's unreachable -> stdlib (does NOT silently fall to Ollama).
    assert embedder is None and llm is None


# ---------------------------------------------------------------------------
# Required Azure fields
# ---------------------------------------------------------------------------


def test_azure_requires_endpoint_key_and_a_deployment(monkeypatch):
    # Endpoint and key but no deployment -> not wired.
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    assert _try_azure_openai() is None


def test_azure_wires_with_only_llm_deployment(monkeypatch):
    """Embed deployment is optional; without it, Relevance falls back to TF-IDF."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    _mock_azure_reachable(monkeypatch)
    embedder, llm = _try_azure_openai()
    assert llm is not None
    assert embedder is None


def test_ollama_returns_none_when_unreachable(monkeypatch):
    _mock_ollama_unreachable(monkeypatch)
    assert _try_ollama() is None


# ---------------------------------------------------------------------------
# CLI integration — main() actually calls build_hooks_from_env
# ---------------------------------------------------------------------------


def test_cli_invokes_build_hooks_from_env(monkeypatch, tmp_path):
    """Verify the CLI wires the autowired backend into default_funnel."""
    from coagula import cli as cli_mod

    called = {"n": 0, "args": None}

    def fake_build_hooks_from_env():
        called["n"] += 1
        return None, None

    # Patch where the CLI imports it (locally inside main()).
    import coagula.models as models_mod
    monkeypatch.setattr(models_mod, "build_hooks_from_env", fake_build_hooks_from_env)

    captured_kwargs = {}
    real_default_funnel = cli_mod.default_funnel

    def spy_default_funnel(**kwargs):
        captured_kwargs.update(kwargs)
        return real_default_funnel(**kwargs)

    monkeypatch.setattr(cli_mod, "default_funnel", spy_default_funnel)

    fixture = tmp_path / "input.txt"
    fixture.write_text("hello world\n\nsome other line\n")
    rc = cli_mod.main(["--query", "test", str(fixture)])
    assert rc == 0
    assert called["n"] == 1, "build_hooks_from_env was not called by the CLI"
    # default_funnel received the (None, None) result.
    assert "embedder" in captured_kwargs and "llm" in captured_kwargs
    assert captured_kwargs["embedder"] is None
    assert captured_kwargs["llm"] is None
