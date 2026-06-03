"""Optional local-model adapters. See SPEC §6.4 / §6.5.

These factories return callables matching the ``embedder`` and ``llm`` hook
signatures on ``coagula.default_funnel`` / ``Relevance`` / ``Summarize``.
They are strictly optional — the funnel runs on stdlib alone.
"""

from __future__ import annotations

from .ollama import OllamaError, make_embedder, make_llm, ping

__all__ = ["OllamaError", "make_embedder", "make_llm", "ping"]
