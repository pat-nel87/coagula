"""``python -m coagula.cli`` — run the default funnel on a file or stdin.

Splits the input on blank-line-separated blocks, auto-detects each block's
``kind`` (json / log / code / text), runs ``default_funnel``, and writes the
assembled prompt to stdout. With ``--report``, also writes the per-stage
table to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import (
    Assemble,
    Budget,
    Chunk,
    Dedup,
    Funnel,
    Normalize,
    Prune,
    default_funnel,
)
from .config import PROFILES, get_profile

_LOG_PREFIX_RE = re.compile(
    r"^\s*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}|^\s*\[?\d{2}:\d{2}:\d{2}"
)
_CODE_HINTS_RE = re.compile(r"^\s*(```|def |class |#include |import |function )")


def detect_kind(block: str) -> str:
    stripped = block.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            return "json"
        except ValueError:
            pass
    # Log detection: first non-blank line looks like a timestamped log entry.
    first_line = next((ln for ln in block.split("\n") if ln.strip()), "")
    if _LOG_PREFIX_RE.search(first_line):
        return "log"
    if _CODE_HINTS_RE.search(first_line):
        return "code"
    return "text"


def chunk_text(text: str) -> list[Chunk]:
    blocks = re.split(r"\n{2,}", text)
    chunks: list[Chunk] = []
    for i, block in enumerate(blocks):
        if not block.strip():
            continue
        kind = detect_kind(block)
        chunks.append(Chunk(text=block, kind=kind, source=f"input/{i}:{kind}"))
    return chunks


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m coagula.cli",
        description="Run the coagula context funnel on a file or stdin.",
    )
    p.add_argument(
        "path",
        nargs="?",
        help="Input file path (omit to read from stdin).",
    )
    p.add_argument(
        "--query",
        default="",
        help=(
            "The question the assembled prompt is meant to support. When "
            "omitted (or empty), the funnel runs in 'lite' mode: only the "
            "lossless stages (normalize, dedup, prune, budget, assemble) "
            "fire — no Relevance ranking, no Summarize. Use this when the "
            "caller doesn't know what question the model is trying to "
            "answer, so we don't collapse arbitrary text against a "
            "meaningless query."
        ),
    )
    p.add_argument("--budget", type=int, default=2000, help="Max tokens (default 2000).")
    p.add_argument("--keep", type=int, default=5, help="Top-K kept by relevance (default 5).")
    p.add_argument(
        "--profile",
        default="passthrough",
        choices=sorted(PROFILES.keys()),
        help="Per-tool JSON pruning denylist (default: passthrough — no pruning).",
    )
    p.add_argument(
        "--report",
        action="store_true",
        help="Print the per-stage token/chunk table to stderr.",
    )
    return p


def _build_lite_funnel(budget: int, denylist: set[str]) -> Funnel:
    """5-stage lossless-only funnel — no Relevance, no Summarize.

    Used when no real query is available: scoring chunks against a generic
    or empty query yields uniformly-low relevance and the Summarize stage
    (especially with an LLM backend) can collapse output to near-zero
    tokens. The 5-stage form still gives ~95%+ reduction on log-shaped
    input via Dedup + Prune alone — the wins that don't need a query.
    """
    return Funnel([
        Normalize(),
        Dedup(),
        Prune(denylist=denylist, max_array=10),
        Budget(max_tokens=budget),
        Assemble(),
    ])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    text = Path(args.path).read_text() if args.path else sys.stdin.read()
    chunks = chunk_text(text)
    denylist = get_profile(args.profile)
    if not args.query.strip():
        # Lite mode: skip Relevance + Summarize. No backend wiring needed.
        funnel = _build_lite_funnel(args.budget, denylist)
    else:
        # Full funnel. Pick up Azure / Ollama from the environment — same
        # priority as the MCP server. Critical for the Copilot CLI hook
        # flow: the hook invokes this CLI on every noisy command, so the
        # user's AZURE_OPENAI_* / OLLAMA_* env vars need to reach the
        # funnel here too.
        from .models import build_hooks_from_env
        embedder, llm = build_hooks_from_env()
        funnel = default_funnel(
            max_tokens=args.budget,
            keep=args.keep,
            json_denylist=denylist,
            embedder=embedder,
            llm=llm,
        )
    out = funnel.run(chunks, args.query, args.budget)
    assembled = next((c for c in out if c.source == "assembled"), None)
    if assembled is None:
        print("(no live chunks survived)", file=sys.stderr)
        return 1
    sys.stdout.write(assembled.text + "\n")
    if args.report:
        sys.stderr.write(funnel.report() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
