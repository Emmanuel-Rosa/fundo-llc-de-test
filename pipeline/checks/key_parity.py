"""Key parity: do the two sides hold the SAME ROWS?

THREE NUMBERS, NEVER ONE, and the reason is arithmetic rather than thoroughness:

    missing   in the source, absent from the warehouse -- replication LOST a row
    extra     in the warehouse, absent from the source -- replication KEPT a deleted row
    matched   present on both sides

One lost row and one stale row net to a row count that matches EXACTLY while two rows are
wrong. A row-count check reports that as healthy, which is why the count is never the
verdict here and why `missing` and `extra` are never summed into one "differences" figure.
They are different defects: the first loses money that exists, the second bills money that
does not.

AGGREGATE FIRST, DRILL DOWN ONLY ON DISAGREEMENT. Pulling every primary key means moving
250,000 integers for `transactions` on every clean run, which is the shape of cost the
whole pipeline exists to avoid. Four scalars per table catch an insert, a delete, a
duplicate and a substitution; the full key sets are fetched only for a table whose scalars
already disagree, so the expensive path runs exactly when something is wrong.

WHAT THE SCALARS CANNOT CATCH, stated rather than implied: a swap that preserves count,
sum, min and max simultaneously -- exchanging keys 10 and 20 for 12 and 18, say. The
composite key set is what would catch that, and on a clean run it is deliberately not read.
This check is a strong filter, not a proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import TableSpec

# How many differing ids to name in the report before truncating. Counts stay exact; only
# the illustrative list is capped. Same contract as score.py's _ID_SAMPLE.
_ID_SAMPLE = 12


@dataclass(frozen=True)
class KeyParityResult:
    """One table's key comparison.

    `drilled` records whether the full key sets were read. It is part of the result rather
    than an implementation detail, because "0 missing" means something weaker when it was
    inferred from four scalars than when it was computed from the sets themselves, and a
    report that hides the difference is overclaiming.
    """

    table: str
    source_rows: int
    warehouse_rows: int
    missing: tuple[int, ...]
    extra: tuple[int, ...]
    drilled: bool
    scalars_agree: bool

    @property
    def matched(self) -> int:
        return self.source_rows - len(self.missing)

    @property
    def ok(self) -> bool:
        return self.scalars_agree and not self.missing and not self.extra

    @property
    def counts_agree_but_keys_do_not(self) -> bool:
        """The netting case this check exists for: equal row counts, unequal key sets."""
        return (
            self.source_rows == self.warehouse_rows
            and bool(self.missing or self.extra)
        )


def _key_scalars_source(snapshot: Any, table: TableSpec) -> tuple[Any, ...]:
    key = table.primary_key[0]
    row = snapshot.query(
        f"SELECT COUNT(*) AS n, SUM(CAST([{key}] AS BIGINT)) AS s, "
        f"MIN([{key}]) AS lo, MAX([{key}]) AS hi FROM dbo.[{table.name}]"
    )[0]
    return (row["n"], row["s"], row["lo"], row["hi"])


def _key_scalars_warehouse(conn: Any, table: TableSpec) -> tuple[Any, ...]:
    key = table.primary_key[0]
    row = conn.execute(
        f'SELECT COUNT(*), SUM(CAST("{key}" AS BIGINT)), MIN("{key}"), MAX("{key}") '
        f'FROM raw."{table.name}"'
    ).fetchone()
    return tuple(row)


def _normalise(values: tuple[Any, ...]) -> tuple[Any, ...]:
    """Make the two engines' scalars comparable.

    pymssql returns Decimal for SUM over BIGINT while DuckDB returns int, and `SUM` over an
    empty table is NULL on both. Comparing the raw tuples would report every table as
    differing on type alone -- a check that fails on a clean run gets switched off, which
    is worse than no check.
    """
    out: list[Any] = []
    for v in values:
        if v is None:
            out.append(None)
        elif isinstance(v, bool):
            out.append(int(v))
        else:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                out.append(v)
    return tuple(out)


def check_key_parity(snapshot: Any, conn: Any, table: TableSpec) -> KeyParityResult:
    """Compare one table's keys. `snapshot` is a SourceSnapshot; `conn` is DuckDB."""
    if len(table.primary_key) != 1:
        # Every table in this manifest has a single-column key, and the aggregate path
        # below assumes it. Refusing loudly beats silently checking only the first column.
        raise ValueError(
            f"{table.name}: key parity handles single-column primary keys, got "
            f"{table.primary_key}"
        )

    src = _normalise(_key_scalars_source(snapshot, table))
    dw = _normalise(_key_scalars_warehouse(conn, table))
    scalars_agree = src == dw

    source_rows, warehouse_rows = src[0] or 0, dw[0] or 0

    if scalars_agree:
        return KeyParityResult(
            table=table.name,
            source_rows=source_rows,
            warehouse_rows=warehouse_rows,
            missing=(),
            extra=(),
            drilled=False,
            scalars_agree=True,
        )

    # The expensive path, reached only because the cheap one already disagreed.
    key = table.primary_key[0]
    src_keys = {
        int(r[key]) for r in snapshot.query(f"SELECT [{key}] FROM dbo.[{table.name}]")
    }
    dw_keys = {
        int(r[0])
        for r in conn.execute(f'SELECT "{key}" FROM raw."{table.name}"').fetchall()
    }
    return KeyParityResult(
        table=table.name,
        source_rows=source_rows,
        warehouse_rows=warehouse_rows,
        missing=tuple(sorted(src_keys - dw_keys)),
        extra=tuple(sorted(dw_keys - src_keys)),
        drilled=True,
        scalars_agree=False,
    )


def describe(result: KeyParityResult) -> str:
    """One line of detail for the scorecard. Counts exact, id list capped."""
    if result.ok:
        return f"{result.matched:,} keys matched"

    parts = [f"{len(result.missing):,} missing", f"{len(result.extra):,} extra"]
    if result.counts_agree_but_keys_do_not:
        parts.append(
            f"row counts BOTH {result.source_rows:,} -- they net out, which is exactly "
            f"why a count is not the verdict"
        )
    if result.missing:
        parts.append("missing ids " + _ids(result.missing))
    if result.extra:
        parts.append("extra ids " + _ids(result.extra))
    if not result.drilled:
        parts.append(
            "scalars disagree but the key sets were not read -- this is a bug in "
            "check_key_parity, which should have drilled down"
        )
    return "; ".join(parts)


def _ids(values: tuple[int, ...]) -> str:
    shown = ", ".join(str(v) for v in values[:_ID_SAMPLE])
    if len(values) > _ID_SAMPLE:
        shown += f", ... ({len(values):,} total)"
    return shown
