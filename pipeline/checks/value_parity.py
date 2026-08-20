"""Value parity: the two sides hold the same rows -- do those rows hold the same VALUES?

Key parity can pass while every amount is wrong. This is the check that reads the money.

ONE TYPE-APPROPRIATE AGGREGATE PER COLUMN, not one checksum per table. A single hash is
cheaper and says "transactions is wrong" and nothing else -- not which column, not whether
it is money or whitespace. The whole reason to build this is the break-and-restore demo,
where the interesting sentence is "row counts pass, key counts pass, and amount_cents is
short by a measurable number of cents". A table-level hash cannot say that.

EVERY AGGREGATE HERE MUST BE PORTABLE BETWEEN SQL SERVER AND DUCKDB, and that constraint
did real work on the design. A check that false-fails on clean data gets switched off, so
three tempting aggregates were rejected outright:

  * LEN() -- SQL Server's LEN silently ignores TRAILING spaces, DuckDB's LENGTH does not.
    This fixture deliberately seeds padded names (' Nguyen '), so LEN would have reported a
    difference that does not exist. The `LEN(col + 'x') - 1` form below is the standard way
    to make LEN count trailing spaces, and it reads identically on both engines.
  * COUNT(DISTINCT <text>) -- the default SQL Server collation is CASE-INSENSITIVE, so
    'MCDONALD' and 'McDonald' count as ONE value there and TWO in DuckDB. This fixture
    seeds exactly that pair, so this aggregate would fail every clean run.
  * MIN/MAX on text -- same collation problem, plus accent handling. The fixture seeds
    'Jose'/'Jose' with a diacritic, so ordering is not comparable across the two engines
    without forcing a binary collation, and forcing one to make a check work is how a
    check starts lying.

WHAT SURVIVES, AND THE GAP IT LEAVES. Integers get COUNT, SUM, MIN and MAX -- strong, and
enough to catch the break demo exactly, because amount_cents is a BIGINT that reconciles to
the cent. Text gets COUNT and a summed character length, which means a same-length text
swap in the middle of a table survives this check. That is a real hole and it is printed
rather than implied.

advances.principal_amount is OMITTED, and the rule is about the TYPE rather than the
column: float and real are not summable reproducibly, so they are named and skipped instead
of reconciled with a tolerance and called a pass. 02e already sets up the contrast --
amount_cents is a BIGINT and reconciles exactly -- and having both in one schema is the
point being made, so the omission is a stated result rather than a gap in the check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..config import TableSpec

_ROW_SAMPLE = 8

# SQL Server DATA_TYPE values, grouped by what can be aggregated portably.
_INTEGER_TYPES = {"int", "bigint", "smallint", "tinyint"}
_EXACT_DECIMAL_TYPES = {"decimal", "numeric", "money", "smallmoney"}
_INEXACT_TYPES = {"float", "real"}
_DATE_TYPES = {"date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time"}
_TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}
_BOOL_TYPES = {"bit"}


@dataclass(frozen=True)
class ColumnComparison:
    column: str
    data_type: str
    aggregate: str
    source_value: Any
    warehouse_value: Any

    @property
    def agrees(self) -> bool:
        return self.source_value == self.warehouse_value

    @property
    def delta(self) -> Any:
        """Signed warehouse-minus-source, when both sides are numbers. Else None."""
        if isinstance(self.source_value, int) and isinstance(self.warehouse_value, int):
            return self.warehouse_value - self.source_value
        return None


@dataclass(frozen=True)
class RowDifference:
    key: int
    column: str
    source_value: Any
    warehouse_value: Any


@dataclass
class ValueParityResult:
    table: str
    comparisons: list[ColumnComparison] = field(default_factory=list)
    omitted: list[tuple[str, str, str]] = field(default_factory=list)  # col, type, why
    row_differences: list[RowDifference] = field(default_factory=list)
    drilled: bool = False

    @property
    def disagreeing(self) -> list[ColumnComparison]:
        return [c for c in self.comparisons if not c.agrees]

    @property
    def ok(self) -> bool:
        return not self.disagreeing


def _classify(data_type: str) -> str:
    dt = data_type.lower()
    if dt in _INEXACT_TYPES:
        return "inexact"
    if dt in _INTEGER_TYPES or dt in _EXACT_DECIMAL_TYPES:
        return "number"
    if dt in _BOOL_TYPES:
        return "bool"
    if dt in _DATE_TYPES:
        return "date"
    if dt in _TEXT_TYPES:
        return "text"
    return "unknown"


def _source_types(snapshot: Any, table: TableSpec) -> dict[str, str]:
    rows = snapshot.query(
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        f"WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = '{table.name}'"
    )
    return {r["COLUMN_NAME"]: str(r["DATA_TYPE"]) for r in rows}


def _aggregates(column: str, kind: str) -> list[tuple[str, str, str]]:
    """(label, sql-server-expression, duckdb-expression) for one column."""
    s, d = f"[{column}]", f'"{column}"'
    if kind == "bool":
        # NO MIN/MAX. SQL Server rejects both on `bit` outright -- "Operand data type bit
        # is invalid for min operator" -- which is a hard error rather than a wrong number,
        # so the first live run against cards.is_default was the thing that found it. The
        # fake-engine unit tests could not have: they answer whatever they are handed.
        # SUM needs the CAST too, because SUM(bit) is refused for the same reason.
        return [
            ("COUNT", f"COUNT({s})", f"COUNT({d})"),
            ("SUM", f"SUM(CAST({s} AS BIGINT))", f"SUM(CAST({d} AS BIGINT))"),
        ]
    if kind == "number":
        return [
            ("COUNT", f"COUNT({s})", f"COUNT({d})"),
            ("SUM", f"SUM(CAST({s} AS BIGINT))", f"SUM(CAST({d} AS BIGINT))"),
            ("MIN", f"MIN({s})", f"MIN({d})"),
            ("MAX", f"MAX({s})", f"MAX({d})"),
        ]
    if kind == "date":
        return [
            ("COUNT", f"COUNT({s})", f"COUNT({d})"),
            ("MIN", f"MIN({s})", f"MIN({d})"),
            ("MAX", f"MAX({s})", f"MAX({d})"),
        ]
    if kind == "text":
        # See the module docstring: the + 'x' - 1 form is what makes trailing spaces count
        # on SQL Server, where a bare LEN() would drop them and report a false difference.
        return [
            ("COUNT", f"COUNT({s})", f"COUNT({d})"),
            ("CHARS",
             f"SUM(CAST(LEN(CAST({s} AS NVARCHAR(MAX)) + N'x') - 1 AS BIGINT))",
             f"SUM(CAST(LENGTH(CAST({d} AS VARCHAR) || 'x') - 1 AS BIGINT))"),
        ]
    return [("COUNT", f"COUNT({s})", f"COUNT({d})")]


def _norm(value: Any) -> Any:
    """Make one engine's scalar comparable with the other's.

    pymssql hands back Decimal for SUM and datetime for dates; DuckDB hands back int and
    date/datetime. Dates are compared as ISO strings so a date and a midnight datetime for
    the same day do not read as a difference in replication when they are a difference in
    driver typing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def column_kinds(snapshot: Any, table: TableSpec) -> dict[str, str]:
    """column -> portability class, for other checks that need the same type map.

    Public because the append-only check needs exactly this and re-deriving it there would
    let the two checks disagree about which columns are summable.
    """
    return {col: _classify(dt) for col, dt in _source_types(snapshot, table).items()}


