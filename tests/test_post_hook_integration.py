"""End-to-end integration tests for the Copilot CLI postToolUse bash hook.

These tests shell out to the actual hook script with synthetic Copilot CLI
payloads. They validate the v0.6.0 additions:

- Per-tool thresholds (bash 2000, view 500, MCP 1000)
- Cumulative session tracking (PPID-scoped state, triggers funneling once
  total bytes exceed COAGULA_CUMULATIVE_THRESHOLD)
- Decision logging (every hook exit emits a debug-log line categorizing
  the verdict)

Skipped on Windows: the bash hook on Windows is only invoked via the
PowerShell auto-defer path (Git Bash), and Windows-style HOME paths
mixed into bash variables produce path-handling failures in the new
session-state code. The canonical Windows hook is the .ps1 sibling,
which has its own integration tests (ps-hook-stdin-windows job in CI).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook tests skipped on Windows — PS hook is the canonical path "
           "(see .ps1 + ps-hook-stdin-windows CI job)",
)

HOOK = Path(__file__).parent.parent / "integrations" / "copilot-cli" / "coagula-post-tool-hook.sh"


def _coagula_available() -> bool:
    return shutil.which("coagula") is not None


needs_coagula = pytest.mark.skipif(
    not _coagula_available(),
    reason="coagula CLI must be on PATH for the hook to fire (e.g. activate the venv)",
)


def _run_hook(payload: dict, env: dict | None = None) -> tuple[dict, str]:
    """Invoke the hook with `payload` on stdin. Returns (stdout-json, debug-log-tail)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=full_env,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"hook exited {result.returncode}: stderr={result.stderr.decode()[:500]}"
        )
    stdout = result.stdout.decode().strip()
    out = json.loads(stdout) if stdout else {}
    debug_log = full_env["COAGULA_DEBUG_LOG"]
    log_tail = Path(debug_log).read_text() if Path(debug_log).exists() else ""
    return out, log_tail


def _payload(tool_name: str, text: str, command: str = "") -> dict:
    return {
        "toolName": tool_name,
        "toolArgs": {"command": command} if command else {},
        "toolResult": {
            "resultType": "success",
            "textResultForLlm": text,
            "exitCode": 0,
        },
    }


# ---------------------------------------------------------------------------
# Per-tool thresholds
# ---------------------------------------------------------------------------


def _dedup_able(n_lines: int) -> str:
    """Generate dedup-able fixture in log shape (timestamp + level + msg).

    Coagula's CLI chunk_text() auto-detects `kind="log"` from the leading
    ISO timestamp; only kind=log chunks go through Dedup. Same-template
    lines with varying timestamps/IDs get collapsed N → 1 + `(xN)`.
    """
    return "\n".join(
        f"2026-01-15T12:00:00.{i:06d}Z payments[{1000+i}] "
        f"ERROR connection refused to db.internal:5432 (req={i:016x})"
        for i in range(n_lines)
    )


@needs_coagula
def test_view_tool_funnels_at_500_token_default(tmp_path):
    """A view output above the 500-token per-tool threshold should fire,
    even though it's under the legacy 2000-token global threshold."""
    # 30 log-shaped lines ≈ 800 tokens, dedups to <50.
    text = _dedup_able(30)
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("view", text), env)
    assert "modifiedResult" in out, f"view above 500 tokens should fire; log:\n{log}"
    assert "fired" in log
    assert "tool=view" in log
    assert "per-call(" in log


@needs_coagula
def test_view_tool_below_500_passes_through(tmp_path):
    """A view output below 500 tokens passes through but still logs the
    decision so firing rate is observable."""
    text = _dedup_able(14)  # ~380 tokens — under view's 500
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("view", text), env)
    assert out == {}, "small view output should pass through"
    assert "under-threshold" in log
    assert "per_call=500" in log


@needs_coagula
def test_bash_tool_keeps_2000_token_threshold(tmp_path):
    """A 1500-token bash output is over view's 500 but under bash's 2000 —
    should still pass through. Validates per-tool tiering."""
    text = _dedup_able(55)  # ~1500 tokens — over view's 500, under bash's 2000
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("bash", text, command="echo stuff"), env)
    assert out == {}, "bash output under 2000 tokens should pass through"
    assert "under-threshold" in log
    assert "per_call=2000" in log


