"""Stage tests per SPEC §11."""

from __future__ import annotations

import json
from pathlib import Path

from coagula import Chunk, Tier
from coagula.stages import Assemble, Budget, Dedup, Normalize, Prune, Relevance, Summarize

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


# ---------------------------------------------------------------------------
# Relevance (SPEC §6.4)
# ---------------------------------------------------------------------------


def test_relevance_never_demotes_critical():
    chunks = [
        Chunk(text="critical signal: FATAL pod down", source="alerts", tier=Tier.CRITICAL),
        Chunk(text="random unrelated chatter about cake and pies", source="noise/0"),
        Chunk(text="more unrelated chatter about cats and dogs", source="noise/1"),
        Chunk(text="another bit of noise about weather and sport", source="noise/2"),
    ]
    out = Relevance(keep=1).process(chunks, "FATAL pod", 1000)
    crit = [c for c in out if c.source == "alerts"][0]
    assert crit.tier == Tier.CRITICAL


def test_relevance_demotes_exactly_candidates_minus_keep():
    candidates = [
        Chunk(text=f"chunk number {i} talks about pods and pgcrashes", source=f"src/{i}")
        for i in range(7)
    ]
    out = Relevance(keep=3).process(candidates, "pods pgcrash", 1000)
    deferred = [c for c in out if c.tier == Tier.DEFERRED]
    assert len(deferred) == len(candidates) - 3


def test_relevance_annotates_meta():
    candidates = [
        Chunk(text="apple banana cherry", source="a"),
        Chunk(text="dog elephant fox", source="b"),
        Chunk(text="apple zebra", source="c"),
        Chunk(text="apple apple apple", source="d"),
    ]
    out = Relevance(keep=2).process(candidates, "apple", 1000)
    for c in out:
        assert "relevance" in c.meta


def test_relevance_noop_when_candidates_le_keep():
    candidates = [Chunk(text="x", source="a"), Chunk(text="y", source="b")]
    out = Relevance(keep=5).process(candidates, "q", 1000)
    assert all(c.tier == Tier.RELEVANT for c in out)


# ---------------------------------------------------------------------------
# Summarize (SPEC §6.5)
# ---------------------------------------------------------------------------


def test_summarize_leaves_json_untouched():
    raw = '{"a": 1, "b": [1,2,3]}'
    c = Chunk(text=raw, kind="json")
    out = Summarize(min_tokens=0).process([c], "q", 1000)[0]
    assert out.text == raw
    assert out.tier == Tier.RELEVANT


def test_summarize_leaves_code_untouched():
    raw = "def f():\n    return 42\n" * 50
    c = Chunk(text=raw, kind="code")
    out = Summarize(min_tokens=0).process([c], "q", 1000)[0]
    assert out.text == raw
    assert out.tier == Tier.RELEVANT


def test_summarize_skips_short_chunks():
    short = Chunk(text="too short.", kind="text")
    out = Summarize(min_tokens=120).process([short], "q", 1000)[0]
    assert out.tier == Tier.RELEVANT


def test_summarize_reduces_long_text_and_marks_tier():
    long = " ".join(
        f"Sentence {i} talks about pods and crashloop diagnostics." for i in range(80)
    )
    long += " The FATAL signal here is the database is unreachable."
    c = Chunk(text=long, kind="text")
    out = Summarize(min_tokens=10, keep_frac=0.3).process(
        [c], "database unreachable", 1000
    )[0]
    assert out.tier == Tier.SUMMARIZED
    assert out.meta.get("summarized") is True
    assert out.tokens < c.tokens


def test_summarize_uses_llm_hook_when_provided():
    calls: list[str] = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        return "MOCK SUMMARY"

    long = " ".join(f"Sentence {i}." for i in range(80))
    c = Chunk(text=long, kind="text")
    out = Summarize(min_tokens=10, llm=fake_llm).process([c], "q", 1000)[0]
    assert out.text == "MOCK SUMMARY"
    assert out.tier == Tier.SUMMARIZED
    assert len(calls) == 1
    assert "q" in calls[0]


# ---------------------------------------------------------------------------
# Budget (SPEC §6.6)
# ---------------------------------------------------------------------------


def test_budget_never_demotes_critical():
    crit = Chunk(text="CRITICAL: " + "x " * 500, source="crit", tier=Tier.CRITICAL)
    fillers = [
        Chunk(text="y " * 100, source=f"f/{i}", meta={"relevance": 1.0 - 0.01 * i})
        for i in range(5)
    ]
    out = Budget(max_tokens=50).process([crit] + fillers, "q", 50)
    survivors = [c for c in out if c.tier == Tier.CRITICAL]
    assert any(c.source == "crit" for c in survivors)
    assert all(c.tier != Tier.DEFERRED for c in survivors)


def test_budget_keeps_live_under_cap_excluding_criticals():
    chunks = [
        Chunk(text="z " * 50, source=f"r/{i}", meta={"relevance": 1.0 / (i + 1)})
        for i in range(10)
    ]
    cap = 30
    out = Budget(max_tokens=cap).process(chunks, "q", cap)
    live_fillable = [
        c for c in out if c.tier in (Tier.RELEVANT, Tier.SUMMARIZED)
    ]
    assert sum(c.tokens for c in live_fillable) <= cap


def test_budget_critical_overflow_flagged_when_criticals_alone_exceed_cap():
    huge_crit = Chunk(text="x " * 2000, source="crit", tier=Tier.CRITICAL)
    out = Budget(max_tokens=10).process([huge_crit], "q", 10)
    crit = [c for c in out if c.tier == Tier.CRITICAL][0]
    assert crit.meta.get("critical_overflow") is True


# ---------------------------------------------------------------------------
# Assemble (SPEC §6.7)
# ---------------------------------------------------------------------------


def test_assemble_returns_single_live_chunk_with_deferred_meta():
    chunks = [
        Chunk(text="kept", source="a", tier=Tier.RELEVANT),
        Chunk(text="dropped", source="b", tier=Tier.DEFERRED),
        Chunk(text="also-dropped", source="c", tier=Tier.DEFERRED),
    ]
    out = Assemble().process(chunks, "q", 1000)
    live = [c for c in out if c.tier != Tier.DEFERRED]
    assert len(live) == 1
    assembled = live[0]
    assert assembled.source == "assembled"
    assert "kept" in assembled.text
    assert "### a" in assembled.text
    assert "deferred (2 chunks)" in assembled.text
    assert assembled.meta["deferred"] == 2
    assert len(assembled.meta["deferred_ids"]) == 2
