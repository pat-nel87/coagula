"""Optional local-model adapters. See SPEC §6.4 / §6.5.

These factories return callables matching the ``embedder`` and ``llm`` hook
signatures on ``coagula.default_funnel`` / ``Relevance`` / ``Summarize``.
They are strictly optional — the funnel runs on stdlib alone.

This module also exposes ``build_hooks_from_env()`` which picks a backend
from environment variables. Both the standalone CLI (``coagula``) and the
MCP server (``coagula-mcp``) call it at startup so the same configuration
works for both invocation paths.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from . import azure_openai, ollama
from .ollama import OllamaError, make_embedder, make_llm, ping

__all__ = [
    "OllamaError",
    "azure_openai",
    "build_hooks_from_env",
    "make_embedder",
    "make_llm",
    "ollama",
    "ping",
]


log = logging.getLogger(__name__)


def _azure_llm_fallback(prompt: str) -> str:
    """If Azure fails mid-call, return the input text unchanged.

    Summarize's input prompt embeds the chunk text after a "Compress the
    following text..." preamble. Returning the prompt verbatim is wrong
    (it'd include the meta-instructions) but it never crashes the funnel.
    Better: return an empty string so Summarize keeps the original chunk
    untouched — Budget will trim it if needed.
    """
    return ""


def _azure_embed_fallback(texts: list[str]) -> list[list[float]]:
    """Return zero vectors. Cosine similarity treats these as score=0,
    so Relevance falls back to keeping the first K chunks in order
    rather than crashing."""
    return [[0.0] for _ in texts]


def _try_azure_openai() -> tuple[Callable | None, Callable | None] | None:
    """Wire Azure OpenAI if env vars are present and a ping succeeds.

    Required: AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY + at least one of
    AZURE_OPENAI_LLM_DEPLOYMENT / AZURE_OPENAI_EMBED_DEPLOYMENT.
    Optional: AZURE_OPENAI_API_VERSION (default 2024-10-21).

    Both factories get fallback callables so a transient network blip
    during a funnel call degrades gracefully rather than crashing the
    whole hook pipeline.
    """
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    llm_dep = os.environ.get("AZURE_OPENAI_LLM_DEPLOYMENT")
    embed_dep = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")
    if not (endpoint and api_key and (llm_dep or embed_dep)):
        return None
    from . import azure_openai as az  # local import keeps the module import-cheap
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    # Only verify reachability against the LLM deployment if we have one.
    if llm_dep and not az.ping(endpoint, api_key, deployment=llm_dep, api_version=api_version):
        log.info("Azure OpenAI configured but ping failed for deployment %s", llm_dep)
        return None
    log.info(
        "Wiring Azure OpenAI: endpoint=%s embed=%s llm=%s api_version=%s",
        endpoint, embed_dep or "(none)", llm_dep or "(none)", api_version,
    )
    embedder = (
        az.make_embedder(embed_dep, endpoint, api_key, api_version=api_version,
                         fallback=_azure_embed_fallback)
        if embed_dep else None
    )
    llm = (
        az.make_llm(llm_dep, endpoint, api_key, api_version=api_version,
                    fallback=_azure_llm_fallback)
        if llm_dep else None
    )
    return embedder, llm


def _try_ollama() -> tuple[Callable | None, Callable | None] | None:
    """Wire Ollama if reachable at OLLAMA_HOST (default http://localhost:11434).

    Models default to ``nomic-embed-text`` (embed) and ``llama3.2:3b`` (llm);
    override with COAGULA_EMBED_MODEL / COAGULA_LLM_MODEL.
    """
    from . import ollama as ol
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not ol.ping(host=host):
        return None
    embed_model = os.environ.get("COAGULA_EMBED_MODEL", "nomic-embed-text")
    llm_model = os.environ.get("COAGULA_LLM_MODEL", "llama3.2:3b")
    log.info("Wiring Ollama: embed=%s, llm=%s, host=%s", embed_model, llm_model, host)
    return (
        ol.make_embedder(model=embed_model, host=host),
        ol.make_llm(model=llm_model, host=host),
    )


def build_hooks_from_env() -> tuple[Callable | None, Callable | None]:
    """Pick an embedder + llm pair based on the current environment.

    Priority: explicit ``COAGULA_BACKEND`` env var > Azure OpenAI > Ollama >
    stdlib fallback (TF-IDF + extractive summarization, no embedder/llm).

    Set ``COAGULA_BACKEND`` to ``azure``, ``ollama``, or ``fallback`` to
    force a specific backend (e.g. for testing or to skip an outage).

    Returns ``(None, None)`` when no backend is configured or reachable —
    the funnel transparently falls back to the stdlib path.
    """
    forced = os.environ.get("COAGULA_BACKEND", "").lower()
    if forced == "azure":
        return _try_azure_openai() or (None, None)
    if forced == "ollama":
        return _try_ollama() or (None, None)
    if forced == "fallback":
        return None, None

    # Auto: try Azure, then Ollama, then stdlib.
    for name, factory in (("azure", _try_azure_openai), ("ollama", _try_ollama)):
        result = factory()
        if result is not None:
            return result
        log.debug("Backend %s not available", name)

    log.info("No external backend available; using stdlib fallback")
    return None, None
