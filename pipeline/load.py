"""The load orchestration -- one run across all five replicated tables.

ONE SNAPSHOT FOR THE WHOLE RUN, not one per table. Every table's version stamp, ceiling
and rows come from a single consistent view of the source. That buys something a
per-table loop cannot: CROSS-TABLE REFERENTIAL CONSISTENCY. Read customers and advances
in separate transactions and an advance can arrive for a customer the customer read never
saw, so the warehouse holds a foreign key to nothing -- intermittently, and only under
concurrent writes, which is the hardest kind of bug to reproduce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Settings, TableSpec
from .source import Source
from .warehouse import TableRunStats, Warehouse, payload_bytes


@dataclass
class RunSummary:
    run_id: int
    tables: list[TableRunStats] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    full_reload: bool = False

    @property
    def rows_read(self) -> int:
        return sum(t.rows_read for t in self.tables)

    @property
    def rows_written(self) -> int:
        return sum(t.rows_written for t in self.tables)

    @property
    def payload_bytes(self) -> int:
        return sum(t.payload_bytes for t in self.tables)

    @property
    def wall_ms(self) -> int:
        return sum(t.wall_ms for t in self.tables)


def run_load(settings: Settings, source: Source, warehouse: Warehouse) -> RunSummary:
    """Execute one incremental load across every table in the manifest."""
    manifest = settings.manifest
    run_id = warehouse.next_run_id()
    summary = RunSummary(run_id=run_id)

    fingerprint = source.source_fingerprint()

    # ── GUARD 1: is the source the same database that issued our watermarks? ─────────
    # Checked BEFORE any read. See Warehouse.assert_source_matches for the silent
    # false-green this prevents.
    identity_notes = warehouse.assert_source_matches(fingerprint)
    if identity_notes:
        summary.notes.extend(identity_notes)
        summary.full_reload = True
        warehouse.reset_all(manifest)

    # ── ONE SNAPSHOT, every table ───────────────────────────────────────────────────
    with source.snapshot() as snap:
        ct_version = snap.change_tracking_version

        # ── GUARD 2: has the change feed aged past our watermark? ───────────────────
        # If cleanup has already discarded the rows that would say what changed, the feed
        # is not partially correct -- it is unusable, and SQL Server does not raise. The
        # only honest response is a full reload, so it is detected before anything is
        # read rather than discovered as divergence weeks later.
        for spec in manifest.by_strategy("change_tracking"):
            stored = warehouse.watermark(spec.name)
            if stored is None:
                continue
            min_valid = snap.min_valid_version(spec)
            if stored < min_valid:
                note = (
                    f"{spec.name}: stored watermark {stored} is older than the source's "
                    f"minimum valid version {min_valid} -- the change feed can no longer "
                    f"answer for that range, so this table is fully reloaded rather than "
                    f"partially updated. (Retention is 2 days by design so this branch "
                    f"is reachable in a test instead of only in production at 3am.)"
                )
                summary.notes.append(note)
                warehouse.connection.execute(
                    "DELETE FROM ops.load_state WHERE table_name = ?", [spec.name]
                )
                warehouse.connection.execute(f'DELETE FROM raw."{spec.name}"')

        payloads: dict[str, tuple[TableSpec, object, TableRunStats, int]] = {}

        # ── READ everything from the one snapshot ───────────────────────────────────
        for spec in manifest.tables:
            t0 = time.monotonic()
            stored = warehouse.watermark(spec.name)

            if spec.strategy == "change_tracking":
                if stored is None:
                    # Run 1 is a SNAPSHOT read, not a change-feed replay. Capture is
                    # enabled after the seed, so asking CHANGETABLE for changes since
                    # version 0 on a freshly enabled table correctly returns nothing --
                    # verified against the live server, not inferred from the docs.
                    rows = snap.read_full_snapshot(spec)
                    stats = TableRunStats(
                        table=spec.name, strategy=spec.strategy, read_mode="snapshot",
                        rows_read=len(rows), payload_bytes=payload_bytes(rows),
                        watermark_before=None, watermark_after=ct_version,
                    )
                    payloads[spec.name] = (spec, rows, stats, ct_version)
                else:
                    changes = snap.read_changes(spec, stored)
                    stats = TableRunStats(
                        table=spec.name, strategy=spec.strategy,
                        read_mode="change_tracking", rows_read=len(changes),
                        payload_bytes=payload_bytes(
                            [c.values for c in changes if c.values]
                        ),
                        watermark_before=stored, watermark_after=ct_version,
                    )
                    payloads[spec.name] = (spec, changes, stats, ct_version)

            else:  # high_water
                since = stored if stored is not None else 0
                ceiling = snap.high_water_ceiling(spec)
                rows = snap.read_high_water(spec, since, ceiling)
                new_watermark = ceiling if ceiling is not None else since
                stats = TableRunStats(
                    table=spec.name, strategy=spec.strategy, read_mode="high_water",
                    rows_read=len(rows), payload_bytes=payload_bytes(rows),
                    watermark_before=stored, watermark_after=new_watermark,
                    notes=[f"closed window ({since}, {ceiling}]"],
                )
                payloads[spec.name] = (spec, rows, stats, new_watermark)

            stats.wall_ms = int((time.monotonic() - t0) * 1000)

    # ── APPLY, in ONE warehouse transaction ─────────────────────────────────────────
    # Rows and watermarks commit together, for every table at once. A crash between two
    # tables therefore rolls back to a coherent point rather than leaving customers at
    # version 51 and advances at version 47 -- a state that is not wrong in any way the
    # checks can see, and quietly wrong in the marts.
    started_at = time.time()
    with warehouse.transaction():
        for spec in manifest.tables:
            spec, data, stats, new_watermark = payloads[spec.name]
            t0 = time.monotonic()

            if spec.strategy == "change_tracking":
                if stats.read_mode == "snapshot":
                    from .source import ChangeRow
                    changes = [
                        ChangeRow(operation="I",
                                  key=tuple(r[k] for k in spec.primary_key), values=r)
                        for r in data
                    ]
                else:
                    changes = data
                ins, upd, dele = warehouse.apply_changes(
                    spec, changes, watermark=new_watermark,
                    fingerprint=fingerprint, run_id=run_id,
                )
                stats.rows_inserted, stats.rows_updated, stats.rows_deleted = ins, upd, dele
            else:
                stats.rows_inserted = warehouse.append_rows(
                    spec, data, watermark=new_watermark,
                    fingerprint=fingerprint, run_id=run_id,
                )

            stats.wall_ms += int((time.monotonic() - t0) * 1000)
            summary.tables.append(stats)

        for stats in summary.tables:
            warehouse.record_run(run_id, stats, started_at)

    return summary


def format_run(summary: RunSummary) -> str:
    """Render one run as a table a non-engineer can read.

    ASCII only, deliberately. Box-drawing characters crash a default Windows console
    (cp1252), and the reviewer piping `docker compose logs` through one is a real path.
    """
    lines: list[str] = []
    lines.append(f"  RUN {summary.run_id}"
                 + ("   [FULL RELOAD]" if summary.full_reload else ""))
    lines.append("  " + "-" * 96)
    lines.append(f"  {'table':<18} {'strategy':<16} {'mode':<15} "
                 f"{'read':>7} {'ins':>7} {'upd':>6} {'del':>5} {'KiB':>8} {'ms':>6}")
    lines.append("  " + "-" * 96)
    for t in summary.tables:
        lines.append(
            f"  {t.table:<18} {t.strategy:<16} {t.read_mode:<15} "
            f"{t.rows_read:>7,} {t.rows_inserted:>7,} {t.rows_updated:>6,} "
            f"{t.rows_deleted:>5,} {t.payload_bytes/1024:>8.1f} {t.wall_ms:>6,}"
        )
    lines.append("  " + "-" * 96)
    lines.append(
        f"  {'TOTAL':<18} {'':<16} {'':<15} "
        f"{summary.rows_read:>7,} "
        f"{sum(t.rows_inserted for t in summary.tables):>7,} "
        f"{sum(t.rows_updated for t in summary.tables):>6,} "
        f"{sum(t.rows_deleted for t in summary.tables):>5,} "
        f"{summary.payload_bytes/1024:>8.1f} {summary.wall_ms:>6,}"
    )
    for note in summary.notes:
        lines.append(f"\n  NOTE: {note}")
    return "\n".join(lines)
