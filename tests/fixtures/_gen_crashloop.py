"""Generate crashloop.log fixture (4000 near-identical lines).

Run from repo root: ``python tests/fixtures/_gen_crashloop.py``.

Kept as a script (not auto-run by tests) so the committed fixture is stable
and the test suite is independent of Python version / locale.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

LINES = 4000
OUT = Path(__file__).with_name("crashloop.log")


def main() -> None:
    base = _dt.datetime(2026, 1, 15, 12, 0, 0)
    with OUT.open("w") as f:
        for i in range(LINES):
            ts = (base + _dt.timedelta(milliseconds=37 * i)).isoformat()
            pid = 1000 + (i % 41)
            req = f"{i:08x}{(i * 31) & 0xFFFFFFFF:08x}"
            f.write(
                f"{ts}Z payments[{pid}] ERROR connection refused: "
                f"upstream postgres unreachable (req={req}) attempt={i}\n"
            )


if __name__ == "__main__":
    main()
    print(f"wrote {LINES} lines to {OUT}")