def check_value_parity(snapshot: Any, conn: Any, table: TableSpec) -> ValueParityResult:
    result = ValueParityResult(table=table.name)
    types = _source_types(snapshot, table)

    src_exprs: list[tuple[str, str, str, str]] = []   # col, label, sql, kind
    dw_exprs: list[str] = []

    for column in table.columns:
        data_type = types.get(column, "unknown")
        kind = _classify(data_type)
        if kind == "inexact":
            result.omitted.append((
                column, data_type,
                f"{data_type.upper()} is not summable reproducibly -- stated, not "
                f"reconciled with a tolerance and called a pass",
            ))
            continue
        if kind == "unknown":
            result.omitted.append((
                column, data_type,
                "no portable aggregate is defined for this type -- skipped loudly rather "
                "than compared with a guess",
            ))
            continue
        for label, s_expr, d_expr in _aggregates(column, kind):
            src_exprs.append((column, label, s_expr, data_type))
            dw_exprs.append(d_expr)

    if not src_exprs:
        return result

    src_row = snapshot.query(
        "SELECT " + ", ".join(f"{e[2]} AS c{i}" for i, e in enumerate(src_exprs))
        + f" FROM dbo.[{table.name}]"
    )[0]
    dw_row = conn.execute(
        "SELECT " + ", ".join(dw_exprs) + f' FROM raw."{table.name}"'
    ).fetchone()

    for i, (column, label, _sql, data_type) in enumerate(src_exprs):
        result.comparisons.append(ColumnComparison(
            column=column,
            data_type=data_type,
            aggregate=label,
            source_value=_norm(src_row[f"c{i}"]),
            warehouse_value=_norm(dw_row[i]),
        ))

    if result.disagreeing:
        _drill(snapshot, conn, table, result)
    return result


