"""Public library API — ``coagula_payload``. See SPEC §9.

Embedding code (your own diagnostic tools, custom scripts, etc.) calls
this from its own implementations before returning context to a model.
It pins severity → tier at ingestion (more reliable than any ranker),
selects the right denylist profile, runs the seven-stage funnel, and
stores the deferred chunks for later retrieval via ``DeferredStore``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import default_funnel
from .config import get_profile
from .stage import Chunk, Tier
from .stages import chunk_id
from .store import DeferredStore

# Severity strings that mark a chunk as CRITICAL per SPEC §9.
_CRITICAL_SEVERITIES = frozenset({"FATAL", "ERROR", "CRIT", "CRITICAL"})


@dataclass
class ChunkSpec:
    """Tool-supplied chunk descriptor. The tool *knows* the kind and severity
    at parse time, so the adapter doesn't need to auto-detect."""

    text: str
    kind: str = "text"            # text | log | json | code
    source: str = "unknown"
    severity: str | None = None   # FATAL | ERROR | WARN | INFO | DEBUG | None


@dataclass
class CoagulaResult:
    prompt: str
    request_id: str
    deferred_manifest: list[dict]
    report: str
    deferred_ids: list[str] = field(default_factory=list)


def _compile_patterns(patterns: Iterable[str] | None) -> list[Callable[[str], bool]]:
    if not patterns:
        return []
    checkers: list[Callable[[str], bool]] = []
    for p in patterns:
        try:
            rx = re.compile(p)
            checkers.append(lambda text, rx=rx: rx.search(text) is not None)
        except re.error:
            # Invalid regex falls back to plain substring match.
            checkers.append(lambda text, needle=p: needle in text)
    return checkers


def _is_critical(
    spec: ChunkSpec,
    query: str,
    extra_checkers: list[Callable[[str], bool]],
) -> bool:
    sev = (spec.severity or "").upper()
    if sev in _CRITICAL_SEVERITIES:
        return True
    if query and query.strip() and query in spec.text:
        return True
    return any(check(spec.text) for check in extra_checkers)


def coagula_payload(
    sources: list[ChunkSpec],
    query: str,
    *,
    max_tokens: int = 2000,
    keep: int = 5,
    profile: str = "passthrough",
    embedder: Callable | None = None,
    llm: Callable | None = None,
    request_id: str | None = None,
    store: DeferredStore | None = None,
    extra_critical_patterns: list[str] | None = None,
    workspace_key: str = "",
) -> CoagulaResult:
    """Run the default funnel on a list of tool-supplied chunks.

    Severity pinning per SPEC §9: FATAL/ERROR → CRITICAL; chunks whose text
    contains the query verbatim → CRITICAL; chunks matching any of
    `extra_critical_patterns` (regex preferred, substring fallback) →
    CRITICAL. Everything else starts RELEVANT.

    Stores deferred chunks in `store` (auto-creates one if not provided)
    under ``(workspace_key, request_id)`` so the LLM can request them
    later via the `retrieve` tool / call. ``workspace_key`` defaults to
    ``""`` for backward compat; multi-workspace deployments (long-lived
    MCP server serving multiple projects) should set it to a stable
    project/session identifier to prevent cross-workspace leaks.
    """
    rid = request_id or uuid.uuid4().hex
    if store is None:
        store = DeferredStore()
    extra_checkers = _compile_patterns(extra_critical_patterns)

    chunks: list[Chunk] = []
    for spec in sources:
        tier = (
            Tier.CRITICAL
            if _is_critical(spec, query, extra_checkers)
            else Tier.RELEVANT
        )
        chunks.append(
            Chunk(
                text=spec.text,
                kind=spec.kind,
                source=spec.source,
                tier=tier,
                meta={"severity": spec.severity} if spec.severity else {},
            )
        )

    denylist = get_profile(profile)
    funnel = default_funnel(
        max_tokens=max_tokens,
        keep=keep,
        embedder=embedder,
        llm=llm,
        json_denylist=denylist,
    )
    out = funnel.run(chunks, query, budget=max_tokens)

    assembled = next(c for c in out if c.source == "assembled")
    deferred = [c for c in out if c.tier == Tier.DEFERRED]
    store.put(rid, deferred, workspace_key=workspace_key)

    return CoagulaResult(
        prompt=assembled.text,
        request_id=rid,
        deferred_manifest=store.manifest(rid, workspace_key=workspace_key),
        report=funnel.report(),
        deferred_ids=[chunk_id(c) for c in deferred],
    )
