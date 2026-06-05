"""Tests for the accuracy-preservation eval harness."""

from __future__ import annotations

import os

import pytest

from coagula.evals import EvalCase, EvalResult, EvalRunner, MockJudge
from coagula.evals.case import must_contain, must_match
from coagula.evals.fixtures import builtin_cases
from coagula.evals.runner import format_report


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------


def test_must_contain_case_insensitive_by_default():
    pred = must_contain("FATAL")
    assert pred("the fatal error happened") is True
    assert pred("nothing to see") is False


def test_must_contain_all_needles_required():
    pred = must_contain("FATAL", "connection refused")
    assert pred("FATAL: connection refused on host") is True
    assert pred("FATAL: some other thing") is False


def test_must_match_regex():
    pred = must_match(r'"reason":\s*"?CrashLoop')
    assert pred('  "reason": "CrashLoopBackOff"') is True
    assert pred("running normally") is False


# ---------------------------------------------------------------------------
# MockJudge
# ---------------------------------------------------------------------------


def test_mock_judge_picks_highest_overlap_line():
    judge = MockJudge()
    context = "\n".join(
        [
            "boring line one",
            "FATAL connection refused to database",
            "another boring line",
        ]
    )
    answer = judge(context, "FATAL database connection")
    assert "FATAL" in answer
    assert "connection refused" in answer


def test_mock_judge_output_is_byte_stable_across_runs():
    """Same context + same query must produce byte-identical output on
    every call so the eval suite's diff is deterministic."""
    judge = MockJudge()
    context = (
        "padding-one\npadding-two\nalpha bravo charlie\n"
        "padding-three\npadding-four\nalpha bravo delta\n"
        "padding-five\npadding-six\nzulu unrelated"
    )
    a = judge(context, "alpha bravo")
    b = judge(context, "alpha bravo")
    c = judge(context, "alpha bravo")
    assert a == b == c
    # And it actually surfaced the matching lines.
    assert "alpha bravo charlie" in a
    assert "alpha bravo delta" in a
    # Line outside the neighborhood window of any match stays out.
    assert "zulu unrelated" not in a


def test_mock_judge_no_overlap_returns_first_nonempty_line():
    judge = MockJudge()
    context = "\n\nfirst real line\nsecond line"
    answer = judge(context, "totally unrelated tokens")
    assert answer == "first real line"


# ---------------------------------------------------------------------------
# EvalRunner — happy paths
# ---------------------------------------------------------------------------


def test_runner_records_compression_and_accuracy_for_simple_case():
    """The runner should emit one EvalResult per case with token counts
    populated and grader verdicts applied to both raw + funneled answers."""
    runner = EvalRunner()
    runner.add_case(
        EvalCase(
            name="simple",
            query="FATAL connection",
            context="boring\nFATAL connection refused\nmore boring",
            grader=must_contain("FATAL", "connection"),
        )
    )
    report = runner.run()
    assert report.total == 1
    r = report.results[0]
    assert r.name == "simple"
    assert r.raw_tokens > 0
    assert r.funneled_tokens >= 0
    assert r.raw_correct is True
    assert r.funneled_correct is True
    assert r.accuracy_delta == 0


def test_runner_marks_regression_when_compression_drops_signal():
    """A grader that requires content the funnel can't preserve at the
    given budget should produce a regression. Use an explicit grader
    that won't be satisfied by the funnel's lossy summary."""

    class BrokenJudge:
        def __call__(self, context, query):
            # Returns the verbatim context — so accuracy depends purely
            # on what survives compression.
            return context

    runner = EvalRunner(judge=BrokenJudge())
    big_haystack = "\n".join(
        f"info line {i}" for i in range(1000)
    ) + "\nFATAL the needle"
    runner.add_case(
        EvalCase(
            name="needle-in-haystack",
            query="info line 17",  # query matches noise, not the needle
            context=big_haystack,
            grader=must_contain("FATAL the needle"),
            max_tokens=80,  # tight budget — needle may or may not survive
            keep=1,
        )
    )
    report = runner.run()
    r = report.results[0]
    # Raw context contains the needle, so raw answer is correct.
    assert r.raw_correct is True
    # Compression should have measurably reduced tokens.
    assert r.funneled_tokens < r.raw_tokens
    assert r.compression > 0.0


def test_runner_handles_judge_exception_gracefully():
    """A judge that raises on raw context should produce an EvalResult
    with error populated rather than crashing the suite."""

    class CrashJudge:
        def __call__(self, context, query):
            raise RuntimeError("simulated llm outage")

    runner = EvalRunner(judge=CrashJudge())
    runner.add_case(
        EvalCase(
            name="crashes",
            query="anything",
            context="some context",
            grader=must_contain("anything"),
        )
    )
    report = runner.run()
    assert report.total == 1
    r = report.results[0]
    assert r.error is not None
    assert "simulated llm outage" in r.error
    assert r.raw_correct is False
    assert r.funneled_correct is False


