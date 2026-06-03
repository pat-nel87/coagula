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


def _build_hooks():
    """Auto-wire Ollama hooks if reachable. Otherwise return (None, None)."""
    try:
        from ..models.ollama import make_embedder, make_llm, ping
    except ImportError:
        return None, None

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    if not ping(host=host):
        log.info("Ollama not reachable at %s; using stdlib fallbacks", host)
        return None, None

    embed_model = os.environ.get("COAGULA_EMBED_MODEL", "nomic-embed-text")
    llm_model = os.environ.get("COAGULA_LLM_MODEL", "llama3.2:3b")
    log.info("Wiring Ollama: embed=%s, llm=%s, host=%s", embed_model, llm_model, host)
    return (
        make_embedder(model=embed_model, host=host),
        make_llm(model=llm_model, host=host),
    )


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
