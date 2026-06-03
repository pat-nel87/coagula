"""Prune stage — SPEC §6.3.

Applies only to ``kind == "json"``. Parses the chunk; if parse fails, passes it
through byte-identical (never corrupt input). Recursively drops keys in a
configurable denylist, truncates arrays longer than ``MAX_ARRAY`` (default 10)
with a marker element, and re-serializes compactly (``indent=1``).
"""

from __future__ import annotations

import json
from typing import Any

from ..stage import Chunk, Stage, Tier


def _prune_value(value: Any, denylist: set[str], max_array: int) -> Any:
    if isinstance(value, dict):
        return {
            k: _prune_value(v, denylist, max_array)
            for k, v in value.items()
            if k not in denylist
        }
    if isinstance(value, list):
        truncated = [_prune_value(v, denylist, max_array) for v in value[:max_array]]
        extra = len(value) - max_array
        if extra > 0:
            truncated.append(f"<+{extra} more items omitted>")
        return truncated
    return value


class Prune(Stage):
    name = "prune"

    def __init__(self, denylist: set[str] | None = None, max_array: int = 10):
        self.denylist = set(denylist or ())
        self.max_array = max_array

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        result: list[Chunk] = []
        for c in chunks:
            if c.tier == Tier.DEFERRED or c.kind != "json":
                result.append(c)
                continue
            try:
                parsed = json.loads(c.text)
            except (ValueError, TypeError):
                # Invalid JSON passes through byte-identical.
                result.append(c)
                continue
            pruned = _prune_value(parsed, self.denylist, self.max_array)
            result.append(c.with_text(json.dumps(pruned, indent=1)))
        return result