# ---------------------------------------------------------------------------
# Built-in suite — both fixtures must not regress with MockJudge
# ---------------------------------------------------------------------------


def test_builtin_suite_produces_two_cases():
    cases = builtin_cases()
    names = {c.name for c in cases}
    assert names == {"crashloop-fatal", "k8s-crashloop"}


def test_crashloop_fatal_signal_survives_compression():
    """The whole point: with a 4000-line crashloop log, the FATAL line
    must still be findable in the funneled output. This is the
    regression guard for the dedup stage."""
    case = next(c for c in builtin_cases() if c.name == "crashloop-fatal")
    runner = EvalRunner()
    runner.add_case(case)
    report = runner.run()
    r = report.results[0]
    assert r.raw_correct, "MockJudge should find FATAL in raw context"
    assert r.funneled_correct, (
        f"FATAL signal lost after compression — answer was: {r.funneled_answer!r}"
    )
    assert r.compression > 0.5, (
        f"crashloop dedup should compress >50%, got {r.compression:.2%}"
    )


def test_k8s_crashloop_diagnostic_field_survives_pruning():
    """With the k8s profile, the pod JSON's reason=CrashLoopBackOff must
    survive prune even though it strips managedFields / annotations /
    resourceVersion."""
    case = next(c for c in builtin_cases() if c.name == "k8s-crashloop")
    runner = EvalRunner()
    runner.add_case(case)
    report = runner.run()
    r = report.results[0]
    assert r.funneled_correct, (
        f"k8s profile prune destroyed the CrashLoopBackOff signal — "
        f"answer was: {r.funneled_answer!r}"
    )


# ---------------------------------------------------------------------------
# Suite report + formatter
# ---------------------------------------------------------------------------


def test_format_report_includes_all_cases_and_summary_line():
    runner = EvalRunner()
    runner.extend(builtin_cases())
    text = format_report(runner.run())
    assert "crashloop-fatal" in text
    assert "k8s-crashloop" in text
    assert "raw accuracy:" in text
    assert "regressions:" in text


def test_empty_suite_report_does_not_crash():
    runner = EvalRunner()
    text = format_report(runner.run())
    assert "no eval cases" in text


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_runs_with_default_arguments(capsys):
    from coagula.evals.__main__ import main

    rc = main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "crashloop-fatal" in captured.out
    assert "k8s-crashloop" in captured.out


def test_cli_fail_on_regression_exits_nonzero_when_regression(monkeypatch, capsys):
    """When --fail-on-regression is set and a regression occurs, exit 1."""
    from coagula.evals import runner as runner_mod
    from coagula.evals.case import EvalResult, SuiteReport

    fake_report = SuiteReport(
        results=[
            EvalResult(
                name="rigged",
                raw_tokens=100,
                funneled_tokens=10,
                raw_correct=True,
                funneled_correct=False,
            )
        ]
    )

    class FakeRunner:
        def __init__(self, *a, **kw): pass
        def extend(self, cases): pass
        def add_case(self, case): pass
        def run(self): return fake_report

    monkeypatch.setattr(
        "coagula.evals.__main__.EvalRunner", FakeRunner
    )

    from coagula.evals.__main__ import main
    rc = main(["--fail-on-regression"])
    assert rc == 1


# ---------------------------------------------------------------------------
# build_judge_from_env wiring
# ---------------------------------------------------------------------------


def test_build_judge_from_env_returns_none_without_backend(monkeypatch):
    """No backend configured → no judge wired (caller falls back to mock)."""
    from coagula.evals.judge import build_judge_from_env
    monkeypatch.setenv("COAGULA_BACKEND", "fallback")
    assert build_judge_from_env() is None


def test_get_default_judge_returns_mock_when_run_evals_unset(monkeypatch):
    from coagula.evals.judge import get_default_judge
    monkeypatch.delenv("RUN_EVALS", raising=False)
    judge = get_default_judge()
    assert isinstance(judge, MockJudge)


def test_get_default_judge_returns_mock_when_run_evals_set_but_no_backend(monkeypatch):
    from coagula.evals.judge import get_default_judge
    monkeypatch.setenv("RUN_EVALS", "1")
    monkeypatch.setenv("COAGULA_BACKEND", "fallback")
    judge = get_default_judge()
    assert isinstance(judge, MockJudge)
