"""Regression tests for the CLI's lite-mode bypass.

Field report: when the hooks shelled out to the CLI with a generic
fallback query like ``'general diagnostic query'``, Relevance + Summarize
collapsed output to ~1 token across multiple real Copilot CLI sessions
(``3921 → 1 tok``, ``3803 → 7 tok``, …). This was *information
annihilation*, not noise reduction.

The CLI now treats missing/empty ``--query`` as a signal to run a
lossless-only funnel (Normalize → Dedup → Prune → Budget → Assemble),
which still gets 95%+ reduction on log-shaped input via dedup alone
without crushing the signal.
"""

from __future__ import annotations

import io
from pathlib import Path

from coagula import cli


FIXTURES = Path(__file__).parent / "fixtures"


def _run_cli(argv, stdin_text, monkeypatch):
    """Drive coagula.cli.main() with stdin and capture stdout."""
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    rc = cli.main(argv)
    return rc, stdout.getvalue()


def test_query_is_no_longer_required():
    """Sanity: argparse no longer raises SystemExit on missing --query."""
    parser = cli.build_parser()
    # Should succeed (default "") rather than SystemExit.
    args = parser.parse_args([])
    assert args.query == ""


def test_lite_mode_preserves_signal_on_crashloop(monkeypatch):
    """The killer regression — generic-query collapse must not recur."""
    crashloop = (FIXTURES / "crashloop.log").read_text()
    rc, out = _run_cli([], crashloop, monkeypatch)
    assert rc == 0
    assert out.strip(), "lite-mode CLI produced empty output (regression!)"
    # Dedup should have collapsed the 4000 lines to ~1 with (xN) annotation.
    assert "(x4000)" in out or "connection refused" in out, (
        f"signal lost from lite-mode output (first 500 chars):\n{out[:500]}"
    )


def test_lite_mode_still_runs_dedup_and_prune(monkeypatch):
    """Lite mode is not a no-op — the lossless stages must still fire."""
    crashloop = (FIXTURES / "crashloop.log").read_text()
    rc, out = _run_cli([], crashloop, monkeypatch)
    assert rc == 0
    # Reduction at least 95% — that's the floor for dedup alone on this fixture.
    reduction = 1.0 - (len(out) / len(crashloop))
    assert reduction >= 0.95, f"lite-mode reduction was only {reduction:.1%}"


def test_lite_mode_with_kubectl_profile_prunes_json(monkeypatch, tmp_path):
    """The --profile flag still works in lite mode (Prune is included)."""
    kubectl = (FIXTURES / "kubectl_pod.json").read_text()
    fixture = tmp_path / "pod.json"
    fixture.write_text(kubectl)
    rc, out = _run_cli(
        ["--profile", "k8s", str(fixture)],
        "",  # stdin unused when path given
        monkeypatch,
    )
    assert rc == 0
    # k8s denylist must have stripped these even in lite mode.
    for forbidden in ("managedFields", "annotations", "resourceVersion"):
        assert forbidden not in out, (
            f"profile=k8s pruning didn't fire in lite mode: {forbidden} still present"
        )
    # And the diagnostic signal survives.
    assert "CrashLoopBackOff" in out


def test_lite_mode_does_not_call_build_hooks_from_env(monkeypatch):
    """Lite mode shouldn't ping Azure/Ollama since it doesn't use them.

    This matters because the Copilot CLI hook fires on every noisy tool
    call — adding a multi-second Azure ping to each one would be a real
    UX hit.
    """
    called = {"n": 0}

    def fake_build_hooks_from_env():
        called["n"] += 1
        return None, None

    import coagula.models as models_mod
    monkeypatch.setattr(models_mod, "build_hooks_from_env", fake_build_hooks_from_env)

    crashloop = (FIXTURES / "crashloop.log").read_text()
    rc, _ = _run_cli([], crashloop, monkeypatch)
    assert rc == 0
    assert called["n"] == 0, (
        "lite-mode CLI called build_hooks_from_env — wasted backend ping"
    )


def test_full_mode_DOES_call_build_hooks_from_env(monkeypatch):
    """Sanity: a real --query DOES wire backends from env."""
    called = {"n": 0}

    def fake_build_hooks_from_env():
        called["n"] += 1
        return None, None

    import coagula.models as models_mod
    monkeypatch.setattr(models_mod, "build_hooks_from_env", fake_build_hooks_from_env)

    rc, _ = _run_cli(
        ["--query", "why crashing"],
        "some text here\n\nanother block",
        monkeypatch,
    )
    assert rc == 0
    assert called["n"] == 1, (
        "full-mode CLI did not call build_hooks_from_env"
    )
