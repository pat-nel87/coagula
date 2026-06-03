"""Standalone MCP server — `coagula-mcp`. See SPEC §9.

Built on the official `mcp` Python SDK (stdio transport — universal across
Claude Code, Claude Desktop, and VSCode 1.99+ with GitHub Copilot). Exposes
two tools:

- ``manicure(text, query, ...)`` — chunks raw input via the same auto-detector
  the CLI uses, runs the funnel, returns the assembled prompt + a request_id
  + the deferred manifest + a per-stage report.
- ``retrieve(request_id, ids)`` — pulls specific deferred chunks back from
  the shared in-memory store.

If Ollama is reachable at module-import time (or `OLLAMA_HOST` is set),
embedder + llm hooks are auto-wired with the stdlib path as fallback so the
funnel never crashes if Ollama dies mid-session.

Install + register:

    pip install "coagula[mcp]"
    claude mcp add coagula coagula-mcp           # Claude Code
    # or in Claude Desktop / VSCode JSON config — see README.
"""

from __future__ import annotations

import logging
import os
import sys

from ..cli import chunk_text
from .adapter import ChunkSpec, coagula_payload
from .store import DeferredStore

log = logging.getLogger("coagula.mcp.server")

# Single process-scoped store so `retrieve` works for any prior `manicure`
# within this server's lifetime.
_STORE = DeferredStore(ttl_seconds=3600.0)


def _try_azure_openai():
    """Wire Azure OpenAI if env vars are present and a ping succeeds."""
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    llm_dep = os.environ.get("AZURE_OPENAI_LLM_DEPLOYMENT")
    embed_dep = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")
    if not (endpoint and api_key and (llm_dep or embed_dep)):
        return None
    try:
        from ..models.azure_openai import make_embedder, make_llm, ping
    except ImportError:
        return None
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
    # Only verify reachability against the LLM deployment if we have one.
    if llm_dep and not ping(endpoint, api_key, deployment=llm_dep, api_version=api_version):
        log.info("Azure OpenAI configured but ping failed for deployment %s", llm_dep)
        return None
    log.info(
        "Wiring Azure OpenAI: endpoint=%s embed=%s llm=%s api_version=%s",
        endpoint, embed_dep or "(none)", llm_dep or "(none)", api_version,
    )
    embedder = (
        make_embedder(embed_dep, endpoint, api_key, api_version=api_version)
        if embed_dep else None
    )
    llm = (
        make_llm(llm_dep, endpoint, api_key, api_version=api_version)
        if llm_dep else None
    )
    return embedder, llm


def _try_ollama():
    """Wire Ollama if reachable."""
    try:
        from ..models.ollama import make_embedder, make_llm, ping
    except ImportError:
        return None
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not ping(host=host):
        return None
    embed_model = os.environ.get("COAGULA_EMBED_MODEL", "nomic-embed-text")
    llm_model = os.environ.get("COAGULA_LLM_MODEL", "llama3.2:3b")
    log.info("Wiring Ollama: embed=%s, llm=%s, host=%s", embed_model, llm_model, host)
    return (
        make_embedder(model=embed_model, host=host),
        make_llm(model=llm_model, host=host),
    )


def _build_hooks():
    """Auto-wire embedder + llm using whichever backend is configured.

    Priority: explicit ``COAGULA_BACKEND`` env var > Azure OpenAI > Ollama >
    stdlib fallback (TF-IDF + extractive summarization, no embedder/llm).

    Set ``COAGULA_BACKEND`` to ``azure``, ``ollama``, or ``fallback`` to
    force a specific backend (e.g. for testing or to skip an outage).
    """
    forced = os.environ.get("COAGULA_BACKEND", "").lower()

    def azure():
        return _try_azure_openai()

    def ollama():
        return _try_ollama()

    if forced == "azure":
        return azure() or (None, None)
    if forced == "ollama":
        return ollama() or (None, None)
    if forced == "fallback":
        return None, None

    # Auto: try Azure, then Ollama, then stdlib.
    for name, factory in (("azure", azure), ("ollama", ollama)):
        result = factory()
        if result is not None:
            return result
        log.debug("Backend %s not available", name)

    log.info("No external backend available; using stdlib fallback")
    return None, None


def build_app():
    """Construct the FastMCP server. Importable for tests."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise SystemExit(
            "coagula-mcp requires the `mcp` extra. Install with: "
            "pip install 'coagula[mcp]'"
        ) from e

    mcp = FastMCP(
        name="coagula",
        instructions=(
            "Local context-funnel. Use `manicure` to trim noisy diagnostic "
            "payloads (kubectl JSON, crashloop logs, Postgres stats, ARM "
            "responses) down to the signal needed to answer a query. Use "
            "`retrieve` to pull specific deferred chunks back when you need "
            "more detail."
        ),
    )

    embedder, llm = _build_hooks()

    @mcp.tool()
    def manicure(
        text: str,
        query: str,
        profile: str = "passthrough",
        max_tokens: int = 2000,
        keep: int = 5,
        extra_critical_patterns: list[str] | None = None,
    ) -> dict:
        """Trim noisy context down to what's needed to answer `query`.

        Chunks the input on blank lines, auto-detects each block's kind
        (json/log/code/text), runs the seven-stage funnel, and returns the
        assembled prompt plus a request_id you can pass to `retrieve` if
        you need a deferred chunk later. Use `profile` ("k8s" | "postgres"
        | "azure" | "passthrough") to enable per-tool JSON pruning.
        """
        chunks = chunk_text(text)
        specs = [
            ChunkSpec(text=c.text, kind=c.kind, source=c.source) for c in chunks
        ]
        result = coagula_payload(
            specs,
            query=query,
            max_tokens=max_tokens,
            keep=keep,
            profile=profile,
            embedder=embedder,
            llm=llm,
            store=_STORE,
            extra_critical_patterns=extra_critical_patterns,
        )
        return {
            "prompt": result.prompt,
            "request_id": result.request_id,
            "deferred_manifest": result.deferred_manifest,
            "deferred_ids": result.deferred_ids,
            "report": result.report,
        }

    @mcp.tool()
    def retrieve(request_id: str, ids: list[str]) -> dict:
        """Pull specific deferred chunks back by id. Use after a `manicure`
        call when the assembled prompt referenced a deferred chunk you need
        full text for. Returns one entry per matched id."""
        chunks = _STORE.retrieve(request_id, ids)
        return {
            "chunks": [
                {
                    "text": c.text,
                    "source": c.source,
                    "kind": c.kind,
                    "tokens": c.tokens,
                }
                for c in chunks
            ],
            "missing": [i for i in ids if i not in {m["id"] for m in _STORE.manifest(request_id)}],
        }

    return mcp


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("COAGULA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    app = build_app()
    app.run("stdio")


if __name__ == "__main__":
    main()
