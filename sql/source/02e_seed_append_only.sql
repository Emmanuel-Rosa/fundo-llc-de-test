-- ═══════════════════════════════════════════════════════════════════════════════════
-- SEED: the append-only tables, plus the abandoned scratch table.
--
--   dbo.transactions            250,000 rows -- 86.8% of everything replicated
--   dbo.customer_history         18,000 rows -- the fastest-growing table in the source
--   dbo.tmp_dedupe_backup_2019    1,200 rows -- dead since 2019, replicated by nobody
--
-- These two append-only tables are why the pipeline uses more than one capture strategy.
-- They hold the overwhelming majority of the rows and almost none of the daily change,
-- so paying change-tracking overhead on them would be paying to be told what a clustered
-- index seek already answers for free.
--
-- THE TRAP THIS FILE EXISTS TO SET: posted_at is NOT monotonic with transaction_id.
-- posted_at is the obvious watermark -- it is a timestamp, it is indexed, and it reads
-- naturally. It is also wrong, because about 5% of each day's rows are BACKDATED
-- (a settlement file lands late, an adjustment is posted for an earlier date). A
-- posted_at watermark silently skips every one of them, permanently. The key cannot be
-- backdated, which is why the key is the watermark.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

DELETE FROM dbo.transactions;
DELETE FROM dbo.customer_history;
DELETE FROM dbo.tmp_dedupe_backup_2019;
GO