def _drill(snapshot: Any, conn: Any, table: TableSpec, result: ValueParityResult) -> None:
    """Name the rows behind a failing column. Runs ONLY because an aggregate disagreed.

    This is the expensive read the aggregate pass exists to avoid on a clean run -- it
    pulls the key and the one failing column from both sides. It is worth paying exactly
    when something is broken, and never otherwise.
    """
    key = table.primary_key[0]
    for column in sorted({c.column for c in result.disagreeing}):
        src = {
            int(r[key]): _norm(r[column])
            for r in snapshot.query(
                f"SELECT [{key}], [{column}] FROM dbo.[{table.name}]"
            )
        }
        dw = {
            int(r[0]): _norm(r[1])
            for r in conn.execute(
                f'SELECT "{key}", "{column}" FROM raw."{table.name}"'
            ).fetchall()
        }
        for k in sorted(set(src) & set(dw)):
            if src[k] != dw[k]:
                result.row_differences.append(
                    RowDifference(key=k, column=column,
                                  source_value=src[k], warehouse_value=dw[k])
                )
    result.drilled = True


def describe(result: ValueParityResult) -> list[str]:
    """Detail lines for the scorecard. Counts exact, row list capped."""
    lines: list[str] = []
    if result.ok:
        lines.append(
            f"{len(result.comparisons)} column aggregate(s) agree"
            + (f"; {len(result.omitted)} column(s) omitted" if result.omitted else "")
        )
    else:
        for c in result.disagreeing:
            delta = c.delta
            tail = f"  delta {delta:+,}" if delta is not None else ""
            lines.append(
                f"{result.table}.{c.column} {c.aggregate}: source {c.source_value!r} vs "
                f"warehouse {c.warehouse_value!r}{tail}"
            )
        shown = result.row_differences[:_ROW_SAMPLE]
        if shown:
            lines.append(
                f"rows behind it (read only because an aggregate disagreed; "
                f"{len(result.row_differences):,} row(s) differ):"
            )
            for d in shown:
                lines.append(
                    f"  {result.table[:-1] if result.table.endswith('s') else result.table}"
                    f" {d.key}  {d.column}  source {d.source_value!r} -> warehouse "
                    f"{d.warehouse_value!r}"
                )
            if len(result.row_differences) > _ROW_SAMPLE:
                lines.append(f"  ... and {len(result.row_differences) - _ROW_SAMPLE:,} more")
        elif result.drilled:
            lines.append(
                "no single row differs, so the aggregate gap comes from rows present on "
                "one side only -- read the key parity line above, not this one"
            )
    for column, data_type, why in result.omitted:
        lines.append(f"OMITTED {result.table}.{column} ({data_type}): {why}")
    return lines
