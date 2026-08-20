-- ═══════════════════════════════════════════════════════════════════════════════════
-- Change capture -- enabled AFTER the 288,000-row seed, deliberately.
--
-- Order matters and this is the part people get wrong. If capture is on during the
-- seed you pay to enumerate every seeded insert into the change-tracking side tables,
-- and the initial load then has two valid but DIFFERENT ways to read the same rows
-- (replay the change feed, or snapshot the base table) with no principled way to
-- choose. Capture-after-snapshot removes the ambiguity: run 1 is unambiguously a
-- snapshot read, run 2+ are unambiguously change-feed reads, and ops.load_run records
-- which mode each run used.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

-- CHANGE_RETENTION is the window inside which a consumer may ask "what changed since
-- version N?" and get a trustworthy answer. Ask about a version older than the window
-- and SQL Server does not quietly return partial data -- CHANGE_TRACKING_MIN_VALID_VERSION
-- rises above your watermark and the correct response is a full reseed. The loader
-- checks that BEFORE reading, and refuses rather than under-reporting.
--
-- 2 days is short on purpose: it is long enough for this demo's three churn windows and
-- short enough that the "your watermark has expired" branch is reachable in a test
-- rather than being unexecuted code that only fires in production at 3am.
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_databases
               WHERE database_id = DB_ID('fundo_src'))
    ALTER DATABASE fundo_src
      SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- Change Tracking on the three mutable tables.
--
-- TRACK_COLUMNS_UPDATED is deliberately OFF. It is tempting -- it tells you WHICH
-- columns changed via SYS_CHANGE_COLUMNS -- but this pipeline keeps current state, not
-- an audit trail, so it would upsert the whole row regardless. Enabling it costs real
-- storage in the side tables to populate a bitmask nothing reads, and a reviewer is
-- entitled to ask what consumes it. Nothing would.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables
               WHERE object_id = OBJECT_ID('dbo.customers'))
    ALTER TABLE dbo.customers ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF);
GO

IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables
               WHERE object_id = OBJECT_ID('dbo.advances'))
    ALTER TABLE dbo.advances ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF);
GO

-- cards gets Change Tracking too, not a ROWVERSION column. See the note in
-- 01_schema.sql: one mechanism covering all three mutable tables beats two mechanisms
-- covering the same ground, and the rowversion argument is preserved as prose.
IF NOT EXISTS (SELECT 1 FROM sys.change_tracking_tables
               WHERE object_id = OBJECT_ID('dbo.cards'))
    ALTER TABLE dbo.cards ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- NOT enabled, and each omission is a decision:
--
--   dbo.transactions       -- the clustered IDENTITY key IS the watermark. Enabling CT
--                             here writes a side-car row per insert on the source's
--                             hottest write path, to tell the loader what a clustered
--                             index seek already tells it for free. 250,000 rows and
--                             2,800 inserts/day: this is where the cost would be.
--   dbo.customer_history   -- same, and it is append-only by construction (no update
--                             path, no delete path, no updated_at column).
--   dbo.tmp_dedupe_backup_2019 -- not replicated at all. Absent from tables.yml, which
--                             is default-deny, so this is belt and braces.
--
-- WHY NOT CDC, since the brief invites comparing mechanisms:
--   CDC gives before-images and a full DML history, which Change Tracking does not.
--   It is declined for two concrete reasons, not preference.
--   (1) It requires SQL Server Agent. Agent is OFF by default in the Linux container,
--       and -- the trap -- enabling CDC without it SUCCEEDS. The capture tables get
--       created, sys.databases.is_cdc_enabled flips to 1, and the change feed is then
--       empty forever. It presents as a pipeline bug, not a configuration error.
--   (2) A stalled CDC capture job holds the log. That is not a warehouse problem, it
--       is a SOURCE problem: the production transaction log cannot truncate, and the
--       failure mode of my nightly extract becomes an outage on Fundo's lending system.
--       Change Tracking's cleanup is synchronous with the DML and cannot stall this way.
--   CDC also needs Standard edition or better, which is a licensing input to a
--   technical decision and worth naming as such.
-- ─────────────────────────────────────────────────────────────────────────────────────

-- Emit the enabled set so the run transcript proves what was configured rather than
-- the reader taking this file's word for it.
SELECT
    OBJECT_SCHEMA_NAME(ctt.object_id) + '.' + OBJECT_NAME(ctt.object_id) AS table_name,
    ctt.is_track_columns_updated_on                                     AS track_columns_updated,
    CHANGE_TRACKING_CURRENT_VERSION()                                   AS current_version,
    CHANGE_TRACKING_MIN_VALID_VERSION(ctt.object_id)                    AS min_valid_version
FROM sys.change_tracking_tables AS ctt
ORDER BY table_name;
GO
