"""Dedup stage — SPEC §6.2.

Applies only to ``kind == "log"``. Builds a template key per line by masking
volatile tokens (ISO-8601 timestamps → ``<TS>``, hex blobs ≥8 chars → ``<HEX>``,
bare integers → ``<N>``). Groups consecutive lines sharing a template, keeps
the first exemplar, collapses repeats to ``exemplar   (xN)`` when N > 1.
Preserves first-seen order. Non-log chunks pass through untouched.

This is the biggest single token win on crashloop-style fixtures.
"""

from __future__ import annotations

import re

from ..stage import Chunk, Stage, Tier

# ISO-8601 timestamps with optional fractional seconds and TZ.
_TS_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
# Hex blob: ≥8 hex chars. Order matters: must run BEFORE the integer mask, so
# pure-digit blobs of length ≥8 do not get treated as bare integers.
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")
# Bare integer (we already stripped timestamps and hex blobs).
_INT_RE = re.compile(r"\b\d+\b")


def template_key(line: str) -> str:
    line = _TS_RE.sub("<TS>", line)
    line = _HEX_RE.sub("<HEX>", line)
    line = _INT_RE.sub("<N>", line)
    return line


def _dedup_text(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    # Walk lines, grouping consecutive identical templates.
    i = 0
    n = len(lines)
    while i < n:
        exemplar = lines[i]
        key = template_key(exemplar)
        j = i + 1
        while j < n and template_key(lines[j]) == key:
            j += 1
        count = j - i
        if count > 1:
            out.append(f"{exemplar}   (x{count})")
        else:
            out.append(exemplar)
        i = j
    return "\n".join(out)


class Dedup(Stage):
    name = "dedup"

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        result: list[Chunk] = []
        for c in chunks:
            if c.tier == Tier.DEFERRED or c.kind != "log":
                result.append(c)
                continue
            result.append(c.with_text(_dedup_text(c.text)))
        return result
