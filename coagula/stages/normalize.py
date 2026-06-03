"""Normalize stage — SPEC §6.1.

Lossless: strip ANSI sequences, strip carriage returns, collapse intra-line
whitespace runs, trim trailing whitespace per line, collapse 3+ blank lines to
2. Applies to all chunks. Idempotent.
"""

from __future__ import annotations

import re

from ..stage import Chunk, Stage

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_INTRA_WS_RE = re.compile(r"[ \t]+")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANKS_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    # Collapse intra-line runs of spaces/tabs (do not touch newlines).
    text = _INTRA_WS_RE.sub(" ", text)
    text = _TRAILING_WS_RE.sub("", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text


class Normalize(Stage):
    name = "normalize"

    def process(self, chunks: list[Chunk], query: str, budget: int) -> list[Chunk]:
        return [c.with_text(normalize_text(c.text)) for c in chunks]
