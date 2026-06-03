"""Ollama-backed embedder + summarizer.

Returns callables compatible with ``coagula.Relevance(embedder=...)`` and
``coagula.Summarize(llm=...)``. Uses ``urllib.request`` so coagula stays
dependency-free on the default path; installing the ``ollama`` extras is
purely documentation (``pip install coagula[ollama]`` adds nothing because
this module uses stdlib only).

Per SPEC §2.5 (Graceful degradation): missing-or-broken Ollama must not crash
the funnel. Each factory accepts an optional ``fallback`` callable that is
invoked when an Ollama call fails (timeout, connection refused, model not
pulled, malformed JSON). If ``fallback`` is None, errors surface as
``OllamaError``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    """Raised when an Ollama call fails and no fallback is configured."""


def _post_json(url: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.URLError as e:
        raise OllamaError(f"Ollama call to {url} failed: {e}") from e
    except OSError as e:
        raise OllamaError(f"Ollama call to {url} failed: {e}") from e
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as e:
        raise OllamaError(f"Ollama returned non-JSON from {url}: {e}") from e


def ping(host: str = "http://localhost:11434", timeout: float = 2.0) -> bool:
    """Return True iff Ollama responds to ``GET /api/tags`` within ``timeout``."""
    url = host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def make_embedder(
    model: str = "nomic-embed-text",
    host: str = "http://localhost:11434",
    timeout: float = 30.0,
    fallback: Callable[[list[str]], list[list[float]]] | None = None,
) -> Callable[[list[str]], list[list[float]]]:
    """Return a callable that maps texts to embedding vectors via Ollama.

    Matches ``Relevance(embedder=...)``: ``list[str] -> list[list[float]]``.
    Calls ``POST /api/embeddings`` once per text (Ollama's single-input API).
    """
    url = host.rstrip("/") + "/api/embeddings"

    def embed(texts: list[str]) -> list[list[float]]:
        try:
            vectors: list[list[float]] = []
            for t in texts:
                resp = _post_json(url, {"model": model, "prompt": t}, timeout)
                vec = resp.get("embedding")
                if not isinstance(vec, list):
                    raise OllamaError(
                        f"Ollama embeddings response missing 'embedding' field: {resp!r}"
                    )
                vectors.append([float(x) for x in vec])
            return vectors
        except OllamaError:
            if fallback is not None:
                log.warning("Ollama embedder failed; using fallback")
                return fallback(texts)
            raise

    return embed


def make_llm(
    model: str = "llama3.2:3b",
    host: str = "http://localhost:11434",
    timeout: float = 60.0,
    fallback: Callable[[str], str] | None = None,
    temperature: float = 0.1,
) -> Callable[[str], str]:
    """Return a callable that maps a prompt to a summary string via Ollama.

    Matches ``Summarize(llm=...)``: ``str -> str``. Calls ``POST /api/generate``
    with ``stream: False`` so the response arrives in one chunk.
    """
    url = host.rstrip("/") + "/api/generate"

    def generate(prompt: str) -> str:
        try:
            resp = _post_json(
                url,
                {
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature},
                },
                timeout,
            )
            text = resp.get("response")
            if not isinstance(text, str):
                raise OllamaError(
                    f"Ollama generate response missing 'response' field: {resp!r}"
                )
            return text.strip()
        except OllamaError:
            if fallback is not None:
                log.warning("Ollama llm failed; using fallback")
                return fallback(prompt)
            raise

    return generate