@needs_coagula
def test_mcp_tool_pattern_uses_1000_threshold(tmp_path):
    """Tools matching the MCP pattern (mcp:* or *__*) get the 1000 threshold."""
    text = _dedup_able(28)  # ~760 tokens — under MCP's 1000
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("github__list_issues", text), env)
    assert out == {}, "MCP output under 1000 tokens should pass through"
    assert "per_call=1000" in log


@needs_coagula
def test_global_threshold_override_takes_precedence(tmp_path):
    """COAGULA_THRESHOLD env overrides per-tool defaults."""
    text = _dedup_able(14)  # ~380 tokens — under view's 500 BUT over global 200
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_THRESHOLD": "200",
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("view", text), env)
    assert "modifiedResult" in out, f"global override should force fire; log:\n{log}"
    # Fired log line uses per-call(N>=M); confirm the override is M.
    assert ">=200" in log, f"expected per-call threshold 200 in log: {log}"


# ---------------------------------------------------------------------------
# Cumulative tracking
# ---------------------------------------------------------------------------


@needs_coagula
def test_cumulative_triggers_after_session_total_exceeds_threshold(tmp_path):
    """Simulate 5 sequential sub-threshold view calls. After cumulative
    crosses COAGULA_CUMULATIVE_THRESHOLD, subsequent calls should funnel."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_CUMULATIVE_THRESHOLD": "1000",  # tight for the test
        "HOME": str(tmp_path),
    }
    # Each call returns ~300 tokens, under view's 500-token per-call threshold.
    chunk_text = _dedup_able(11)  # ~300 tokens, under view's 500

    for i in range(3):
        out, _ = _run_hook(_payload("view", chunk_text), env)
        assert out == {}, f"call {i+1} should pass through (cumulative under threshold)"

    # 4th call should push us past cumulative threshold and fire.
    out, log = _run_hook(_payload("view", chunk_text), env)
    assert "modifiedResult" in out, f"cumulative should have triggered; log:\n{log}"
    assert "cumulative(" in log

    # Verify state file exists and is well-formed.
    state_files = list((tmp_path / ".copilot" / "coagula-session-state").glob("*.json"))
    assert state_files, "session state file should be created"
    state = json.loads(state_files[0].read_text())
    assert "updated_at" in state
    assert "total_tokens" in state
    assert state["total_tokens"] > 0


@needs_coagula
def test_cumulative_disabled_when_threshold_is_zero(tmp_path):
    """Setting COAGULA_CUMULATIVE_THRESHOLD=0 disables cumulative funneling.

    Note: v0.7+ may still emit a modifiedResult from the multi-view nudge
    path. Disable both via COAGULA_VIEW_NUDGE_AFTER=0 for a pure
    no-modification test."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_SUMMARY_INJECT": "off",
        "HOME": str(tmp_path),
    }
    chunk_text = _dedup_able(11)  # ~300 tokens (under view's 500)

    # 20 sub-threshold calls — none should funnel.
    fired_count = 0
    for _ in range(20):
        out, _ = _run_hook(_payload("view", chunk_text), env)
        if "modifiedResult" in out:
            fired_count += 1
    assert fired_count == 0, "all v0.7 injection paths disabled, none should fire"


# ---------------------------------------------------------------------------
# Decision logging — every exit path logs
# ---------------------------------------------------------------------------


@needs_coagula
def test_skipped_internal_tool_logs_decision(tmp_path):
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("report_intent", "anything"), env)
    assert out == {}
    assert "skipped" in log
    assert "internal-bookkeeping" in log


@needs_coagula
def test_skipped_user_skip_list_logs_decision(tmp_path):
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_SKIP_TOOLS": "my_custom_tool",
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("my_custom_tool", "anything"), env)
    assert out == {}
    assert "user-skip-list" in log


@needs_coagula
def test_disabled_logs_decision(tmp_path):
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_DISABLE": "1",
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("bash", "x" * 100000), env)
    assert out == {}
    assert "disabled" in log


@needs_coagula
def test_fired_log_includes_in_out_tokens_and_trigger_reason(tmp_path):
    """The 'fired' log line is the one users grep for to compute compression
    rate. Verify it has the fields they need."""
    text = "\n".join(
        f"FATAL connection refused (attempt={i})" for i in range(1000)
    )  # ~9k tokens
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_payload("bash", text, command="cat"), env)
    assert "modifiedResult" in out
    fired_line = next((l for l in log.splitlines() if "fired" in l), None)
    assert fired_line, "expected a 'fired' log line"
    for field in ("tool=bash", "in=", "out=", "reason=per-call", "cumulative="):
        assert field in fired_line, f"missing {field} in: {fired_line}"


