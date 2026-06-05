"""Accuracy-preservation eval harness.

Most token-compression libraries report compression ratios. That answers
"how much smaller is the prompt?" but not the question that actually
matters to a user thinking about adoption: *does the model still answer
correctly after compression?*

This module runs the funnel over a set of ``EvalCase`` records — each one
a (query, raw context, expected-answer predicate) triple — and measures
both the compression ratio AND the answer-accuracy delta between the raw
context and the funneled context.

Run from the command line::

    python -m coagula.evals                       # mocked LLM, instant
    RUN_EVALS=1 python -m coagula.evals           # real LLM via env
    RUN_EVALS=1 python -m coagula.evals --suite tier1

The mocked LLM is a deterministic substring-detector — good enough to
catch a *regression* where compression destroyed the signal, but not a
substitute for a real model. The ``RUN_EVALS=1`` gate routes through the
backend that ``build_hooks_from_env`` would pick (Azure / Ollama /
stdlib) and is opt-in because real-backend runs cost real tokens.

External datasets (BFCL tool-calling, SQuAD QA) are *not* bundled — the
harness exposes ``EvalRunner.add_case`` so callers can plug them in.
"""

from __future__ import annotations

from .case import EvalCase, EvalResult
from .judge import MockJudge, build_judge_from_env
from .runner import EvalRunner

__all__ = [
    "EvalCase",
    "EvalResult",
    "EvalRunner",
    "MockJudge",
    "build_judge_from_env",
]
