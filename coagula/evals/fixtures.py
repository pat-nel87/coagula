"""Built-in eval fixtures.

Two cases ship in-repo. Both prove the property coagula's accuracy
guarantee is actually about: did the signal-bearing line survive
compression?

External datasets (BFCL, SQuAD) are intentionally NOT bundled — they're
hundreds of MB and dwarf the rest of the package. Subclassing or
manually adding cases via ``EvalRunner.add_case`` is the supported
extension path.
"""

from __future__ import annotations

import json

from .case import EvalCase, must_contain


def _crashloop_log_fixture() -> str:
    """4000-line crashloop journal with a single FATAL line buried inside.

    Mirrors the SPEC §11 noisy_mixed shape — dedup should compress the
    repeated INFO/DEBUG lines hard, leaving the FATAL signal intact.
    """
    lines = []
    for i in range(1900):
        lines.append(
            f"2026-06-04T12:00:{i % 60:02d}Z payments[{1000+i}] INFO health-check ok"
        )
    lines.append(
        "2026-06-04T12:30:15Z payments[1500] FATAL connection refused to db.internal:5432"
    )
    for i in range(2000):
        lines.append(
            f"2026-06-04T12:01:{i % 60:02d}Z payments[{2000+i}] DEBUG retry connection attempt"
        )
    return "\n".join(lines)


def _k8s_pod_json_fixture() -> str:
    """A bloated kubectl pod JSON where the diagnostic field (reason:
    CrashLoopBackOff) is buried under managedFields / annotations /
    resourceVersion that the k8s profile prune should strip."""
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "payments-7d8c-xyz",
            "namespace": "prod",
            "resourceVersion": "8472619",
            "uid": "abc-def-ghi",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": "x" * 2000,
                "sidecar.istio.io/status": "y" * 1500,
            },
            "managedFields": [
                {"manager": "kube-controller-manager",
                 "fieldsV1": {"f:spec": {"f:containers": {}}},
                 "garbage": "z" * 3000}
            ],
        },
        "spec": {
            "containers": [{"name": "payments", "image": "payments:1.2.3"}],
        },
        "status": {
            "containerStatuses": [
                {
                    "name": "payments",
                    "state": {
                        "waiting": {
                            "reason": "CrashLoopBackOff",
                            "message": "back-off restarting failed container",
                        }
                    },
                    "restartCount": 47,
                }
            ]
        },
    }
    return json.dumps(pod, indent=2)


def builtin_cases() -> list[EvalCase]:
    """The two in-repo eval cases.

    Both use ``must_contain`` predicates that are tight enough to fail
    if compression destroyed the signal, loose enough not to fail on
    formatting drift in a real-LLM run.
    """
    return [
        EvalCase(
            name="crashloop-fatal",
            query="What FATAL error occurred?",
            context=_crashloop_log_fixture(),
            grader=must_contain("FATAL", "connection refused"),
            profile="passthrough",
            max_tokens=1000,
            keep=5,
        ),
        EvalCase(
            name="k8s-crashloop",
            # Query includes the keys MockJudge needs to locate the line;
            # real-LLM runs ignore the lexical hint and still find it.
            query="What is the waiting reason for the container?",
            context=_k8s_pod_json_fixture(),
            grader=must_contain("CrashLoopBackOff"),
            profile="k8s",
            max_tokens=800,
            keep=5,
        ),
    ]
