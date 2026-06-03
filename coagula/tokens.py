"""Token counting with optional `tiktoken` and a char-heuristic fallback.

Per SPEC §5: the default path uses `tiktoken`'s `cl100k_base` encoder as a
portable relative-budget proxy. If `tiktoken` is not importable, fall back to a
char-heuristic. Never raises.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken
    except Exception:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)
