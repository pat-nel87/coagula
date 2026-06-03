"""End-to-end funnel tests per SPEC §11."""

from __future__ import annotations

from pathlib import Path

from coagula import Chunk, Tier, default_funnel
from coagula.stages import chunk_id

FIXTURES = Path(__file__).parent / "fixtures"

K8S_DENYLIST = {
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


def _build_noisy_mixed_scenario() -> tuple[list[Chunk], str]:
    """Build the SPEC §11 noisy_mixed scenario; return (chunks, fatal_text)."""
    crashloop = (FIXTURES / "crashloop.log").read_text()
    kubectl = (FIXTURES / "kubectl_pod.json").read_text()
    noise = (FIXTURES / "noisy_mixed.txt").read_text()
    blocks = [b for b in noise.split("\n\n") if b.strip()]
    fatal_line = blocks[0].strip()
    other_blocks = blocks[1:]
    chunks: list[Chunk] = [
        Chunk(text=fatal_line, kind="text", source="alerts/payments", tier=Tier.CRITICAL),
        Chunk(text=kubectl, kind="json", source="kubectl/pod"),
        Chunk(text=crashloop, kind="log", source="logs/payments"),
    ]
    for i, blk in enumerate(other_blocks):
        chunks.append(Chunk(text=blk, kind="text", source=f"docs/runbook:{i}"))
    return chunks, fatal_line


def test_stage_adjacency_token_reconciliation():
    """For adjacent stages: results[i].tokens_out == results[i+1].tokens_in."""
    chunks, _ = _build_noisy_mixed_scenario()
    funnel = default_funnel(max_tokens=800, keep=4, json_denylist=K8S_DENYLIST)
    funnel.run(chunks, "why is the payments pod crashlooping", budget=800)
    for a, b in zip(funnel.results, funnel.results[1:]):
        assert a.tokens_out == b.tokens_in, (
            f"adjacency broken between {a.name}→{b.name}: "
            f"tokens_out={a.tokens_out} tokens_in={b.tokens_in}"
        )


def test_end_to_end_reduction_above_99_pct_and_fatal_survives():
    chunks, fatal_text = _build_noisy_mixed_scenario()
    funnel = default_funnel(max_tokens=800, keep=4, json_denylist=K8S_DENYLIST)
    out = funnel.run(chunks, "why is the payments pod crashlooping", budget=800)

    total_in = funnel.results[0].tokens_in
    total_out = funnel.results[-1].tokens_out
    ratio = (total_in - total_out) / total_in
    assert ratio >= 0.99, f"end-to-end reduction was {ratio:.2%}, expected ≥ 99%"

    assembled = next(c for c in out if c.source == "assembled")
    # The FATAL signal must survive regardless of ranker behavior.
    fatal_signature = "DNS lookup timeout"
    assert fatal_signature in assembled.text
    assert "FATAL" in assembled.text


def test_assemble_returns_one_live_chunk_with_deferred_footer():
    chunks, _ = _build_noisy_mixed_scenario()
    funnel = default_funnel(max_tokens=800, keep=4, json_denylist=K8S_DENYLIST)
    out = funnel.run(chunks, "why is the payments pod crashlooping", budget=800)
    live = [c for c in out if c.tier != Tier.DEFERRED]
    assert len(live) == 1
    assembled = live[0]
    deferred = [c for c in out if c.tier == Tier.DEFERRED]
    if deferred:
        for src in {c.source for c in deferred}:
            assert src in assembled.text  # footer lists every deferred source
        assert assembled.meta["deferred"] == len(deferred)
        assert set(assembled.meta["deferred_ids"]) == {chunk_id(c) for c in deferred}


def test_critical_chunk_never_demoted_in_full_pipeline():
    chunks, _ = _build_noisy_mixed_scenario()
    funnel = default_funnel(max_tokens=50, keep=2, json_denylist=K8S_DENYLIST)
    out = funnel.run(chunks, "irrelevant query about turnips", budget=50)
    # The assembled chunk should still include the FATAL line even under tight
    # budget and a query that doesn't lexically match anything.
    assembled = next(c for c in out if c.source == "assembled")
    assert "FATAL" in assembled.text


def test_funnel_with_zero_stages_still_runs():
    """Sanity from M1: funnel must be runnable even when empty."""
    from coagula import Funnel

    f = Funnel([])
    chunks = [Chunk(text="hello", source="x")]
    out = f.run(chunks, "q", 100)
    assert out == chunks
    assert f.results == []