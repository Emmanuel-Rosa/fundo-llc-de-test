"""The warehouse side: DuckDB schemas, the bookkeeping tables, and applying changes.

Two things here carry the weight of the exercise.

THE WATERMARK IS COMMITTED WITH THE ROWS, in one transaction. If the rows land and the
watermark does not, the next run re-applies them (harmless for an upsert, duplicates for
an append-only insert). If the watermark advances and the rows do not, those rows are
lost forever with no error. Splitting them is the difference between a crash being
recoverable and a crash being silent data loss, so they are never split.

THE SOURCE FINGERPRINT IS CHECKED BEFORE ANY READ. A watermark is only meaningful
relative to the database incarnation that issued it. See `assert_source_matches`.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import duckdb

from .config import Manifest, TableSpec, WarehouseConfig
from .source import ChangeRow, SourceFingerprint

# ─────────────────────────────────────────────────────────────────────────────────────
# Type mapping. Deliberately conservative and explicit rather than inferred.
#
# NOTE ON principal_amount: it arrives as a FLOAT because the source stores money in a
# float, and it is stored as DOUBLE here rather than being "fixed" on the way in. Casting
# it to DECIMAL during the load would hide the defect and produce a number that looks
# exact and is not -- the precision was already lost at the source. The value-parity check
# therefore refuses to reconcile that column and prints why. Repairing data in flight to
# make a check pass is how a pipeline starts lying.
# ─────────────────────────────────────────────────────────────────────────────────────
_DUCKDB_TYPES: dict[str, str] = {
    # customers
    "customer_id": "INTEGER", "first_name": "VARCHAR", "last_name": "VARCHAR",
    "email": "VARCHAR", "phone": "VARCHAR", "ssn_last4": "VARCHAR",
    "date_of_birth": "DATE", "address_line1": "VARCHAR", "city": "VARCHAR",
    "state_code": "VARCHAR", "postal_code": "VARCHAR", "employer_name": "VARCHAR",
    "signup_channel": "VARCHAR", "created_at": "TIMESTAMP", "updated_at": "TIMESTAMP",
    "is_deleted": "BOOLEAN",
    # advances
    "advance_id": "INTEGER", "external_advance_id": "VARCHAR", "status": "VARCHAR",
    "principal_amount": "DOUBLE", "fee_amount": "DOUBLE",
    "funded_at": "TIMESTAMP", "paid_off_at": "TIMESTAMP",
    "repayment_account_hash": "VARCHAR",
    # transactions
    "transaction_id": "BIGINT", "direction": "VARCHAR", "amount_cents": "BIGINT",
    "currency": "VARCHAR", "posted_at": "TIMESTAMP",
    # customer_history
    "history_id": "BIGINT", "changed_column": "VARCHAR", "old_value": "VARCHAR",
    "new_value": "VARCHAR", "changed_at": "TIMESTAMP", "changed_by": "VARCHAR",
    # cards
    "card_id": "INTEGER", "card_token": "VARCHAR", "card_fingerprint": "VARCHAR",
    "brand": "VARCHAR", "last4": "VARCHAR", "exp_month": "SMALLINT",
    "exp_year": "SMALLINT", "is_default": "BOOLEAN", "billing_postal": "VARCHAR",
}


def duckdb_type(column: str) -> str:
    if column not in _DUCKDB_TYPES:
        # Better to fail than to guess: an unmapped column means the manifest and this
        # module have drifted, and inferring VARCHAR would silently store a date as text.
        raise KeyError(
            f"No warehouse type mapped for column {column!r}. Add it to _DUCKDB_TYPES "
            f"rather than letting the loader guess."
        )
    return _DUCKDB_TYPES[column]


@dataclass
class TableRunStats:
    """What one table's load did. Every number in SOLUTION.md comes from one of these."""

    table: str
    strategy: str
    read_mode: str                  # snapshot | change_tracking | high_water
    rows_read: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_deleted: int = 0
    payload_bytes: int = 0
    wall_ms: int = 0
    watermark_before: int | None = None
    watermark_after: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rows_written(self) -> int:
        return self.rows_inserted + self.rows_updated + self.rows_deleted