-- A numbers table big enough for 250,000 rows. sys.all_objects has a few thousand rows,
-- so a self cross join gives millions -- more than enough, and it needs no permissions
-- beyond reading the catalogue.
DROP TABLE IF EXISTS #big;
GO
CREATE TABLE #big (n INT PRIMARY KEY);
INSERT INTO #big (n)
SELECT TOP (250000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- PRIYA'S SIX REAL REPAYMENTS (customer 88, advance 91).
--
-- Seeded first, at low transaction_ids, and they exist to give the test-account false
-- positive a PRICE. Priya is a genuine staff member on the internal @fundo.com domain
-- with a real paid-off advance. `email LIKE '%@fundo.com'` removes her -- and with her,
-- these six transactions leave the book with no error and no trace: a $250.00
-- disbursement, a $12.50 fee, and FOUR repayments totalling $262.50.
-- "Precision 0.500 on the naive filter" is an abstraction; $262.50 of vanished
-- repayments is what it actually means.
-- ─────────────────────────────────────────────────────────────────────────────────────
SET IDENTITY_INSERT dbo.transactions ON;
GO

-- THE IDS HERE MUST MATCH THE DATES, and the first version got that wrong in a way that
-- broke a different exhibit entirely. These six rows were originally given ids 1-6 while
-- carrying April 2026 timestamps -- but the bulk rows start in August 2024, so the
-- running maximum of posted_at jumped to April 2026 at row 6 and every one of the next
-- 212,000 rows then read as "out of order". The posted_at monotonicity measurement
-- reported 223,876 of 250,000 rows out of sequence instead of the ~5% that are genuinely
-- backdated, which would have made the watermark argument look manufactured.
--
-- Ids 212401-212406 place these rows where April 2026 actually falls in the id sequence.
INSERT INTO dbo.transactions
 (transaction_id, customer_id, advance_id, direction, amount_cents, currency,
  posted_at, created_at)
VALUES
 (212401, 88, 91, 'disbursement', 25000, 'USD', '2026-04-01T09:05:00', '2026-04-01T09:05:00'),
 (212402, 88, 91, 'fee',           1250, 'USD', '2026-04-01T09:05:00', '2026-04-01T09:05:00'),
 (212403, 88, 91, 'repayment',    -6500, 'USD', '2026-04-08T09:00:00', '2026-04-08T09:00:00'),
 (212404, 88, 91, 'repayment',    -6500, 'USD', '2026-04-15T09:00:00', '2026-04-15T09:00:00'),
 (212405, 88, 91, 'repayment',    -6500, 'USD', '2026-04-22T09:00:00', '2026-04-22T09:00:00'),
 (212406, 88, 91, 'repayment',    -6750, 'USD', '2026-04-29T09:00:00', '2026-04-29T09:00:00');
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- BULK TRANSACTIONS -- to 250,000.
--
-- amount_cents is a BIGINT, and it is the ONE money column in the whole schema that
-- reconciles EXACTLY. Contrast advances.principal_amount, which is a FLOAT and therefore
-- cannot be summed reproducibly -- the value-parity check omits that column and prints
-- the reason rather than reconciling it with a tolerance and calling that a pass.
-- Having both in the same schema is what makes the point concrete.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #txn_cust;
GO
CREATE TABLE #txn_cust (seq INT PRIMARY KEY, customer_id INT);
INSERT INTO #txn_cust (seq, customer_id)
SELECT ROW_NUMBER() OVER (ORDER BY customer_id), customer_id
FROM dbo.customers
-- Customer 88 (Priya) is EXCLUDED from bulk transactions. Her exhibit rests on an exact
-- figure -- "excluding her as test data removes $262.50 of genuine repayments from the
-- book" -- and that claim is only checkable if her six hand-built rows are all she has.
-- The first version let the bulk pass assign her another ~20 transactions, so her net
-- came out at $1,215.53 and the exhibit no longer said anything specific.
--
-- Every other duplicate-group member DOES receive bulk transactions, deliberately: the
-- merge has to demonstrate that resolving identity loses no transaction history, and it
-- can only demonstrate that if there is history to lose.
WHERE customer_id <> 88;
GO

DECLARE @ncust INT = (SELECT COUNT(*) FROM #txn_cust);

WITH t AS (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('txc:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('txa:', CAST(n AS VARCHAR(12)))))) AS ha,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('txd:', CAST(n AS VARCHAR(12)))))) AS hd,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('txb:', CAST(n AS VARCHAR(12)))))) AS hb
    FROM #big
    WHERE n NOT BETWEEN 212401 AND 212406   -- reserved for Priya's hand-built repayments
)
INSERT INTO dbo.transactions
 (transaction_id, customer_id, advance_id, direction, amount_cents, currency,
  posted_at, created_at)
SELECT
    t.n,
    (SELECT customer_id FROM #txn_cust WHERE seq = 1 + (t.hc % @ncust)),
    NULL,                              -- most rows carry no advance link (legacy, no FK)
    CASE t.hd % 5 WHEN 0 THEN 'disbursement' WHEN 1 THEN 'repayment'
                  WHEN 2 THEN 'fee' WHEN 3 THEN 'refund' ELSE 'adjustment' END,
    -- Repayments and refunds are negative; disbursements and fees positive. Signed money
    -- means the parity check has to sum correctly rather than sum absolute values.
    CASE WHEN t.hd % 5 IN (1, 3) THEN -(500 + (t.ha % 49500))
         ELSE (500 + (t.ha % 49500)) END,
    'USD',
    -- ═══ THE NON-MONOTONIC TIMESTAMP ═══
    -- The baseline walks FORWARD with the id -- 4 minutes per row across ~2 years -- so
    -- posted_at normally ASCENDS with transaction_id and looks like a perfectly good
    -- watermark. Then ~5% of rows are pushed 1-90 days into the past: a settlement file
    -- lands late, or an adjustment is posted against an earlier date. Those rows carry a
    -- HIGH transaction_id and an OLD posted_at.
    --
    -- The direction here is the whole exhibit and the first version got it backwards: the
    -- baseline ran BACKWARDS from today, so the entire table descended and 249,992 of
    -- 250,000 rows were "out of order". A trap that fires on every row is not a trap, it
    -- is a broken fixture -- and it would have made the posted_at argument unfalsifiable
    -- rather than demonstrated.
    --
    -- Consequence, measured by the run: a watermark on posted_at skips the backdated rows
    -- permanently, because by the time they are written the watermark has already moved
    -- past their timestamp. A watermark on transaction_id cannot miss them, because an
    -- IDENTITY value is never issued out of order.
    CASE WHEN t.hb % 100 < 5
         THEN DATEADD(DAY, -(1 + (t.hb % 90)),
                DATEADD(MINUTE, t.n * 4, CAST('2024-08-19T09:00:00' AS DATETIME2(3))))
         ELSE DATEADD(MINUTE, t.n * 4, CAST('2024-08-19T09:00:00' AS DATETIME2(3)))
    END,
    -- created_at is when the row was WRITTEN, and it stays monotonic with the id. The gap
    -- between created_at and posted_at IS the backdating, and it is visible.
    DATEADD(MINUTE, t.n * 4, CAST('2024-08-19T09:00:00' AS DATETIME2(3)))
FROM t;
GO

SET IDENTITY_INSERT dbo.transactions OFF;
GO

SELECT 'transactions_seeded' AS stage,
       COUNT(*) AS total,
       SUM(amount_cents) AS sum_amount_cents,
       COUNT(DISTINCT customer_id) AS distinct_customers,
       SUM(CASE WHEN posted_at < created_at THEN 1 ELSE 0 END) AS backdated_rows,
       CAST(100.0 * SUM(CASE WHEN posted_at < created_at THEN 1 ELSE 0 END)
            / COUNT(*) AS DECIMAL(5,2)) AS backdated_pct
FROM dbo.transactions;
GO

-- PROOF that posted_at is unusable as a watermark, stated as a number rather than an
-- argument: rows whose posted_at is EARLIER than that of a row with a LOWER id. Each one
-- is a row a posted_at watermark would skip.
SELECT 'posted_at_monotonicity' AS stage,
       SUM(CASE WHEN posted_at < prev_max THEN 1 ELSE 0 END) AS rows_out_of_order,
       COUNT(*) AS rows_checked
FROM (
    SELECT posted_at,
           MAX(posted_at) OVER (ORDER BY transaction_id
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max
    FROM dbo.transactions
) x;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- customer_history -- 18,000 rows, append-only, and it keeps PII copies forever.
--
-- old_value and new_value retain former emails and former ssn_last4 values indefinitely.
-- That is the second reason retention here is a COMPLIANCE decision rather than an
-- engineering one: this table is a standing record of data customers may have asked to
-- have corrected, and 6.67%/day growth means the problem compounds.
--
-- 3 rows deliberately reference customer_ids that no longer exist (purged in 2019, before
-- the current customers table was populated). There is no foreign key, so the source
-- permits them. They are reported as SOURCE-DIRTY by the scorecard -- never as a load
-- failure, because refusing to load a row the source legitimately contains just moves the
-- problem into the pipeline.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #hcols;
GO
CREATE TABLE #hcols (i INT IDENTITY(0,1) PRIMARY KEY, col VARCHAR(64), pii BIT);
INSERT INTO #hcols (col, pii) VALUES
 ('email', 1), ('phone', 1), ('ssn_last4', 1), ('address_line1', 1),
 ('employer_name', 0), ('city', 0), ('postal_code', 0), ('last_name', 1);
GO

SET IDENTITY_INSERT dbo.customer_history ON;
GO

-- The three orphans first, at low ids, so they are easy to point at.
INSERT INTO dbo.customer_history
 (history_id, customer_id, changed_column, old_value, new_value, changed_at, changed_by)
VALUES
 (1, 90001, 'email',     N'purged.user1@oldmail.com', N'purged.user1@newmail.com',
  '2019-03-14T11:00:00', 'ops_batch'),
 (2, 90002, 'ssn_last4', N'4417', N'4418', '2019-05-02T14:30:00', 'dba_manual'),
 (3, 90003, 'phone',     N'+15125550001', N'+15125550002', '2019-07-19T09:15:00', 'app');
GO

DECLARE @nc INT = (SELECT COUNT(*) FROM #txn_cust);

WITH h AS (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('hc:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('hk:', CAST(n AS VARCHAR(12)))))) AS hk,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('hv:', CAST(n AS VARCHAR(12)))))) AS hv
    FROM #big
    WHERE n > 3 AND n <= 18000
)
INSERT INTO dbo.customer_history
 (history_id, customer_id, changed_column, old_value, new_value, changed_at, changed_by)
SELECT
    h.n,
    (SELECT customer_id FROM #txn_cust WHERE seq = 1 + (h.hc % @nc)),
    (SELECT col FROM #hcols WHERE i = h.hk % 8),
    CONCAT(N'old-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('o:', CAST(h.n AS VARCHAR(12)))), 2))),
    CONCAT(N'new-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('n:', CAST(h.n AS VARCHAR(12)))), 2))),
    DATEADD(MINUTE, -(h.n * 4), CAST('2026-08-18T09:00:00' AS DATETIME2(3))),
    CASE h.hv % 10 WHEN 0 THEN 'dba_manual' WHEN 1 THEN 'ops_batch' ELSE 'app' END
FROM h;
GO

SET IDENTITY_INSERT dbo.customer_history OFF;
GO

SELECT 'customer_history_seeded' AS stage,
       COUNT(*) AS total,
       SUM(CASE WHEN c.customer_id IS NULL THEN 1 ELSE 0 END) AS orphan_rows,
       SUM(CASE WHEN hc.pii = 1 THEN 1 ELSE 0 END) AS rows_holding_pii_history
FROM dbo.customer_history h
LEFT JOIN dbo.customers c ON c.customer_id = h.customer_id
LEFT JOIN #hcols hc ON hc.col = h.changed_column;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- tmp_dedupe_backup_2019 -- the abandoned scratch table. NOT replicated.
--
-- The point is the recorded DECISION, not the 1,200 rows of daily traffic it saves. Its
-- deadness is evidenced, not inferred from the tmp_ prefix:
--   * no primary key, no index, no foreign key referencing it, no constraints
--   * 4 of its 8 columns are 100% NULL -- someone built a review workflow and abandoned it
--   * reviewed_at is a DATE STORED AS TEXT, and it is entirely NULL
--   * the newest note dates to 2019-11-08
--   * zero rows change across either churn window
--   * zero rows in sys.sql_modules mention the table name
--
-- On a real system I would also check sys.dm_db_index_usage_stats for a last read, search
-- Query Store for the name, and then the honest one: rename it and wait a month. A prefix
-- is not evidence, and dropping on a prefix is how month-end reporting breaks.
-- ─────────────────────────────────────────────────────────────────────────────────────
WITH d AS (
    SELECT n, ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('dd:', CAST(n AS VARCHAR(12)))))) AS h
    FROM #big WHERE n <= 1200
)
INSERT INTO dbo.tmp_dedupe_backup_2019
 (cust_id, email, note, dupe_of, reviewed_by, reviewed_at, batch_id, score)
SELECT
    1 + (d.h % 5000),
    CONCAT('legacy', CAST(d.n AS VARCHAR(6)), '@oldmail.com'),
    CONCAT('candidate pair flagged by 2019 dedupe sweep; batch ', d.h % 40),
    1 + ((d.h / 7) % 5000),
    NULL,   -- 100% NULL: nobody ever reviewed one
    NULL,   -- 100% NULL, and a date typed as VARCHAR(50)
    NULL,   -- 100% NULL
    NULL    -- 100% NULL
FROM d;
GO

SELECT 'scratch_table_seeded' AS stage,
       COUNT(*) AS total_rows,
       SUM(CASE WHEN reviewed_by IS NULL THEN 1 ELSE 0 END) AS reviewed_by_null,
       SUM(CASE WHEN reviewed_at IS NULL THEN 1 ELSE 0 END) AS reviewed_at_null,
       SUM(CASE WHEN batch_id    IS NULL THEN 1 ELSE 0 END) AS batch_id_null,
       SUM(CASE WHEN score       IS NULL THEN 1 ELSE 0 END) AS score_null
FROM dbo.tmp_dedupe_backup_2019;
GO

-- The deadness evidence set, gathered from the catalogue rather than asserted.
SELECT 'scratch_deadness_evidence' AS stage,
       (SELECT COUNT(*) FROM sys.indexes
         WHERE object_id = OBJECT_ID('dbo.tmp_dedupe_backup_2019')
           AND index_id > 0)                                        AS indexes_count,
       (SELECT COUNT(*) FROM sys.key_constraints
         WHERE parent_object_id = OBJECT_ID('dbo.tmp_dedupe_backup_2019')) AS key_constraints,
       (SELECT COUNT(*) FROM sys.foreign_keys
         WHERE referenced_object_id = OBJECT_ID('dbo.tmp_dedupe_backup_2019')) AS inbound_fks,
       (SELECT COUNT(*) FROM sys.sql_modules
         WHERE definition LIKE '%tmp_dedupe_backup_2019%')          AS referencing_modules;
GO
