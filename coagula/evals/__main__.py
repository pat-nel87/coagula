"""CLI: ``python -m coagula.evals`` — run the built-in suite + print a table.

Defaults to the mocked judge (deterministic, no network). With
``RUN_EVALS=1`` and a configured Azure / Ollama backend, the harness
routes through the real model.

Exit code:
- 0 if no funneled answer regressed from a previously-correct raw answer
- 1 if any regression — useful as a CI gate when ``RUN_EVALS=1`` is set
"""

from __future__ import annotations

import argparse
import sys

from .fixtures import builtin_cases
from .judge import get_default_judge
from .runner import EvalRunner, format_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run coagula accuracy-preservation eval suite."
    )
    parser.add_argument(
        "--suite",
        default="tier1",
        choices=["tier1"],
        help="Which built-in suite to run (default: tier1).",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit 1 if any case had a funneled regression vs raw.",
    )
    args = parser.parse_args(argv)

    runner = EvalRunner(judge=get_default_judge())
    runner.extend(builtin_cases())
    report = runner.run()
    print(format_report(report))

    if args.fail_on_regression and report.regressions:
        print(
            f"\nFAIL: {len(report.regressions)} case(s) regressed under compression",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
