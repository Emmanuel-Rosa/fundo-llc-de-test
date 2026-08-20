"""Break-demo tests -- the two properties that decide whether the demo demonstrates anything.

Run:  python -m tests.test_break_demo

The demo grades itself at runtime, which is stronger than a unit test for its orchestration:
it reads the real scorecard verdicts and exits non-zero on a surprise in either direction.
What runtime grading CANNOT catch is a demo that has quietly stopped being a demonstration
while still passing its own predictions. Two ways that happens, and both are here:

  1. THE DELTAS CANCEL. `_DELTAS` altering two amounts by +500 and -500 leaves SUM
     unchanged. Break 1 then becomes a second copy of break 2, the scorecard correctly
     returns non-FAIL, and the demo reports "the rewrite was NOT caught" -- blaming the
     checks for a defect in its own constants. Someone tidying two ugly numbers into a
     symmetric pair is a completely plausible edit.

  2. THE ROWS ARE IDENTICAL. A swap of two equal amounts is a no-op dressed as a
     corruption: the scorecard passes because nothing was broken, and the demo prints that
     the blind spot was demonstrated. `_pick_rows` is what prevents it, and its refusal
     path matters as much as its happy path.

No database and no driver: `_pick_rows` takes whatever answers `query`, so a recorded fake
is enough.
"""

from __future__ import annotations

import sys
import types

# Same stub, same reason as tests/test_watermark.py -- pipeline.break_demo imports
# pipeline.source for its type annotations, and that module imports pymssql at module scope.
if "pymssql" not in sys.modules:
    _stub = types.ModuleType("pymssql")
    _stub.Error = type("Error", (Exception,), {})       # type: ignore[attr-defined]
    _stub.connect = lambda *a, **k: None                # type: ignore[attr-defined]
    sys.modules["pymssql"] = _stub

from pipeline.break_demo import _DELTAS, _SEARCH_ROWS, _pick_rows  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


class FakeSource:
    """Answers `query` with one canned result set and records the SQL it was asked."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.queries: list[tuple[str, object]] = []

    def query(self, sql: str, params: object = None) -> list[dict]:
        self.queries.append((sql, params))
        return self._rows


def rows(*pairs: tuple[int, int]) -> list[dict]:
    return [{"transaction_id": t, "amount_cents": a} for t, a in pairs]


# ─────────────────────────────────────────────────────────────────────────────────────
# 1. THE DELTAS MUST ACTUALLY MOVE THE SUM
# ─────────────────────────────────────────────────────────────────────────────────────

check("there are exactly two deltas, one per targeted row", len(_DELTAS), 2)
check("the deltas DO NOT CANCEL -- a cancelling pair silently turns break 1 into break 2 "
      "and makes the demo blame the checks for its own constants",
      sum(_DELTAS) != 0, True)
check("the two deltas differ -- identical deltas shift MIN and MAX together and test one "
      "aggregate arm instead of two",
      _DELTAS[0] != _DELTAS[1], True)
check("neither delta is zero", all(d != 0 for d in _DELTAS), True)


# ─────────────────────────────────────────────────────────────────────────────────────
# 2. THE PICKED ROWS MUST BE SWAPPABLE
# ─────────────────────────────────────────────────────────────────────────────────────

# The ordinary case: the two oldest rows already differ, so they are the pair.
src = FakeSource(rows((1, -6500), (2, -12000), (3, -900)))
picked = _pick_rows(src, 253_700)
check("the two oldest differing rows are chosen",
      picked and (picked[0].transaction_id, picked[1].transaction_id), (1, 2))
check("their amounts come along", picked and (picked[0].amount_cents, picked[1].amount_cents),
      (-6500, -12000))

# The pick is CONFINED to the loaded window. A row above the watermark is not history yet,
# and rewriting it would be caught by the ordinary incremental path rather than by the
# frozen-segment argument this demo is about.
check("the pick is bounded by the watermark", "transaction_id <= %s" in src.queries[0][0], True)
check("the watermark is bound, not interpolated", src.queries[0][1], (253_700,))
check("the pick is ordered oldest-first", "ORDER BY transaction_id" in src.queries[0][0], True)
check("the search is bounded", f"TOP {_SEARCH_ROWS}" in src.queries[0][0], True)

# Equal amounts at the front: skip forward to the first row that differs, rather than
# returning a pair whose swap is a no-op.
src = FakeSource(rows((1, -6500), (2, -6500), (3, -6500), (4, -2100)))
picked = _pick_rows(src, 253_700)
check("leading duplicates are skipped for a genuinely swappable pair",
      picked and (picked[0].transaction_id, picked[1].transaction_id), (1, 4))

# Every amount identical: there is no swappable pair, and the demo must say so rather than
# swap two equal values and report a demonstration.
src = FakeSource(rows((1, -6500), (2, -6500), (3, -6500)))
check("an all-identical window refuses instead of faking a swap",
      _pick_rows(src, 253_700), None)

# Too few rows to break anything.
check("a single-row window refuses", _pick_rows(FakeSource(rows((1, -6500))), 253_700), None)
check("an empty window refuses", _pick_rows(FakeSource([]), 253_700), None)


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_break_demo: {checks} checks passed")