# ---------------------------------------------------------------------------
# Quality: cumulative-triggered funneling preserves signal
# ---------------------------------------------------------------------------


@needs_coagula
def test_cumulative_triggered_call_preserves_fatal_signal(tmp_path):
    """When cumulative threshold triggers funneling on what would have been
    a small passthrough call, the funnel still preserves CRITICAL signals
    (FATAL / ERROR) per the funnel's correctness guarantee."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_CUMULATIVE_THRESHOLD": "500",  # tight, triggers fast
        "HOME": str(tmp_path),
    }
    # Prime the session with one big-enough call so cumulative is over 500.
    prime = "\n".join(f"INFO line {i} padding text" for i in range(60))
    _run_hook(_payload("view", prime), env)

    # Next call: small payload containing a FATAL line. Should be funneled,
    # but the FATAL line must survive.
    small = (
        "INFO startup ok\n"
        + "INFO ready\n"
        + "FATAL: cannot bind to port 5432\n"
        + "INFO retrying\n"
    )
    out, log = _run_hook(_payload("view", small), env)
    if "modifiedResult" in out:
        funneled_text = out["modifiedResult"]["textResultForLlm"]
        assert "FATAL" in funneled_text, (
            f"FATAL signal lost after cumulative-triggered funnel: {funneled_text!r}"
        )


# ---------------------------------------------------------------------------
# v0.7.0 — multi-view nudge (A.1)
# ---------------------------------------------------------------------------


def _view_payload(text: str, path: str = "tests/fixtures/crashloop.log") -> dict:
    """View tool payload with a `path` field (defensive — actual Copilot CLI
    field name may differ; the hook tries multiple)."""
    return {
        "toolName": "view",
        "toolArgs": {"path": path, "view_range": [1, 1]},
        "toolResult": {
            "resultType": "success",
            "textResultForLlm": text,
            "exitCode": 0,
        },
    }


@needs_coagula
def test_view_nudge_fires_after_threshold_view_calls(tmp_path):
    """After COAGULA_VIEW_NUDGE_AFTER view calls, the next response gets a
    one-time nudge prepended suggesting bash alternatives that funnel."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "3",
        "COAGULA_SUMMARY_INJECT": "off",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    tiny = "INFO line 1 some text"  # well under threshold

    # First 2 calls: no nudge, just passthrough.
    for _ in range(2):
        out, _ = _run_hook(_view_payload(tiny, "/tmp/no-such-file"), env)
        assert out == {}

    # 3rd call: nudge fires.
    out, log = _run_hook(_view_payload(tiny, "/tmp/no-such-file"), env)
    assert "modifiedResult" in out, f"3rd view should trigger nudge; log:\n{log}"
    text = out["modifiedResult"]["textResultForLlm"]
    assert "[coagula tip:" in text
    assert "grep" in text or "cat" in text  # mentions bash alternatives
    assert tiny in text  # original content preserved
    assert "nudge-injected" in log


