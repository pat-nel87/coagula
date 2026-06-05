"""EvalRunner — drives a set of EvalCases through the funnel."""

from __future__ import annotations

from typing import Iterable

from .. import Tier, default_funnel
from ..cli import chunk_text
from ..config import get_profile
from ..tokens import count_tokens
from .case import EvalCase, EvalResult, SuiteReport
from .judge import Judge, MockJudge


class EvalRunner:
    """Run a list of EvalCases against a judge.

    For each case: feed raw context to judge (baseline accuracy); feed
    funneled context to judge (post-compression accuracy); record token
    counts and grader verdicts. Aggregate into a SuiteReport.
    """

    def __init__(self, judge: Judge | None = None):
        self.judge: Judge = judge or MockJudge()
        self.cases: list[EvalCase] = []

    def add_case(self, case: EvalCase) -> None:
        self.cases.append(case)

    def extend(self, cases: Iterable[EvalCase]) -> None:
        for c in cases:
            self.add_case(c)

    def _run_one(self, case: EvalCase) -> EvalResult:
        raw_tokens = count_tokens(case.context)
        try:
            raw_answer = self.judge(case.context, case.query)
        except Exception as e:
            return EvalResult(
                name=case.name,
                raw_tokens=raw_tokens,
                funneled_tokens=raw_tokens,
                raw_correct=False,
                funneled_correct=False,
                error=f"raw judge failed: {e}",
            )

        chunks = chunk_text(case.context)
        funnel = default_funnel(
            max_tokens=case.max_tokens,
            keep=case.keep,
            json_denylist=get_profile(case.profile),
        )
        out = funnel.run(chunks, case.query, budget=case.max_tokens)
        assembled = next((c for c in out if c.source == "assembled"), None)
        if assembled is None:
            return EvalResult(
                name=case.name,
                raw_tokens=raw_tokens,
                funneled_tokens=raw_tokens,
                raw_correct=case.grader(raw_answer),
                funneled_correct=False,
                raw_answer=raw_answer,
                error="funnel produced no assembled chunk",
            )

        funneled_tokens = count_tokens(assembled.text)
        try:
            funneled_answer = self.judge(assembled.text, case.query)
        except Exception as e:
            return EvalResult(
                name=case.name,
                raw_tokens=raw_tokens,
                funneled_tokens=funneled_tokens,
                raw_correct=case.grader(raw_answer),
                funneled_correct=False,
                raw_answer=raw_answer,
                error=f"funneled judge failed: {e}",
            )

        return EvalResult(
            name=case.name,
            raw_tokens=raw_tokens,
            funneled_tokens=funneled_tokens,
            raw_correct=case.grader(raw_answer),
            funneled_correct=case.grader(funneled_answer),
            raw_answer=raw_answer,
            funneled_answer=funneled_answer,
        )

    def run(self) -> SuiteReport:
        report = SuiteReport()
        for case in self.cases:
            report.results.append(self._run_one(case))
        return report


def format_report(report: SuiteReport) -> str:
    """Render a SuiteReport as a fixed-width text table."""
    if not report.results:
        return "(no eval cases)"
    header = f"{'CASE':<24}  {'COMPRESSION':>11}  {'RAW':>4}  {'FUNNELED':>8}  DELTA"
    rows: list[str] = [header, "-" * len(header)]
    for r in report.results:
        delta = (
            "+" if r.accuracy_delta > 0
            else "-" if r.accuracy_delta < 0
            else "="
        )
        rows.append(
            f"{r.name:<24}  "
            f"{r.compression * 100:>10.1f}%  "
            f"{'OK' if r.raw_correct else 'FAIL':>4}  "
            f"{'OK' if r.funneled_correct else 'FAIL':>8}  "
            f"{delta}"
            + (f"  ({r.error})" if r.error else "")
        )
    rows.append("")
    rows.append(
        f"raw accuracy: {report.raw_correct}/{report.total}   "
        f"funneled accuracy: {report.funneled_correct}/{report.total}   "
        f"regressions: {len(report.regressions)}"
    )
    return "\n".join(rows)
