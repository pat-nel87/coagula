"""Stage tests per SPEC §11."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coagula import Chunk, Tier
from coagula.stages import Dedup, Normalize, Prune

FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Normalize (SPEC §6.1)
# ---------------------------------------------------------------------------


def test_normalize_strips_ansi():
    text = "\x1b[31merror\x1b[0m happened"
    chunk = Chunk(text=text)
    out = Normalize().process([chunk], "", 100)[0]
    assert "\x1b" not in out.text
    assert "error" in out.text


def test_normalize_strips_carriage_returns():
    chunk = Chunk(text="line1\r\nline2\r\n")
    out = Normalize().process([chunk], "", 100)[0]
    assert "\r" not in out.text


def test_normalize_collapses_intra_line_whitespace():
    chunk = Chunk(text="foo     bar\t\tbaz")
    out = Normalize().process([chunk], "", 100)[0]
    assert out.text == "foo bar baz"


def test_normalize_trims_trailing_whitespace():
    chunk = Chunk(text="line with trailing   \nnext line\t\n")
    out = Normalize().process([chunk], "", 100)[0]
    for line in out.text.split("\n"):
        assert line == line.rstrip()


def test_normalize_collapses_blank_runs():
    chunk = Chunk(text="a\n\n\n\n\nb")
    out = Normalize().process([chunk], "", 100)[0]
    assert out.text == "a\n\nb"


def test_normalize_idempotent():
    raw = "\x1b[31mfoo\x1b[0m   bar\r\n\n\n\nbaz   \n\n\n"
    chunk = Chunk(text=raw)
    once = Normalize().process([chunk], "", 100)
    twice = Normalize().process(once, "", 100)
    assert once[0].text == twice[0].text


# ---------------------------------------------------------------------------
# Dedup (SPEC §6.2)
# ---------------------------------------------------------------------------


def test_dedup_collapses_identical_modulo_timestamp():
    n = 50
    lines = [
        f"2026-01-15T12:00:{i:02d}Z payments[{1000 + i}] ERROR connection refused"
        for i in range(n)
    ]
    chunk = Chunk(text="\n".join(lines), kind="log")
    out = Dedup().process([chunk], "", 100)[0]
    out_lines = out.text.split("\n")
    assert len(out_lines) == 1
    assert f"(x{n})" in out_lines[0]


def test_dedup_passes_through_non_log_chunks():
    text = "2026-01-15T12:00:00Z a\n2026-01-15T12:00:01Z a"
    chunk = Chunk(text=text, kind="text")
    out = Dedup().process([chunk], "", 100)[0]
    assert out.text == text


def test_dedup_preserves_first_seen_order_for_distinct_templates():
    chunk = Chunk(
        text="\n".join(
            [
                "2026-01-15T12:00:00Z ERROR pg down",
                "2026-01-15T12:00:01Z ERROR pg down",
                "2026-01-15T12:00:02Z INFO recovered",
                "2026-01-15T12:00:03Z ERROR pg down",
            ]
        ),
        kind="log",
    )
    out = Dedup().process([chunk], "", 100)[0]
    lines = out.text.split("\n")
    assert lines[0].startswith("2026-01-15T12:00:00Z ERROR pg down")
    assert "(x2)" in lines[0]
    assert "INFO recovered" in lines[1]
    assert lines[2].endswith("ERROR pg down")


def test_dedup_crashloop_fixture_reduction_above_95_pct():
    """SPEC §11: dedup ≥ 95% reduction on the crashloop fixture."""
    raw = (FIXTURES / "crashloop.log").read_text()
    chunk = Chunk(text=raw, kind="log")
    in_tokens = chunk.tokens
    out = Dedup().process([chunk], "", 100)[0]
    out_tokens = out.tokens
    saved_ratio = (in_tokens - out_tokens) / in_tokens
    assert (
        saved_ratio >= 0.95
    ), f"dedup reduction was {saved_ratio:.1%}, expected ≥ 95%"
    # Sanity: 4000 lines collapsed to a small handful.
    assert len(out.text.split("\n")) < 100


# ---------------------------------------------------------------------------
# Prune (SPEC §6.3)
# ---------------------------------------------------------------------------


def test_prune_drops_denylisted_keys_at_any_depth():
    payload = {
        "keep": 1,
        "drop_me": "x",
        "nested": {"drop_me": "y", "also_keep": [{"drop_me": "z", "good": True}]},
    }
    chunk = Chunk(text=json.dumps(payload), kind="json")
    out = Prune(denylist={"drop_me"}).process([chunk], "", 100)[0]
    parsed = json.loads(out.text)
    assert "drop_me" not in parsed
    assert "drop_me" not in parsed["nested"]
    assert "drop_me" not in parsed["nested"]["also_keep"][0]
    assert parsed["nested"]["also_keep"][0]["good"] is True


def test_prune_truncates_arrays_with_marker():
    payload = {"items": list(range(50))}
    chunk = Chunk(text=json.dumps(payload), kind="json")
    out = Prune(max_array=10).process([chunk], "", 100)[0]
    parsed = json.loads(out.text)
    items = parsed["items"]
    assert len(items) == 11
    assert items[:10] == list(range(10))
    assert items[10] == "<+40 more items omitted>"


def test_prune_passes_invalid_json_through_byte_identical():
    raw = "this is not { valid json"
    chunk = Chunk(text=raw, kind="json")
    out = Prune(denylist={"anything"}).process([chunk], "", 100)[0]
    assert out.text == raw


def test_prune_ignores_non_json_chunks():
    raw = '{"keep": 1, "drop_me": 2}'
    chunk = Chunk(text=raw, kind="text")
    out = Prune(denylist={"drop_me"}).process([chunk], "", 100)[0]
    assert out.text == raw


def test_prune_kubectl_fixture_drops_k8s_cruft():
    raw = (FIXTURES / "kubectl_pod.json").read_text()
    chunk = Chunk(text=raw, kind="json")
    k8s_denylist = {
        "managedFields",
        "resourceVersion",
        "uid",
        "generation",
        "creationTimestamp",
        "selfLink",
        "ownerReferences",
        "finalizers",
        "annotations",
        "labels",
    }
    out = Prune(denylist=k8s_denylist).process([chunk], "", 100)[0]
    parsed = json.loads(out.text)
    md = parsed["metadata"]
    for key in ("managedFields", "annotations", "labels", "uid", "resourceVersion"):
        assert key not in md
    # The signal that actually matters survives.
    waiting = parsed["status"]["containerStatuses"][0]["state"]["waiting"]
    assert waiting["reason"] == "CrashLoopBackOff"
    assert out.tokens < chunk.tokens
