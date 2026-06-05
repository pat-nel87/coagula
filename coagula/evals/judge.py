"""Judge callables — the "LLM" that the eval harness asks the question of.

Two concrete judges:

- ``MockJudge`` — deterministic, no network. Picks the line/snippet from
  the context that overlaps most with the query tokens and returns it.
  Good enough to detect *regression* (i.e. coagula destroyed the signal
  from the context) but not a substitute for real-model quality.

- ``build_judge_from_env()`` — wires Azure OpenAI or Ollama from the
  same env vars as the CLI / MCP server. Only used when ``RUN_EVALS=1``.

The harness only ever calls ``judge(context, query) -> str``. Plug in
anything matching that signature.
"""

from __future__ import annotations

import os
import re
from typing import Callable

Judge = Callable[[str, str], str]


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) >= 2}


class MockJudge:
    """Deterministic substring-overlap judge.

    For each line in the context, count how many query tokens it
    contains; return the top-``top_k`` lines (plus their nearest-line
    neighbors, to capture nested-JSON style answers where the
    signal-bearing field is sibling to the highest-overlap field).
    Ties broken by source order (first wins) — keeps output
    byte-stable across runs.

    This won't catch nuanced reasoning failures, but it WILL catch the
    failure mode coagula's accuracy guarantee is about: did the
    signal-bearing line survive compression?
    """

    def __init__(self, top_k: int = 3, neighborhood: int = 2):
        self.top_k = top_k
        self.neighborhood = neighborhood

    def __call__(self, context: str, query: str) -> str:
        q_tokens = _tokens(query)
        lines = context.splitlines()
        if not q_tokens or not lines:
            return context[:400]

        scored: list[tuple[int, int, str]] = []  # (-score, index, line)
        for i, line in enumerate(lines):
            line_tokens = _tokens(line)
            score = len(q_tokens & line_tokens)
            if score > 0:
                scored.append((-score, i, line))

        if not scored:
            for line in lines:
                if line.strip():
                    return line.strip()
            return ""

        scored.sort()
        top_indices: set[int] = set()
        for _, idx, _ in scored[: self.top_k]:
            for j in range(idx - self.neighborhood, idx + self.neighborhood + 1):
                if 0 <= j < len(lines):
                    top_indices.add(j)
        return "\n".join(lines[i].strip() for i in sorted(top_indices) if lines[i].strip())


def build_judge_from_env() -> Judge | None:
    """Wire a real LLM judge from env vars, or return None if nothing's set.

    Uses the same backend autowire as the CLI: Azure first, then Ollama.
    The returned judge sends a fresh chat completion per call — no
    caching, no batching. Intended for opt-in (RUN_EVALS=1) use only.
    """
    from ..models import build_hooks_from_env

    _, llm = build_hooks_from_env()
    if llm is None:
        return None

    def judge(context: str, query: str) -> str:
        prompt = (
            f"Answer the question using only the context below.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {query}\n\nAnswer:"
        )
        return llm(prompt).strip()

    return judge


def get_default_judge() -> Judge:
    """Return a real judge if RUN_EVALS=1 and a backend is configured;
    otherwise the deterministic MockJudge."""
    if os.environ.get("RUN_EVALS") == "1":
        real = build_judge_from_env()
        if real is not None:
            return real
    return MockJudge()
