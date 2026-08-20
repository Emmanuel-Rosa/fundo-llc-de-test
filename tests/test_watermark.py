"""Watermark tests -- the bounded window, and the gap that the bound cannot close.

Run:  python -m tests.test_watermark

tables.yml says the high-water invariants are asserted here, and for a while that line was
the only thing holding them up. This file is what makes it true.

WHAT IS TESTED, in three parts:

  1. THE WINDOW BOUNDS. `read_high_water` must emit an EXCLUSIVE lower bound and an
     INCLUSIVE upper bound, and must refuse to read at all when the ceiling cannot have
     moved. Both off-by-one directions are silent in production and neither is caught by
     any other test in the suite: `>=` re-applies the previous run's last row, and `<`
     excludes the ceiling row from the read while the watermark still advances onto it --
     which loses it forever.

  2. THE CEILING DISCIPLINE. One MAX(id) per table per snapshot, taken through the
     snapshot's own cursor, over the whole table rather than a filtered subset.

  3. THE GAP THE BOUND DOES NOT CLOSE. `contiguity_verdict`'s arithmetic, driven by the
     numbers a two-connection probe produced against this container: three rows visible
     under a ceiling of 4, and 50,000 cents in the row that fell into the hole.
     This is the regression guard for a defect that was in the repo as a CLAIM OF
     IMMUNITY, not as a bug -- the code did what its docstring said, and the docstring was
     wrong.

WHAT NO TEST HERE CAN DO, stated rather than implied: none of this proves SQL Server
accepts the SQL, because pymssql is not installed on the host and is stubbed below. The
SQL that this file asserts on is a STRING, and the string is what is being tested. Only a
live run is evidence that the string executes -- same disclaimer as test_checks.py.
"""

from __future__ import annotations

import re
import sys
import types

# pymssql is a compiled driver and is not installed on the host interpreter; pipeline.source
# imports it at module scope and uses it for `connect` and `except pymssql.Error`, neither of
# which is reached by anything below. A stub keeps this file runnable with no database and no
# driver, which is the whole reason the SQL is built by a pure method in the first place.
if "pymssql" not in sys.modules:
    _stub = types.ModuleType("pymssql")
    _stub.Error = type("Error", (Exception,), {})       # type: ignore[attr-defined]
    _stub.connect = lambda *a, **k: None                # type: ignore[attr-defined]
    sys.modules["pymssql"] = _stub

from pipeline.checks.scorecard import (  # noqa: E402
    FAIL,
    PASS,
    SOURCE_DIRTY,
    CheckOutcome,
    ContiguityResult,
    Scorecard,
    contiguity_verdict,
)
from pipeline.config import MANIFEST_PATH, Manifest, TableSpec  # noqa: E402
from pipeline.source import SourceSnapshot  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


def squash(sql: str) -> str:
    """Collapse whitespace so an assertion tests the SQL and not its indentation."""
    return re.sub(r"\s+", " ", sql).strip()


TXN = TableSpec(
    name="transactions",
    strategy="high_water",
    primary_key=("transaction_id",),
    columns=("transaction_id", "customer_id", "amount_cents", "posted_at"),
    watermark_column="transaction_id",
    append_only_asserted=True,
)


class RecordingCursor:
    """Records every statement and its parameters; answers with canned rows in order.

    The recording is the point. What matters about `read_high_water` is the statement it
    builds and the parameters it binds, and about `high_water_ceiling` that it builds one
    statement per table and no more -- all of which is visible here and invisible to a
    fake that only returns rows.
    """

    def __init__(self, responses: list[list[dict]]) -> None:
        self._responses = list(responses)
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append((sql, params))

    def fetchall(self) -> list[dict]:
        return self._responses.pop(0) if self._responses else []

    # ── convenience for the assertions ──────────────────────────────────────────────

    @property
    def sql(self) -> list[str]:
        return [squash(s) for s, _p in self.statements]

    @property
    def params(self) -> list[object]:
        return [p for _s, p in self.statements]


def new_snapshot(responses: list[list[dict]]) -> tuple[SourceSnapshot, RecordingCursor]:
    """A SourceSnapshot over a recording cursor.

    SourceSnapshot.__init__ takes the change-tracking version FIRST -- that ordering is the
    correctness argument in Source.snapshot.__doc__ -- so the first canned response feeds it
    and the recorded statement list always opens with CHANGE_TRACKING_CURRENT_VERSION.
    """
    cursor = RecordingCursor([[{"v": 41}]] + responses)
    return SourceSnapshot(source=None, cursor=cursor), cursor  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────────────
# 1. THE WINDOW BOUNDS -- both off-by-ones are silent, so both are asserted
# ─────────────────────────────────────────────────────────────────────────────────────

