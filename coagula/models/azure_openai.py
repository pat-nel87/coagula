"""Azure OpenAI-backed embedder + summarizer.

Returns callables compatible with ``coagula.Relevance(embedder=...)`` and
``coagula.Summarize(llm=...)``. Uses ``urllib.request`` so coagula stays
dependency-free on the default path — no `openai` / `azure-sdk` install needed.

Intended use: route the funnel's optional embed + summarize tier through a
cheap Azure deployment (``gpt-4o-mini``, ``gpt-5.4-nano``, etc.) before the
frontier model (Claude / GPT-4) sees the cleaned context. Big wins at scale
on noisy diagnostic payloads.

Per SPEC §2.5 (Graceful degradation): missing-or-broken Azure must not crash
the funnel. Each factory accepts an optional ``fallback`` callable that is
invoked when an Azure call fails (timeout, 401/403, deployment missing,
malformed JSON). If ``fallback`` is None, errors surface as ``AzureOpenAIError``.

Authentication: API-key only for now (set ``AZURE_OPENAI_API_KEY``).
Microsoft Entra ID / managed identity can be added later if needed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Callable

log = logging.getLogger(__name__)

# Stable default — bump if Azure deprecates it. Users can override per call.
DEFAULT_API_VERSION = "2024-10-21"


class AzureOpenAIError(RuntimeError):
    """Raised when an Azure OpenAI call fails and no fallback is configured."""


def _normalize_endpoint(endpoint: str) -> str:
    """Strip trailing slashes; allow either ``https://x.openai.azure.com`` or
    the full resource URL."""
    return endpoint.rstrip("/")


def _post_json(
    url: str, body: dict, api_key: str, timeout: float
) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        # Surface useful detail (Azure returns helpful error messages in body).
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body_text = ""
        raise AzureOpenAIError(
            f"Azure OpenAI call to {url} failed: HTTP {e.code} {e.reason}: {body_text}"
        ) from e
    except urllib.error.URLError as e:
        raise AzureOpenAIError(f"Azure OpenAI call to {url} failed: {e}") from e
    except OSError as e:
        raise AzureOpenAIError(f"Azure OpenAI call to {url} failed: {e}") from e
    try:
        return json.loads(payload)
    except (ValueError, TypeError) as e:
        raise AzureOpenAIError(
            f"Azure OpenAI returned non-JSON from {url}: {e}"
        ) from e


def ping(
    endpoint: str,
    api_key: str,
    deployment: str,
    api_version: str = DEFAULT_API_VERSION,
    timeout: float = 3.0,
) -> bool:
    """Return True iff the deployment responds to a trivial chat completion.

    Cheap reachability check used by the MCP server to decide whether to
    auto-wire Azure as the backend. A real call (not a HEAD) because Azure
    doesn't expose a generic ``/health`` endpoint per deployment.
    """
    url = (
        f"{_normalize_endpoint(endpoint)}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )
    try:
        _post_json(
            url,
            {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            api_key,
            timeout,
        )
        return True
    except Exception:
        return False


def make_embedder(
    deployment: str,
    endpoint: str,
    api_key: str,
    api_version: str = DEFAULT_API_VERSION,
    timeout: float = 30.0,
    fallback: Callable[[list[str]], list[list[float]]] | None = None,
) -> Callable[[list[str]], list[list[float]]]:
    """Return an embedder callable matching ``Relevance(embedder=...)``.

    ``deployment`` is the *name* you gave the embedding deployment in your
    Azure resource (typically the model name, but Azure lets you rename;
    pass whatever you actually deployed under). The underlying model is
    typically ``text-embedding-3-small`` or ``text-embedding-3-large``.

    Sends all texts in one request (Azure embeddings accept arrays).
    """
    url = (
        f"{_normalize_endpoint(endpoint)}/openai/deployments/{deployment}"
        f"/embeddings?api-version={api_version}"
    )

    def embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            resp = _post_json(url, {"input": texts}, api_key, timeout)
            data = resp.get("data")
            if not isinstance(data, list):
                raise AzureOpenAIError(
                    f"Azure embeddings response missing 'data' array: {str(resp)[:200]}"
                )
            # Preserve request order. Azure echoes ``index`` per item; sort.
            sorted_data = sorted(data, key=lambda d: d.get("index", 0))
            return [[float(x) for x in d["embedding"]] for d in sorted_data]
        except AzureOpenAIError:
            if fallback is not None:
                log.warning("Azure embedder failed; using fallback")
                return fallback(texts)
            raise

    return embed


def make_llm(
    deployment: str,
    endpoint: str,
    api_key: str,
    api_version: str = DEFAULT_API_VERSION,
    timeout: float = 60.0,
    fallback: Callable[[str], str] | None = None,
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> Callable[[str], str]:
    """Return an LLM callable matching ``Summarize(llm=...)``.

    ``deployment`` is the name of your Azure chat-completion deployment (e.g.
    ``gpt-4o-mini``, ``gpt-5.4-nano`` if you deployed it as that name).
    """
    url = (
        f"{_normalize_endpoint(endpoint)}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )

    def generate(prompt: str) -> str:
        body: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            resp = _post_json(url, body, api_key, timeout)
            choices = resp.get("choices")
            if not isinstance(choices, list) or not choices:
                raise AzureOpenAIError(
                    f"Azure chat response missing 'choices': {str(resp)[:200]}"
                )
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if not isinstance(content, str):
                raise AzureOpenAIError(
                    f"Azure chat response missing message.content: {str(resp)[:200]}"
                )
            return content.strip()
        except AzureOpenAIError:
            if fallback is not None:
                log.warning("Azure llm failed; using fallback")
                return fallback(prompt)
            raise

    return generate
