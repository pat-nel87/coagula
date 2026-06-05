"""EvalCase + EvalResult data classes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

Predicate = Callable[[str], bool]
"""Grader callable. Receives the LLM's answer; returns True if correct."""


def must_contain(*needles: str, case_sensitive: bool = False) -> Predicate:
    """Predicate: answer must contain ALL of these substrings."""
    if case_sensitive:
        return lambda answer: all(n in answer for n in needles)
    lowered = [n.lower() for n in needles]
    return lambda answer: all(n in answer.lower() for n in lowered)


def must_match(pattern: str, flags: int = re.IGNORECASE) -> Predicate:
    """Predicate: answer must match the regex."""
    rx = re.compile(pattern, flags)
    return lambda answer: rx.search(answer) is not None


@dataclass
class EvalCase:
    """A single eval test case.

    ``context`` is the raw noisy payload; ``query`` is the question the
    model will answer; ``grader`` decides whether the model's answer
    counts as correct. ``profile`` selects a JSON denylist (per-tool
    pruning); ``max_tokens`` / ``keep`` mirror the CLI flags.
    """

    name: str
    query: str
    context: str
    grader: Predicate
    profile: str = "passthrough"
    max_tokens: int = 2000
    keep: int = 5


@dataclass
class EvalResult:
    """Per-case eval outcome."""

    name: str
    raw_tokens: int
    funneled_tokens: int
    raw_correct: bool
    funneled_correct: bool
    raw_answer: str = ""
    funneled_answer: str = ""
    error: str | None = None

    @property
    def compression(self) -> float:
        """Fraction of tokens removed (1.0 = 100% removed)."""
        if self.raw_tokens == 0:
            return 0.0
        return 1.0 - (self.funneled_tokens / self.raw_tokens)

    @property
    def accuracy_delta(self) -> int:
        """+1: compression rescued an answer; -1: compression broke it; 0: tie."""
        return int(self.funneled_correct) - int(self.raw_correct)


@dataclass
class SuiteReport:
    """Aggregate eval-suite outcome."""

    results: list[EvalResult] = field(default_factory=list)

    @property
    def regressions(self) -> list[EvalResult]:
        """Cases where compression took a previously-correct answer wrong."""
        return [r for r in self.results if r.accuracy_delta < 0]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def funneled_correct(self) -> int:
        return sum(1 for r in self.results if r.funneled_correct)

    @property
    def raw_correct(self) -> int:
        return sum(1 for r in self.results if r.raw_correct)
