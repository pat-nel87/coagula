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
# Graceful degradation — Azure factories wired with fallback callables
# ---------------------------------------------------------------------------


def test_azure_embedder_falls_back_on_runtime_failure(monkeypatch):
    """Mid-funnel Azure failure must NOT crash; fallback returns zero vectors."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "text-embed")
    _mock_azure_reachable(monkeypatch)  # ping succeeds

    embedder, llm = _try_azure_openai()
    assert embedder is not None and llm is not None

    # Now make subsequent calls fail.
    _mock_azure_unreachable(monkeypatch)
    vecs = embedder(["one", "two"])  # must not raise
    assert vecs == [[0.0], [0.0]]


def test_azure_llm_falls_back_on_runtime_failure(monkeypatch):
    """Same shape for the LLM hook."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    _mock_azure_reachable(monkeypatch)

    _, llm = _try_azure_openai()
    assert llm is not None

    _mock_azure_unreachable(monkeypatch)
    result = llm("any prompt")  # must not raise
    # Fallback returns empty string — Summarize keeps the original chunk
    # untouched in that case.
    assert result == ""


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


# ---------------------------------------------------------------------------
# Cache-stable mode (v0.3.11)
# ---------------------------------------------------------------------------


def test_cache_stable_off_by_default(monkeypatch):
    from coagula.models import _cache_stable_enabled
    monkeypatch.delenv("COAGULA_CACHE_STABLE", raising=False)
    assert _cache_stable_enabled() is False


def test_cache_stable_on_via_truthy_values(monkeypatch):
    from coagula.models import _cache_stable_enabled
    for val in ("on", "1", "yes", "true", "ON", "Enabled"):
        monkeypatch.setenv("COAGULA_CACHE_STABLE", val)
        assert _cache_stable_enabled() is True, f"value {val!r} should enable"


def test_cache_stable_off_via_falsy_values(monkeypatch):
    from coagula.models import _cache_stable_enabled
    for val in ("off", "0", "no", "false", "", "disabled"):
        monkeypatch.setenv("COAGULA_CACHE_STABLE", val)
        assert _cache_stable_enabled() is False, f"value {val!r} should disable"


def test_cache_stable_forces_zero_temperature_on_azure_llm(monkeypatch):
    """When cache-stable is on, the Azure LLM factory should be invoked with
    temperature=0.0 so repeated identical inputs produce identical output."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.setenv("COAGULA_CACHE_STABLE", "on")
    _mock_azure_reachable(monkeypatch)

    captured: dict = {}
    from coagula.models import azure_openai as az_mod

    real_make_llm = az_mod.make_llm

    def spy_make_llm(*args, **kwargs):
        captured.update(kwargs)
        return real_make_llm(*args, **kwargs)

    monkeypatch.setattr(az_mod, "make_llm", spy_make_llm)

    from coagula.models import build_hooks_from_env
    embedder, llm = build_hooks_from_env()
    assert llm is not None
    assert captured.get("temperature") == 0.0, (
        f"cache-stable should force temperature=0; got kwargs={captured}"
    )


def test_azure_default_temperature_when_cache_stable_off(monkeypatch):
    """Sanity: without cache-stable, the Azure llm gets the non-zero default
    (0.1) so the existing behavior is preserved."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://my.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_LLM_DEPLOYMENT", "gpt-nano")
    monkeypatch.delenv("COAGULA_CACHE_STABLE", raising=False)
    _mock_azure_reachable(monkeypatch)

    captured: dict = {}
    from coagula.models import azure_openai as az_mod

    real_make_llm = az_mod.make_llm

    def spy_make_llm(*args, **kwargs):
        captured.update(kwargs)
        return real_make_llm(*args, **kwargs)

    monkeypatch.setattr(az_mod, "make_llm", spy_make_llm)

    from coagula.models import build_hooks_from_env
    _, llm = build_hooks_from_env()
    assert llm is not None
    # Without cache-stable, temperature should be 0.1 (the documented default).
    assert captured.get("temperature") == 0.1


def test_funnel_output_byte_stable_on_stdlib_path():
    """The stdlib funnel path (no LLM hooks) MUST be byte-deterministic for
    any chance at provider prompt cache hits on repeated identical inputs.
    Validates the documented claim in the cache-stable section.
    """
    from coagula import default_funnel
    from coagula.cli import chunk_text

    text = (
        "FATAL: payments pod down\n\n"
        + "\n".join(
            f"2026-01-15T12:00:0{i}Z payments[{1000+i}] ERROR connection refused"
            for i in range(20)
        )
        + "\n\nsome unrelated runbook text " * 30
    )

    def run_once():
        funnel = default_funnel(max_tokens=500, keep=3, embedder=None, llm=None)
        out = funnel.run(chunk_text(text), "why down", budget=500)
        return next(c for c in out if c.source == "assembled").text

    first = run_once()
    second = run_once()
    third = run_once()
    assert first == second == third, (
        "stdlib funnel output is not byte-stable across runs — cache-stable "
        "mode cannot deliver provider cache hits"
    )