@needs_coagula
def test_view_nudge_fires_only_once_per_session(tmp_path):
    """The nudge is meant to be a hint, not a repeated nag. Once sent,
    subsequent view calls don't re-inject it."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "1",
        "COAGULA_SUMMARY_INJECT": "off",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    tiny = "x"  # 1 char — nudge fires immediately

    # Call 1: nudge fires.
    out1, _ = _run_hook(_view_payload(tiny, "/tmp/no-such-file"), env)
    assert "[coagula tip:" in out1.get("modifiedResult", {}).get("textResultForLlm", "")

    # Calls 2-5: nudge does NOT fire again.
    for i in range(2, 6):
        out, _ = _run_hook(_view_payload(tiny, "/tmp/no-such-file"), env)
        assert out == {}, f"call {i} should not re-inject nudge"


@needs_coagula
def test_view_nudge_disabled_when_after_is_zero(tmp_path):
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_SUMMARY_INJECT": "off",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    for _ in range(20):
        out, _ = _run_hook(_view_payload("x", "/tmp/no-such-file"), env)
        assert out == {}, "nudge_after=0 should disable nudge"


# ---------------------------------------------------------------------------
# v0.7.0 — conditional file-summary injection (B.3)
# ---------------------------------------------------------------------------


@needs_coagula
def test_summary_injected_on_first_view_when_file_dedupable(tmp_path):
    """When the viewed file compresses well (e.g., crashloop log), the first
    view call gets a coagula summary prepended, helping the model decide
    whether to keep paginating."""
    # Create a dedup-able fixture
    fixture = tmp_path / "fake-crashloop.log"
    fixture.write_text(_dedup_able(500))  # ~13k tokens, dedups massively

    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_view_payload("one line of result", str(fixture)), env)
    assert "modifiedResult" in out, f"summary should inject; log:\n{log}"
    text = out["modifiedResult"]["textResultForLlm"]
    assert "[coagula summary of" in text
    assert "one line of result" in text  # original content preserved
    assert "summary-computed" in log


@needs_coagula
def test_summary_not_injected_when_file_not_dedupable(tmp_path):
    """When the viewed file is NOT compressible (e.g., random code), no
    summary is injected so we don't waste tokens."""
    # Heterogeneous content with no template repetition
    fixture = tmp_path / "unique-content.txt"
    fixture.write_text("\n".join(f"unique line {i} with totally different content {i*i}" for i in range(200)))

    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    out, log = _run_hook(_view_payload("one line", str(fixture)), env)
    assert out == {}, "non-dedupable file should not inject summary"
    assert "summary-skipped" in log


@needs_coagula
def test_summary_cached_across_view_calls_on_same_file(tmp_path):
    """Second view on same file uses cached summary; doesn't re-compute."""
    fixture = tmp_path / "fake-crashloop.log"
    fixture.write_text(_dedup_able(500))

    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    # First call computes
    _run_hook(_view_payload("line one", str(fixture)), env)
    # Second call should hit the cache
    out, log = _run_hook(_view_payload("line two", str(fixture)), env)
    assert "modifiedResult" in out
    log_lines = log.splitlines()
    assert any("summary-cached" in l for l in log_lines), (
        "second call should hit cache, not recompute"
    )
    assert sum(1 for l in log_lines if "summary-computed" in l) == 1, (
        "summary should only be computed once per file per session"
    )


@needs_coagula
def test_summary_disabled_via_env(tmp_path):
    fixture = tmp_path / "fake.log"
    fixture.write_text(_dedup_able(500))

    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_SUMMARY_INJECT": "off",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    out, _ = _run_hook(_view_payload("line", str(fixture)), env)
    assert out == {}, "summary-injection=off should disable feature"


@needs_coagula
def test_summary_extracts_path_from_string_shaped_toolargs(tmp_path):
    """Copilot CLI passes view's toolArgs as a JSON STRING, not an object.
    The extractor must fromjson-decode strings before looking up fields.
    Regression test for the v0.7.0 bug where summary never fired in real
    Copilot sessions because path extraction returned empty."""
    fixture = tmp_path / "fake.log"
    fixture.write_text(_dedup_able(500))

    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    # The real Copilot CLI shape: toolArgs is a STRING containing JSON.
    payload = {
        "toolName": "view",
        "toolArgs": json.dumps({"path": str(fixture), "view_range": [1, 1]}),
        "toolResult": {"resultType": "success", "textResultForLlm": "line", "exitCode": 0},
    }
    out, log = _run_hook(payload, env)
    assert "modifiedResult" in out, (
        f"summary should inject from string-shaped toolArgs; log:\n{log}"
    )
    assert "[coagula summary of" in out["modifiedResult"]["textResultForLlm"]
    assert "summary-computed" in log


@needs_coagula
def test_summary_skipped_when_file_path_undetectable(tmp_path):
    """If toolArgs has no recognized path field, summary path is skipped
    silently (no error)."""
    env = {
        "COAGULA_DEBUG_LOG": str(tmp_path / "debug.log"),
        "COAGULA_VIEW_NUDGE_AFTER": "0",
        "COAGULA_CUMULATIVE_THRESHOLD": "0",
        "HOME": str(tmp_path),
    }
    # Payload with no recognizable path field
    payload = {
        "toolName": "view",
        "toolArgs": {"unknown_field": "/tmp/x"},
        "toolResult": {"resultType": "success", "textResultForLlm": "x", "exitCode": 0},
    }
    out, _ = _run_hook(payload, env)
    assert out == {}, "no path → no summary attempt → passthrough"