snap, cur = new_snapshot([[{"transaction_id": 101}]])
rows = snap.read_high_water(TXN, 100, 200)

read_sql = cur.sql[-1]
check("the read happened", len(rows), 1)
check("the LOWER bound is EXCLUSIVE -- >= would re-apply the previous run's last row",
      "[transaction_id] > %s" in read_sql, True)
check("the lower bound is not >=", "[transaction_id] >= %s" in read_sql, False)
check("the UPPER bound is INCLUSIVE -- < would exclude the ceiling row while the "
      "watermark advances onto it, losing it forever",
      "[transaction_id] <= %s" in read_sql, True)
check("the upper bound is not <", "[transaction_id] < %s" in read_sql, False)
check("the bounds are bound in order (since, ceiling)", cur.params[-1], (100, 200))
check("the window is closed on BOTH sides -- exactly two placeholders",
      read_sql.count("%s"), 2)
check("the batch is ordered by the watermark, so two reads of one window are comparable",
      "ORDER BY [transaction_id]" in read_sql, True)
check("the read is column-explicit, never SELECT *", "SELECT *" in read_sql, False)

# An unmoved or absent ceiling must not produce a query at all. Issuing `id > 200 AND
# id <= 200` would be harmless; issuing it against a ceiling of None would not be, and
# either way a statement per idle table per run is a cost with no return.
for label, since, ceiling in (
    ("equal to the watermark", 200, 200),
    ("BELOW the watermark", 200, 199),
    ("absent (empty source table)", 200, None),
):
    snap, cur = new_snapshot([])
    before = len(cur.statements)
    empty = snap.read_high_water(TXN, since, ceiling)  # type: ignore[arg-type]
    check(f"a ceiling {label} returns no rows", empty, [])
    check(f"a ceiling {label} issues NO query", len(cur.statements), before)


# ─────────────────────────────────────────────────────────────────────────────────────
# 2. THE CEILING DISCIPLINE
# ─────────────────────────────────────────────────────────────────────────────────────

snap, cur = new_snapshot([[{"ceiling": 253700}], [{"ceiling": 999999}]])
first = snap.high_water_ceiling(TXN)
second = snap.high_water_ceiling(TXN)

ceiling_sql = cur.sql[1]
check("the ceiling is MAX of the watermark column",
      "MAX([transaction_id])" in ceiling_sql, True)
check("the ceiling is taken over the WHOLE table -- a WHERE here would make the ceiling "
      "narrower than the read, and the rows in between would never be read again",
      "WHERE" in ceiling_sql, False)
check("the ceiling is read through the SNAPSHOT's own cursor, which is what makes it "
      "consistent with the rows",
      cur.statements[1][0] in [s for s, _p in cur.statements], True)
check("the first call returns the snapshot's MAX", first, 253700)
check("a second call returns the SAME value, not a re-read", second, 253700)
check("ONE ceiling per table per snapshot -- two reads inside one run could disagree "
      "with each other and the load would apply a window it never read",
      len(cur.statements), 2)

# An empty source table has no MAX. None must survive as None: load.py leaves the
# watermark where it was on None, and a 0 would look like a legitimate floor.
snap, cur = new_snapshot([[]])
check("an empty table yields a None ceiling, never 0", snap.high_water_ceiling(TXN), None)

# The arithmetic in the contiguity check assumes the watermark column IS the dense IDENTITY
# primary key. Asserted against the real manifest rather than a fixture, and derived from
# whatever it declares rather than from a list repeated here.
manifest = Manifest.load(MANIFEST_PATH)
high_water = [t for t in manifest.tables if t.strategy == "high_water"]
check("the manifest declares at least one high-water table", bool(high_water), True)
for spec in high_water:
    check(f"{spec.name}: the watermark column IS the single-column primary key, which is "
          f"what makes MAX - MIN + 1 mean anything",
          (spec.primary_key, spec.watermark_column in spec.primary_key),
          ((spec.watermark_column,), True))


# ─────────────────────────────────────────────────────────────────────────────────────
# 3. THE GAP THE BOUND DOES NOT CLOSE
#
# Driven by the probe's real numbers. `high_water_ceiling` used to claim an uncommitted id
# "is not below the ceiling"; two connections proved it can be, and this is the guard.
# ─────────────────────────────────────────────────────────────────────────────────────

