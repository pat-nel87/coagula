"""End-to-end demo of the coagula default funnel on a noisy diagnostic payload.

Run from repo root: ``python demo.py``.

Builds the SPEC §11 ``noisy_mixed`` scenario (bloated kubectl JSON +
4000-line crashloop + a FATAL line pinned CRITICAL + irrelevant runbook /
holiday-party / marketing noise), runs the default funnel, prints the
per-stage savings table and the final assembled prompt.
"""

from __future__ import annotations

from pathlib import Path

from coagula import Chunk, Tier, default_funnel

FIXTURES = Path(__file__).parent / "tests" / "fixtures"

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


def build_scenario() -> list[Chunk]:
    crashloop = (FIXTURES / "crashloop.log").read_text()
    kubectl = (FIXTURES / "kubectl_pod.json").read_text()
    noise = (FIXTURES / "noisy_mixed.txt").read_text()

    # Split noisy_mixed into blocks; first block is the FATAL line we pin.
    blocks = [b for b in noise.split("\n\n") if b.strip()]
    fatal_line = blocks[0].strip()
    other_text_blocks = blocks[1:]

    chunks: list[Chunk] = [
        Chunk(text=fatal_line, kind="text", source="alerts/payments", tier=Tier.CRITICAL),
        Chunk(text=kubectl, kind="json", source="kubectl/pod"),
        Chunk(text=crashloop, kind="log", source="logs/payments"),
    ]
    for i, blk in enumerate(other_text_blocks):
        chunks.append(Chunk(text=blk, kind="text", source=f"docs/runbook:{i}"))
    return chunks


def main() -> int:
    chunks = build_scenario()
    funnel = default_funnel(max_tokens=800, keep=4, json_denylist=K8S_DENYLIST)
    out = funnel.run(chunks, "why is the payments pod crashlooping", budget=800)

    print(funnel.report())
    print()
    assembled = next(c for c in out if c.source == "assembled")
    print("=" * 72)
    print("ASSEMBLED PROMPT")
    print("=" * 72)
    print(assembled.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
