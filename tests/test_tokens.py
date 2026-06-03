"""Tests for `coagula.tokens` per SPEC §11 (Tokens)."""

from __future__ import annotations

import builtins
import importlib
import sys


def test_count_tokens_nonempty_positive():
    from coagula import tokens

    tokens._encoder.cache_clear()
    assert tokens.count_tokens("hello world") > 0


def test_count_tokens_empty_zero():
    from coagula import tokens

    assert tokens.count_tokens("") == 0


def test_count_tokens_falls_back_without_tiktoken(monkeypatch):
    """Per SPEC §5: if `tiktoken` is absent, use char-heuristic; never raise."""
    # Drop any cached tiktoken module and force ImportError on re-import.
    monkeypatch.delitem(sys.modules, "tiktoken", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tiktoken" or name.startswith("tiktoken."):
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Force re-evaluation of the encoder cache.
    from coagula import tokens

    tokens._encoder.cache_clear()
    text = "hello world"
    assert tokens._encoder() is None
    assert tokens.count_tokens(text) == max(1, len(text) // 4)

    # Cleanup: clear cache so subsequent tests can re-detect tiktoken if present.
    tokens._encoder.cache_clear()


def test_count_tokens_single_char_returns_at_least_one():
    from coagula import tokens

    # With or without tiktoken, a non-empty string must report > 0 tokens.
    assert tokens.count_tokens("x") >= 1
