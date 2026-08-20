"""The scorecard: run every check, render one table, decide the exit code.

Compute and render are separate here for the same reason they are in resolve.py and
score.py -- the check modules return dataclasses and know nothing about formatting, so the
report can change without touching a comparison.

Three of the five checks live in this file rather than getting a module each, because they
are a handful of queries and splitting them would be structure for its own sake:

  APPEND-ONLY, which is the check the break-and-restore demo trips. tables.yml asserts
  `append_only_asserted: true` on transactions and customer_history. Append-only means
  history is never rewritten, so the segment at or below the loaded watermark is FROZEN and
  its aggregates must match on both sides forever. Editing a historical amount is a rewrite
  inside that segment. Whole-table value parity catches it too, but this is the check that
  NAMES it: "history was rewritten below the watermark" is the actionable diagnosis, and it
  is the exact bet a high-water strategy makes.

  CONTIGUITY, which is the only thing in this repo that can catch the high-water mark's
  actual hole. `high_water_ceiling` bounds the window from above; a row still uncommitted
  when the snapshot opened, sitting below an id that already committed, falls into a gap
  INSIDE that window and is buried the moment the watermark advances. That is measured,
  not theorised -- see that method's docstring. COUNT(*) against MAX(id) - MIN(id) + 1
  over the loaded window finds the gap for the cost of three scalars per table. It is a
  HEURISTIC and is reported as one: a rolled-back insert burns an IDENTITY value
  permanently, so a gap has two possible causes and this check cannot separate them.
  Naming a gap an operator can go and explain beats a docstring promising the gap is
  impossible.

  REFERENTIAL INTEGRITY, which is where the three-tier verdict earns its place. Orphans are
  checked on BOTH sides and the verdict depends on which side they are on:
      orphaned in the source AND the warehouse -> SOURCE-DIRTY. Faithful replication.
      orphaned in the warehouse ONLY           -> FAIL. The parent existed and the load
                                                  lost it -- a real defect wearing the
                                                  costume of source dirt.
  A boolean verdict cannot express that difference, and nothing looking only at the
  warehouse can tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..config import Manifest, TableSpec
from . import key_parity, value_parity

# TYPE-ONLY, and this is load-bearing rather than tidiness. `Source` imports pymssql and
# `Warehouse` imports duckdb, so importing them at runtime would make `pipeline.checks`
# undecidable on a machine with neither -- and tests/test_checks.py exists precisely to
# test the comparison arithmetic without a database. Everything below uses these objects
# by their behaviour (`source.snapshot()`, `warehouse.connection`, `warehouse.watermark`),
# never by their type, so nothing is lost.
if TYPE_CHECKING:                                    # pragma: no cover
    from ..source import Source
    from ..warehouse import Warehouse

PASS = "PASS"
SOURCE_DIRTY = "SOURCE-DIRTY"
FAIL = "FAIL"

# Worst-wins, and the order is the precedence. FAIL beats SOURCE-DIRTY beats PASS.
_SEVERITY = {PASS: 0, SOURCE_DIRTY: 1, FAIL: 2}

_WIDTH = 96
_ID_SAMPLE = 12

# Parent/child links. The source declares no foreign key on several of these -- which is
# why this check exists rather than being redundant with the database's own constraints.
_RELATIONSHIPS: tuple[tuple[str, str, str, str], ...] = (
    # (child table, child column, parent table, parent column)
    ("advances", "customer_id", "customers", "customer_id"),
    ("cards", "customer_id", "customers", "customer_id"),
    ("transactions", "customer_id", "customers", "customer_id"),
    ("customer_history", "customer_id", "customers", "customer_id"),
)


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    verdict: str
    detail: list[str]


@dataclass
class Scorecard:
    outcomes: list[CheckOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.outcomes:
            return PASS
        return max((o.verdict for o in self.outcomes), key=lambda v: _SEVERITY[v])

    @property
    def exit_code(self) -> int:
        """Non-zero ONLY for FAIL. SOURCE-DIRTY is a finding, not a broken pipeline."""
        return 1 if self.verdict == FAIL else 0


def _int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


# ─────────────────────────────────────────────────────────────────────────────────────
# Append-only
# ─────────────────────────────────────────────────────────────────────────────────────

def _check_append_only(
    snapshot: Any, warehouse: "Warehouse", tables: tuple[TableSpec, ...]
) -> CheckOutcome:
    conn = warehouse.connection
    detail: list[str] = []
    verdict = PASS
    checked = 0

    for table in tables:
        if not table.append_only_asserted or not table.watermark_column:
            continue
        mark = warehouse.watermark(table.name)
        if mark is None:
            detail.append(
                f"{table.name}: no watermark recorded yet, so there is no frozen segment "
                f"to compare -- not checked"
            )
            continue
        checked += 1
        wm = table.watermark_column

        # Every summable column in the frozen segment. COUNT(*) alone would miss a rewrite
        # that changes a value without changing the row count -- which is the only kind of
        # rewrite worth worrying about here.
        kinds = value_parity.column_kinds(snapshot, table)
        numeric = [c for c in table.columns if kinds.get(c) in ("number", "bool")]

        src_sel = ", ".join(
            [f"COUNT(*) AS c0"]
            + [f"SUM(CAST([{c}] AS BIGINT)) AS c{i+1}" for i, c in enumerate(numeric)]
        )
        dw_sel = ", ".join(
            ["COUNT(*)"] + [f'SUM(CAST("{c}" AS BIGINT))' for c in numeric]
        )
        src = snapshot.query(
            f"SELECT {src_sel} FROM dbo.[{table.name}] WHERE [{wm}] <= {int(mark)}"
        )[0]
        dw = conn.execute(
            f'SELECT {dw_sel} FROM raw."{table.name}" WHERE "{wm}" <= {int(mark)}'
        ).fetchone()

        labels = ["row count"] + numeric
        src_vals = [_int(src[f"c{i}"]) for i in range(len(labels))]
        dw_vals = [_int(v) for v in dw]

        drifted = [
            (label, s, d)
            for label, s, d in zip(labels, src_vals, dw_vals) if s != d
        ]
        if drifted:
            verdict = FAIL
            detail.append(
                f"{table.name}: HISTORY WAS REWRITTEN below the watermark "
                f"({wm} <= {mark:,}) -- this table is declared append-only in tables.yml"
            )
            for label, s, d in drifted:
                detail.append(f"  {label}: source {s:,} vs warehouse {d:,} "
                              f"(delta {d - s:+,})")
        else:
            detail.append(
                f"{table.name}: frozen segment {wm} <= {mark:,} agrees on row count and "
                f"{len(numeric)} summable column(s)"
            )

    if checked:
        detail.append(
            "WHAT THIS CANNOT SEE: only changes that move a sum. Two edits inside the "
            "frozen segment that cancel exactly (+500 and -500) pass this check, and a "
            "same-length text change is not summed at all."
        )
    return CheckOutcome("append-only", verdict, detail)


# ─────────────────────────────────────────────────────────────────────────────────────
# Contiguity -- the high-water hazard detector
# ─────────────────────────────────────────────────────────────────────────────────────

def _opt_int(value: Any) -> int | None:
    """Like _int, but keeps NULL as None. An empty window has no MIN and no MAX, and
    coercing those to 0 would invent a span of 1 for a table holding nothing."""
    return None if value is None else _int(value)


@dataclass(frozen=True)
class ContiguityResult:
    """One high-water table's loaded window, counted on both sides.

    A dense IDENTITY window holds exactly MAX - MIN + 1 rows. The SHORTFALL against that is
    the number of ids the window does not have -- the only signal available at this price
    that the watermark advanced past something.
    """

    table: str
    watermark_column: str
    watermark: int
    source_rows: int
    source_low: int | None
    source_high: int | None
    warehouse_rows: int
    warehouse_low: int | None
    warehouse_high: int | None

    @staticmethod
    def _span(low: int | None, high: int | None) -> int:
        return 0 if low is None or high is None else high - low + 1

    @property
    def source_span(self) -> int:
        return self._span(self.source_low, self.source_high)

    @property
    def warehouse_span(self) -> int:
        return self._span(self.warehouse_low, self.warehouse_high)

    @property
    def source_gaps(self) -> int:
        return self.source_span - self.source_rows

    @property
    def warehouse_gaps(self) -> int:
        return self.warehouse_span - self.warehouse_rows

    @property
    def windows_agree(self) -> bool:
        """All three numbers, not just the count -- see key_parity on why a count nets out."""
        return (self.source_rows, self.source_low, self.source_high) == (
            self.warehouse_rows,
            self.warehouse_low,
            self.warehouse_high,
        )


def contiguity_verdict(r: ContiguityResult) -> tuple[str, list[str]]:
    """One table's verdict from the counted window. No database, no formatting.

    THE TWO-SIDED SPLIT is the same argument as the referential check: a gap the SOURCE has
    as well was replicated faithfully and is a fact about the source, while a window only
    the WAREHOUSE is short of is a replication defect.
    """
    wm, mark = r.watermark_column, r.watermark

    if not r.windows_agree:
        lines = [
            f"{r.table}: the warehouse's window is NOT the source's window at "
            f"{wm} <= {mark:,} -- what the load holds is not what the source has"
        ]
        for label, s, d in (
            ("rows", r.source_rows, r.warehouse_rows),
            ("lowest id", r.source_low, r.warehouse_low),
            ("highest id", r.source_high, r.warehouse_high),
        ):
            if s != d:
                lines.append(
                    f"  {label}: source {s:,} vs warehouse {d:,}"
                    if s is not None and d is not None
                    else f"  {label}: source {s} vs warehouse {d}"
                )
        return FAIL, lines

    if r.source_rows == 0:
        return PASS, [f"{r.table}: no rows at or below the watermark yet, nothing to count"]

    if r.source_gaps < 0:
        # Impossible under the primary key: more rows than the span can hold means an id is
        # duplicated. Saying so beats printing a negative gap count.
        return FAIL, [
            f"{r.table}: {r.source_rows:,} rows inside a span of {r.source_span:,} ids "
            f"({wm} {r.source_low:,}..{r.source_high:,}) -- an id is duplicated, which the "
            f"primary key should have made impossible"
        ]

    if r.source_gaps > 0:
        return SOURCE_DIRTY, [
            f"{r.table}: {r.source_gaps:,} id(s) MISSING from the loaded window -- "
            f"{wm} {r.source_low:,}..{r.source_high:,} spans {r.source_span:,} ids and "
            f"holds {r.source_rows:,}, on BOTH sides",
            "  a gap has two causes and three scalars cannot separate them: a rolled-back "
            "insert burns an IDENTITY value permanently (benign), or the watermark advanced "
            "past a row still uncommitted while a higher id committed (the hazard "
            "high_water_ceiling documents, 50,000 cents in the probe that proved it)",
        ]

    return PASS, [
        f"{r.table}: window {wm} <= {mark:,} is DENSE -- {r.source_rows:,} rows across ids "
        f"{r.source_low:,}..{r.source_high:,}, so no id below the watermark is missing"
    ]


def _check_contiguity(
    snapshot: Any, warehouse: "Warehouse", tables: tuple[TableSpec, ...]
) -> CheckOutcome:
    conn = warehouse.connection
    detail: list[str] = []
    verdict = PASS
    checked = 0

    for table in tables:
        if table.strategy != "high_water" or not table.watermark_column:
            continue
        mark = warehouse.watermark(table.name)
        if mark is None:
            detail.append(
                f"{table.name}: no watermark recorded yet, so there is no loaded window to "
                f"count -- not checked"
            )
            continue
        checked += 1
        wm = table.watermark_column

        src = snapshot.query(
            f"SELECT COUNT(*) AS n, MIN([{wm}]) AS lo, MAX([{wm}]) AS hi "
            f"FROM dbo.[{table.name}] WHERE [{wm}] <= {int(mark)}"
        )[0]
        dw = conn.execute(
            f'SELECT COUNT(*), MIN("{wm}"), MAX("{wm}") '
            f'FROM raw."{table.name}" WHERE "{wm}" <= {int(mark)}'
        ).fetchone()

        result = ContiguityResult(
            table=table.name,
            watermark_column=wm,
            watermark=int(mark),
            source_rows=_int(src["n"]),
            source_low=_opt_int(src["lo"]),
            source_high=_opt_int(src["hi"]),
            warehouse_rows=_int(dw[0]),
            warehouse_low=_opt_int(dw[1]),
            warehouse_high=_opt_int(dw[2]),
        )
        table_verdict, lines = contiguity_verdict(result)
        if _SEVERITY[table_verdict] > _SEVERITY[verdict]:
            verdict = table_verdict
        detail.extend(lines)

    if checked:
        detail.append(
            "WHAT THIS CANNOT SEE: a gap BELOW the lowest id present. MIN comes from the "
            "rows that are there, so an id missing underneath it leaves no trace. The top "
            "end needs no such caveat -- the ceiling is always an id the snapshot could "
            "see, so MAX inside the window is the watermark itself."
        )
    return CheckOutcome("contiguity", verdict, detail)


# ─────────────────────────────────────────────────────────────────────────────────────
# Referential integrity
# ─────────────────────────────────────────────────────────────────────────────────────

def _orphans_source(snapshot: Any, child: str, col: str, parent: str, pcol: str) -> set[int]:
    rows = snapshot.query(
        f"SELECT c.[{col}] AS k FROM dbo.[{child}] c "
        f"LEFT JOIN dbo.[{parent}] p ON p.[{pcol}] = c.[{col}] "
        f"WHERE c.[{col}] IS NOT NULL AND p.[{pcol}] IS NULL "
        f"GROUP BY c.[{col}]"
    )
    return {int(r["k"]) for r in rows}


def _orphans_warehouse(conn: Any, child: str, col: str, parent: str, pcol: str) -> set[int]:
    rows = conn.execute(
        f'SELECT DISTINCT c."{col}" FROM raw."{child}" c '
        f'LEFT JOIN raw."{parent}" p ON p."{pcol}" = c."{col}" '
        f'WHERE c."{col}" IS NOT NULL AND p."{pcol}" IS NULL'
    ).fetchall()
    return {int(r[0]) for r in rows}


def _check_referential(
    snapshot: Any, warehouse: "Warehouse", present: set[str]
) -> CheckOutcome:
    conn = warehouse.connection
    detail: list[str] = []
    verdict = PASS

    for child, col, parent, pcol in _RELATIONSHIPS:
        if child not in present or parent not in present:
            continue
        src = _orphans_source(snapshot, child, col, parent, pcol)
        dw = _orphans_warehouse(conn, child, col, parent, pcol)

        warehouse_only = dw - src
        both = dw & src

        if warehouse_only:
            verdict = FAIL
            detail.append(
                f"{child}.{col} -> {parent}: {len(warehouse_only):,} value(s) orphaned in "
                f"the WAREHOUSE ONLY -- the parent exists in the source, so the load lost "
                f"it. Ids: {_ids(warehouse_only)}"
            )
        if both:
            if verdict != FAIL:
                verdict = SOURCE_DIRTY
            detail.append(
                f"{child}.{col} -> {parent}: {len(both):,} value(s) orphaned in BOTH -- "
                f"the source really is like that and it was replicated faithfully. "
                f"Ids: {_ids(both)}"
            )
        if not warehouse_only and not both:
            detail.append(f"{child}.{col} -> {parent}: no orphans on either side")

    if verdict == SOURCE_DIRTY:
        detail.append(
            "SOURCE-DIRTY is not a failure. Repairing these in flight would hide a source "
            "problem behind a clean-looking warehouse, so they are reported and kept."
        )
    return CheckOutcome("referential integrity", verdict, detail)


def _ids(values: set[int]) -> str:
    ordered = sorted(values)
    shown = ", ".join(str(v) for v in ordered[:_ID_SAMPLE])
    if len(ordered) > _ID_SAMPLE:
        shown += f", ... ({len(ordered):,} total)"
    return shown


# ─────────────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────────────

def run_scorecard(
    source: "Source", warehouse: "Warehouse", manifest: Manifest
) -> Scorecard:
    """Run every check against both sides. Reads happen in ONE source snapshot.

    The snapshot matters: these checks read five tables and take seconds to do it, so
    without it `customers` could be read before a write and `transactions` after it, and
    the report would name a difference that never existed at any single instant. Same
    argument as one snapshot per load run.
    """
    card = Scorecard()
    conn = warehouse.connection
    present = {
        r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw'"
        ).fetchall()
    }
    tables = tuple(t for t in manifest.tables if t.name in present)
    if len(tables) != len(manifest.tables):
        missing = sorted({t.name for t in manifest.tables} - present)
        card.notes.append(
            f"not in the warehouse yet and therefore not checked: {', '.join(missing)}"
        )

    with source.snapshot() as snapshot:
        # ── key parity ────────────────────────────────────────────────────────────────
        key_results = [key_parity.check_key_parity(snapshot, conn, t) for t in tables]
        key_detail = [f"{r.table:<18} {key_parity.describe(r)}" for r in key_results]
        key_verdict = FAIL if any(not r.ok for r in key_results) else PASS
        if any(r.counts_agree_but_keys_do_not for r in key_results):
            key_detail.append(
                "at least one table has EQUAL ROW COUNTS and unequal key sets -- a lost "
                "row and a stale row net out, which is why three numbers are reported "
                "instead of a count"
            )
        card.outcomes.append(CheckOutcome("key parity", key_verdict, key_detail))

        # ── value parity ──────────────────────────────────────────────────────────────
        val_results = [value_parity.check_value_parity(snapshot, conn, t) for t in tables]
        val_detail: list[str] = []
        for r in val_results:
            for line in value_parity.describe(r):
                val_detail.append(f"{r.table:<18} {line}" if r.ok else line)
        val_verdict = FAIL if any(not r.ok for r in val_results) else PASS
        card.outcomes.append(CheckOutcome("value parity", val_verdict, val_detail))

        # ── append-only ───────────────────────────────────────────────────────────────
        card.outcomes.append(_check_append_only(snapshot, warehouse, tables))

        # ── contiguity ────────────────────────────────────────────────────────────────
        card.outcomes.append(_check_contiguity(snapshot, warehouse, tables))

        # ── referential integrity ─────────────────────────────────────────────────────
        card.outcomes.append(_check_referential(snapshot, warehouse, present))

    return card


def format_scorecard(card: Scorecard) -> str:
    """ASCII only, same reason as every other report here: box-drawing characters crash a
    default cp1252 Windows console, and a reviewer piping `docker compose logs` through one
    is a real path rather than a hypothetical."""
    lines: list[str] = []
    lines.append("  REPLICATION SCORECARD -- both sides read, not just the loader's own log")
    lines.append("  " + "-" * _WIDTH)
    lines.append(f"  {'check':<24} {'verdict':<13} detail")
    lines.append("  " + "-" * _WIDTH)
    for o in card.outcomes:
        head = o.detail[0] if o.detail else ""
        lines.append(f"  {o.name:<24} {o.verdict:<13} {head}")
        for extra in o.detail[1:]:
            lines.append(f"  {'':<24} {'':<13} {extra}")
    lines.append("  " + "-" * _WIDTH)
    lines.append(f"  OVERALL {card.verdict}   exit {card.exit_code}")

    if card.verdict == SOURCE_DIRTY:
        lines.append("")
        lines.append("  SOURCE-DIRTY, and exiting 0 on purpose: the two sides AGREE. What they")
        lines.append("  agree on includes rows the source itself has orphaned. That is a finding")
        lines.append("  about the source, not a replication defect, and the alternative -- failing")
        lines.append("  the build on it -- would need a hardcoded expected-orphan count that goes")
        lines.append("  stale the first time the source changes.")
    if card.verdict == FAIL:
        lines.append("")
        lines.append("  FAILED, and exiting non-zero: the warehouse does not say what the source")
        lines.append("  says. Every line marked FAIL above names the table, the column and, where")
        lines.append("  a row-level read was warranted, the rows.")
    for note in card.notes:
        lines.append(f"\n  NOTE: {note}")
    return "\n".join(lines)