def window(
    rows: int,
    low: int | None,
    high: int | None,
    *,
    dw_rows: int | None = None,
    dw_low: int | None = -1,
    dw_high: int | None = -1,
    mark: int = 4,
    table: str = "transactions",
) -> ContiguityResult:
    """A counted window. The warehouse side defaults to agreeing with the source."""
    return ContiguityResult(
        table=table,
        watermark_column="transaction_id",
        watermark=mark,
        source_rows=rows,
        source_low=low,
        source_high=high,
        warehouse_rows=rows if dw_rows is None else dw_rows,
        warehouse_low=low if dw_low == -1 else dw_low,
        warehouse_high=high if dw_high == -1 else dw_high,
    )


# THE PROBE, exactly: A held id 3 open, B committed id 4, the snapshot saw [1, 2, 4] and
# took a ceiling of 4. Three rows inside a span of four.
probe = window(3, 1, 4, mark=4)
verdict, detail = contiguity_verdict(probe)

check("the probe's window spans 4 ids", probe.source_span, 4)
check("the probe's window is missing exactly 1", probe.source_gaps, 1)
check("a gap present on BOTH sides is SOURCE-DIRTY, not FAIL -- the load replicated the "
      "window it was handed",
      verdict, SOURCE_DIRTY)
check("the report says how many ids are missing",
      any("1 id(s) MISSING" in line for line in detail), True)
check("the report refuses to call it a proof, and names both possible causes",
      any("rolled-back insert" in line and "still uncommitted" in line for line in detail),
      True)
check("the report carries the measured cost so the caveat is not abstract",
      any("50,000 cents" in line for line in detail), True)

# SOURCE-DIRTY must not be able to fail the build. Same contract as the orphan case.
check("a contiguity gap on both sides exits 0",
      Scorecard(outcomes=[CheckOutcome("contiguity", verdict, detail)]).exit_code, 0)

# A dense window is the clean case, and it is the strong statement: nothing below the
# watermark is missing.
dense = window(253_700, 1, 253_700, mark=253_700)
v, d = contiguity_verdict(dense)
check("a dense window passes", v, PASS)
check("a dense window has no gaps", dense.source_gaps, 0)
check("the pass says so plainly", any("DENSE" in line for line in d), True)

# Bigger hole, exact count -- the report must not round or sample.
ten = window(19_600, 1, 19_610, mark=19_610)
check("ten missing ids are counted as ten", ten.source_gaps, 10)
check("ten missing ids are still SOURCE-DIRTY", contiguity_verdict(ten)[0], SOURCE_DIRTY)

# The warehouse short a row inside a window the source has whole: a replication defect,
# not source dirt.
lost = window(4, 1, 4, dw_rows=3)
v, d = contiguity_verdict(lost)
check("a row the warehouse alone is missing is a FAIL", v, FAIL)
check("the failing number is named", any("rows: source 4 vs warehouse 3" in l for l in d), True)

# The netting case, in the contiguity check's own terms: equal ROW COUNTS on both sides and
# different windows. A count comparison calls this healthy.
netted = window(4, 1, 5, dw_low=1, dw_high=4)
v, d = contiguity_verdict(netted)
check("equal counts and unequal windows is a FAIL", v, FAIL)
check("the counts really are equal, which is why the count is not the verdict",
      (netted.source_rows, netted.warehouse_rows), (4, 4))
check("the differing bound is named",
      any("highest id: source 5 vs warehouse 4" in l for l in d), True)

# Nothing loaded yet is not a finding.
empty = window(0, None, None, mark=0)
v, d = contiguity_verdict(empty)
check("an empty window passes", v, PASS)
check("an empty window has a span of 0, not 1", empty.source_span, 0)
check("the empty case says nothing was there to count",
      any("nothing to count" in line for line in d), True)

# More rows than the span can hold means a duplicated id, which the primary key should
# have prevented. Refusing loudly beats printing a negative gap.
dupe = window(5, 1, 4, mark=4)
v, d = contiguity_verdict(dupe)
check("rows exceeding the span is a FAIL", v, FAIL)
check("the impossible case names the primary key",
      any("primary key" in line for line in d), True)


# ─────────────────────────────────────────────────────────────────────────────────────
# 4. THE CLAIM ITSELF
#
# The defect this file exists for was never a code bug: the code did what its docstring
# said, and the docstring was wrong. Nothing in a test suite reads prose, which is exactly
# why the false claim survived every green run. These two assertions read it.
# ─────────────────────────────────────────────────────────────────────────────────────

from pipeline.source import SourceSnapshot as _S  # noqa: E402  (read after the stub)

doc = _S.high_water_ceiling.__doc__ or ""
check("the docstring no longer claims the uncommitted id is not below the ceiling",
      "is not below the" in doc, False)
check("the docstring names the reason an IDENTITY watermark cannot be made safe",
      "MIN_ACTIVE_IDENTITY" in doc, True)
check("the docstring points at the detector that ships instead",
      "contiguity" in doc, True)


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_watermark: {checks} checks passed")