def payload_bytes(rows: Sequence[dict[str, Any]]) -> int:
    """Approximate the bytes moved for these rows.

    LABELLED HONESTLY, because the distinction matters to the cost argument: this is the
    size of the VALUES transferred, not bytes on the wire. It excludes TDS protocol
    framing, column metadata and TLS overhead, so it is a floor rather than a measurement
    of network traffic. It is the right basis for a full-copy-versus-delta comparison --
    both sides are measured the same way -- and the wrong basis for a bandwidth bill.
    """
    total = 0
    for row in rows:
        for value in row.values():
            if value is None:
                continue
            if isinstance(value, (bytes, bytearray)):
                total += len(value)
            elif isinstance(value, bool):
                total += 1
            elif isinstance(value, int):
                total += 8
            elif isinstance(value, float):
                total += 8
            else:
                total += len(str(value).encode("utf-8"))
    return total


class Warehouse:
    """The BigQuery stand-in."""

    def __init__(self, config: WarehouseConfig) -> None:
        self._config = config
        self._conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> None:
        self._config.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._config.path))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            raise RuntimeError("Warehouse.connect() has not been called")
        return self._conn

    # ── schema ──────────────────────────────────────────────────────────────────────

    def create_schema(self, manifest: Manifest) -> None:
        """Create the raw mirror, the bookkeeping tables and the identity outputs."""
        c = self.connection
        c.execute("CREATE SCHEMA IF NOT EXISTS raw")
        c.execute("CREATE SCHEMA IF NOT EXISTS ops")
        c.execute("CREATE SCHEMA IF NOT EXISTS meta")

        for spec in manifest.tables:
            columns = ",\n    ".join(
                f'"{col}" {duckdb_type(col)}' for col in spec.columns
            )
            pk = ", ".join(f'"{k}"' for k in spec.primary_key)
            c.execute(
                f'CREATE TABLE IF NOT EXISTS raw."{spec.name}" (\n'
                f"    {columns},\n"
                f"    PRIMARY KEY ({pk})\n"
                f")"
            )

        # ops.load_state -- ONE ROW PER TABLE. The watermark, and the fingerprint of the
        # source incarnation that issued it.
        c.execute("""
            CREATE TABLE IF NOT EXISTS ops.load_state (
                table_name         VARCHAR PRIMARY KEY,
                strategy           VARCHAR NOT NULL,
                watermark          BIGINT,
                source_fingerprint VARCHAR,
                last_run_id        BIGINT,
                updated_at         TIMESTAMP
            )
        """)

        # ops.load_run -- ONE ROW PER TABLE PER RUN. This is where every number in the
        # write-up comes from. It is written as a side effect of loading, never
        # back-filled, which is the point: the figures cannot be retrofitted to suit a
        # narrative because they exist before the narrative does.
        c.execute("""
            CREATE TABLE IF NOT EXISTS ops.load_run (
                run_id           BIGINT NOT NULL,
                table_name       VARCHAR NOT NULL,
                strategy         VARCHAR NOT NULL,
                read_mode        VARCHAR NOT NULL,
                rows_read        BIGINT NOT NULL,
                rows_inserted    BIGINT NOT NULL,
                rows_updated     BIGINT NOT NULL,
                rows_deleted     BIGINT NOT NULL,
                payload_bytes    BIGINT NOT NULL,
                wall_ms          BIGINT NOT NULL,
                watermark_before BIGINT,
                watermark_after  BIGINT,
                notes            VARCHAR,
                started_at       TIMESTAMP NOT NULL,
                PRIMARY KEY (run_id, table_name)
            )
        """)

        # meta.customer_map -- ONE ROW PER SOURCE CUSTOMER, and the only place a merge
        # exists. The merge is INDIRECTION, never MUTATION: no source key and no foreign
        # key is ever rewritten, so withdrawing a merge costs a re-derivation instead of
        # a data repair. Without this layer, undoing a wrong merge means restoring the
        # rows it overwrote -- and nobody has a backup of a row that was overwritten
        # correctly-looking six weeks ago.
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta.customer_map (
                customer_id           INTEGER PRIMARY KEY,
                canonical_customer_id INTEGER NOT NULL,
                is_canonical          BOOLEAN NOT NULL,
                resolution            VARCHAR NOT NULL,  -- singleton|merged|review|excluded_test
                rule                  VARCHAR,           -- proof_tuple|manual_merge|NULL
                group_key             VARCHAR,           -- ssn|dob|surname, or NULL for singletons
                survivor_reason       VARCHAR,           -- money_moved|freshest|NULL
                evidence              VARCHAR,
                run_id                BIGINT NOT NULL,
                resolved_at           TIMESTAMP NOT NULL
            )
        """)

        # meta.merge_review -- the REFUSALS, one row per group the resolver would not
        # merge on its own. Its existence is the whole difference between refusing and
        # silently dropping: a group with two money-moved members leaves an auditable row
        # naming the customers, the reason and the funding instruments, rather than a log
        # line nobody reads. `review_id` is derived from the sorted customer-id tuple, so
        # a recurring refusal keeps its id across runs and a reviewer can tell it from a
        # new one.
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta.merge_review (
                review_id    BIGINT NOT NULL,
                run_id       BIGINT NOT NULL,
                group_key    VARCHAR,
                customer_ids VARCHAR NOT NULL,     -- comma-separated, ASCII
                reason       VARCHAR NOT NULL,
                evidence     VARCHAR NOT NULL,
                created_at   TIMESTAMP NOT NULL,
                PRIMARY KEY (review_id, run_id)
            )
        """)

        # meta.manual_merge -- the output path of the review queue, and the third PROVES
        # source. Starts EMPTY and is read on every run, so a human decision survives
        # every re-derivation. Not prose: a real table the resolver reads, so the tier is
        # exercised rather than described. A review queue with no write-back is a queue
        # whose decisions are lost the next time the map is rebuilt.
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta.manual_merge (
                customer_id_a INTEGER NOT NULL,
                customer_id_b INTEGER NOT NULL,
                decided_by    VARCHAR NOT NULL,
                decided_at    TIMESTAMP NOT NULL,
                note          VARCHAR,
                PRIMARY KEY (customer_id_a, customer_id_b)
            )
        """)

        # meta.identity_run -- ONE ROW PER RESOLUTION RUN, written as a side effect of
        # resolving, exactly like ops.load_run above: the figures exist before the
        # narrative does, so no number in the write-up can be retrofitted to suit it. It
        # is also the only place `canonical_with_bad_contact` is recorded -- the stated
        # cost of refusing field-level coalescing, which a summary counting only merges
        # would never surface.
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta.identity_run (
                run_id                     BIGINT PRIMARY KEY,
                customers_in               BIGINT NOT NULL,
                excluded_test              BIGINT NOT NULL,
                groups_formed              BIGINT NOT NULL,
                rows_merged                BIGINT NOT NULL,
                canonical_out              BIGINT NOT NULL,
                review_rows                BIGINT NOT NULL,
                manual_decisions_applied   BIGINT NOT NULL,
                canonical_with_bad_contact BIGINT NOT NULL,
                wall_ms                    BIGINT NOT NULL,
                resolved_at                TIMESTAMP NOT NULL
            )
        """)

    def next_run_id(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(run_id), 0) + 1 FROM ops.load_run"
        ).fetchone()
        return int(row[0]) if row else 1

    # ── the false-green guard ───────────────────────────────────────────────────────

    def assert_source_matches(self, fingerprint: SourceFingerprint) -> list[str]:
        """Refuse to use a watermark issued by a different source incarnation.

        THE FAILURE THIS PREVENTS. `docker compose down -v && docker compose up` rebuilds
        the source; its change-tracking version restarts at 0. If the warehouse still
        holds "caught up to version 47", then asking for changes since 47 returns NOTHING
        and no error: the min-valid-version check passes because 0 <= 47, the change feed
        is empty, and every downstream check goes green against a database that was
        rebuilt from scratch and shares none of its history.

        Re-running is the first thing any reviewer does, so this is not hypothetical.

        Resetting the watermarks costs one full reload. Not resetting them costs a
        confidently wrong warehouse, which is strictly worse -- so the guard resets and
        says so, loudly, rather than asking.
        """
        notes: list[str] = []
        rows = self.connection.execute(
            "SELECT table_name, source_fingerprint FROM ops.load_state "
            "WHERE source_fingerprint IS NOT NULL"
        ).fetchall()
        stale = [t for t, fp in rows if fp != fingerprint.token]
        if stale:
            notes.append(
                f"SOURCE IDENTITY CHANGED -- the source database is a different "
                f"incarnation from the one that issued the stored watermarks "
                f"({len(stale)} table(s) affected). The watermarks are meaningless "
                f"against it, so they have been reset and this run is a full reload. "
                f"Left alone, the change feed would have returned nothing and every "
                f"check would have passed against a rebuilt database."
            )
            self.connection.execute("DELETE FROM ops.load_state")
            for table in ("raw",):
                pass  # raw tables are re-populated by the reload; see load.py
        return notes

    def reset_all(self, manifest: Manifest) -> None:
        """Truncate the mirror and forget every watermark. Used on a full reload."""
        for spec in manifest.tables:
            self.connection.execute(f'DELETE FROM raw."{spec.name}"')
        self.connection.execute("DELETE FROM ops.load_state")

    # ── watermarks ──────────────────────────────────────────────────────────────────

    def watermark(self, table: str) -> int | None:
        row = self.connection.execute(
            "SELECT watermark FROM ops.load_state WHERE table_name = ?", [table]
        ).fetchone()
        return None if row is None else (None if row[0] is None else int(row[0]))

    # ── transactional apply ─────────────────────────────────────────────────────────

    @contextmanager
    def transaction(self) -> Iterator[None]:
        c = self.connection
        c.execute("BEGIN TRANSACTION")
        try:
            yield
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def apply_changes(
        self,
        spec: TableSpec,
        changes: Sequence[ChangeRow],
        *,
        watermark: int,
        fingerprint: SourceFingerprint,
        run_id: int,
    ) -> tuple[int, int, int]:
        """Upsert and delete a change-tracking batch, then advance the watermark.

        MERGE, not delete-then-insert. This is where the BigQuery cost argument lives, and
        it is the step that inverts the naive conclusion:

          "Loading into BigQuery is free" is true -- batch loads are not billed. But the
          UPSERT is a MERGE, and MERGE is billed as a QUERY. An unpruned MERGE scans the
          ENTIRE target table every single day, so a pipeline that congratulates itself on
          a free load has quietly swapped it for a billed full scan of the destination.
          On a 250,000-row table that is noise; on the real table this stands in for, it
          is the dominant line item and it grows with total history rather than with
          today's change.

          The fix is a STATIC partition filter in the MERGE predicate so BigQuery can
          prune, plus -- for genuinely append-only tables -- skipping MERGE entirely and
          appending, which is free. That is why transactions and customer_history do not
          come through this method at all.

        Returns (inserted, updated, deleted).
        """
        c = self.connection
        keys = list(spec.primary_key)
        upserts = [ch for ch in changes if ch.operation in ("I", "U") and ch.values]
        deletes = [ch for ch in changes if ch.operation == "D"]

        staging = f"stg_{spec.name}"
        columns = ",\n    ".join(f'"{col}" {duckdb_type(col)}' for col in spec.columns)
        c.execute(f"CREATE OR REPLACE TEMP TABLE {staging} (\n    {columns}\n)")

        if upserts:
            placeholders = ", ".join("?" for _ in spec.columns)
            c.executemany(
                f'INSERT INTO {staging} VALUES ({placeholders})',
                [[ch.values[col] for col in spec.columns] for ch in upserts],
            )

        inserted = updated = deleted = 0

        # Count first, so the reported numbers distinguish an INSERT from an UPDATE.
        # "rows written" alone cannot tell a new customer from a changed one, and the
        # difference is exactly what the change-rate argument depends on.
        if upserts:
            on = " AND ".join(f't."{k}" = s."{k}"' for k in keys)
            row = c.execute(
                f'SELECT COUNT(*) FROM {staging} s '
                f'WHERE EXISTS (SELECT 1 FROM raw."{spec.name}" t WHERE {on})'
            ).fetchone()
            updated = int(row[0]) if row else 0
            inserted = len(upserts) - updated

            set_clause = ", ".join(
                f'"{col}" = s."{col}"' for col in spec.columns if col not in keys
            )
            insert_cols = ", ".join(f'"{col}"' for col in spec.columns)
            insert_vals = ", ".join(f's."{col}"' for col in spec.columns)
            merge_on = " AND ".join(f't."{k}" = s."{k}"' for k in keys)
            # DuckDB 1.4 LTS added real MERGE INTO. This is the statement whose cost the
            # write-up argues about, so it is the statement that gets used.
            c.execute(
                f'MERGE INTO raw."{spec.name}" AS t '
                f"USING {staging} AS s ON {merge_on} "
                + (f"WHEN MATCHED THEN UPDATE SET {set_clause} " if set_clause else "")
                + f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
            )

        if deletes:
            # Deletes carry ONLY the primary key -- the row is gone from the source, so
            # there is nothing else to select. This is the half of the problem that no
            # watermark strategy can express at all, and the reason these tables use
            # change tracking.
            key_pred = " AND ".join(f'"{k}" = ?' for k in keys)
            before = c.execute(f'SELECT COUNT(*) FROM raw."{spec.name}"').fetchone()[0]
            c.executemany(
                f'DELETE FROM raw."{spec.name}" WHERE {key_pred}',
                [list(ch.key) for ch in deletes],
            )
            after = c.execute(f'SELECT COUNT(*) FROM raw."{spec.name}"').fetchone()[0]
            deleted = int(before) - int(after)

        self._write_watermark(spec.name, spec.strategy, watermark, fingerprint, run_id)
        return inserted, updated, deleted

    def append_rows(
        self,
        spec: TableSpec,
        rows: Sequence[dict[str, Any]],
        *,
        watermark: int,
        fingerprint: SourceFingerprint,
        run_id: int,
    ) -> int:
        """Append an append-only batch, then advance the watermark.

        A plain INSERT, and that is the cost argument's other half: in BigQuery a batch
        load is FREE, while the MERGE used for the mutable tables is billed as a query
        over the whole target. So the correct strategy per table is not a stylistic
        preference -- it changes the bill. Append-only tables should never see a MERGE.
        """
        if not rows:
            self._write_watermark(spec.name, spec.strategy, watermark, fingerprint, run_id)
            return 0
        placeholders = ", ".join("?" for _ in spec.columns)
        self.connection.executemany(
            f'INSERT INTO raw."{spec.name}" VALUES ({placeholders})',
            [[r[col] for col in spec.columns] for r in rows],
        )
        self._write_watermark(spec.name, spec.strategy, watermark, fingerprint, run_id)
        return len(rows)

    def _write_watermark(
        self,
        table: str,
        strategy: str,
        watermark: int,
        fingerprint: SourceFingerprint,
        run_id: int,
    ) -> None:
        """Record the watermark. Called INSIDE the same transaction as the row changes.

        Never call this separately. See the module docstring: the atomicity of
        rows-and-watermark is what makes a crash recoverable instead of silent.
        """
        self.connection.execute(
            """
            INSERT INTO ops.load_state
                (table_name, strategy, watermark, source_fingerprint, last_run_id, updated_at)
            VALUES (?, ?, ?, ?, ?, now())
            ON CONFLICT (table_name) DO UPDATE SET
                strategy           = excluded.strategy,
                watermark          = excluded.watermark,
                source_fingerprint = excluded.source_fingerprint,
                last_run_id        = excluded.last_run_id,
                updated_at         = excluded.updated_at
            """,
            [table, strategy, watermark, fingerprint.token, run_id],
        )

    # ── run bookkeeping ─────────────────────────────────────────────────────────────

    def record_run(self, run_id: int, stats: TableRunStats, started_at: float) -> None:
        self.connection.execute(
            """
            INSERT INTO ops.load_run
                (run_id, table_name, strategy, read_mode, rows_read, rows_inserted,
                 rows_updated, rows_deleted, payload_bytes, wall_ms, watermark_before,
                 watermark_after, notes, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, to_timestamp(?))
            """,
            [run_id, stats.table, stats.strategy, stats.read_mode, stats.rows_read,
             stats.rows_inserted, stats.rows_updated, stats.rows_deleted,
             stats.payload_bytes, stats.wall_ms, stats.watermark_before,
             stats.watermark_after, "; ".join(stats.notes) or None, started_at],
        )

    def row_count(self, table: str) -> int:
        row = self.connection.execute(f'SELECT COUNT(*) FROM raw."{table}"').fetchone()
        return int(row[0]) if row else 0
