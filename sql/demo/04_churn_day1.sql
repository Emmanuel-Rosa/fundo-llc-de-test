-- ═══════════════════════════════════════════════════════════════════════════════════
-- CHURN DAY 1 -- one simulated day of source activity.
--
-- THIS FILE LIVES IN sql/demo/, NOT IN THE SEED CHAIN, and that placement is
-- load-bearing. Run it during the build and the FIRST load already sees the churned
-- state, so there is no delta to demonstrate at all -- the incremental run reads zero
-- rows and the whole point of the exercise evaporates. Seed, load, THEN churn.
--
-- The shape of the day is what matters. About 1.5% of rows change, and that change is
-- distributed extremely unevenly:
--
--   transactions       +2,800 inserts   -- 1.1% of its own rows, but the bulk of the day
--   customer_history   +1,200 inserts   -- 6.7% of its own rows: fastest-growing table
--   customers              99 changes   -- 2.0% of its rows, INCLUDING 13 HARD DELETES
--   cards                  30 changes
--   advances               50 changes
--
-- The mutable tables contribute a tiny number of rows and all of the difficulty; the
-- append-only tables contribute almost all the rows and none of it. That asymmetry is the
-- entire argument for choosing a strategy per table instead of applying one uniformly.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 1. THE 13 HARD DELETES.
--
-- The application deletes rows outright. It does NOT set customers.is_deleted -- that
-- column exists and no row has ever been set to 1. Anyone who trusts it builds a pipeline
-- that never sees a delete and never learns that it doesn't.
--
-- Deletes are also the half of change capture that no watermark strategy can express: a
-- deleted row leaves nothing behind to carry a timestamp or a version. Only the change
-- feed still knows it existed.
--
-- Chosen from ordinary singletons, avoiding every exhibit, so a deletion never quietly
-- removes a fixture the identity section depends on.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #to_delete;
GO
CREATE TABLE #to_delete (customer_id INT PRIMARY KEY);
INSERT INTO #to_delete (customer_id)
SELECT TOP (13) c.customer_id
FROM dbo.customers c
WHERE c._seed_person_id LIKE 'P-SINGLE-%'
  AND NOT EXISTS (SELECT 1 FROM dbo.advances a WHERE a.customer_id = c.customer_id)
ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
         CONCAT('del1:', CAST(c.customer_id AS VARCHAR(12))))));
GO

-- Cards are NOT deleted with the customer, because dbo.cards has no foreign key. That
-- asymmetry is deliberate and it is WHY orphan cards can exist: a customer can be
-- hard-deleted out from under their payment instruments. The scorecard reports the
-- resulting orphans as source-dirty, never as a load failure.
DELETE FROM dbo.customers WHERE customer_id IN (SELECT customer_id FROM #to_delete);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 2. C5002 ARRIVES -- a fourth member of the Marisol Duarte group.
--
-- Same proof tuple as the existing three (ssn 6640, dob 1985-02-25, surname Duarte), so
-- the resolver must alias it ON INGEST rather than letting it reach a mart as a separate
-- customer. A resolver that runs once at initial load leaves this duplicate standing
-- forever, which is the failure mode this row exists to catch.
--
-- On churn day 2 it gains a FUNDED advance, at which point the group holds two
-- money-moved members and the merge has to be WITHDRAWN. Because merging is indirection
-- and never mutation, that withdrawal is a map recompute with zero source-data repair.
-- ─────────────────────────────────────────────────────────────────────────────────────
SET IDENTITY_INSERT dbo.customers ON;
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (5002, N'Marisol', N'Duarte', N'marisol.duarte2026@gmail.com', '+15035550123', '6640',
  '1985-02-25', N'800 SW 6th Ave', N'Portland', 'OR', '97204', N'Halcyon Foods',
  'ios', '2026-08-19T08:00:00', '2026-08-19T08:00:00', 'P-MARISOL');
SET IDENTITY_INSERT dbo.customers OFF;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 3. CUSTOMER UPDATES -- 85 rows.
--
-- 60 of them deliberately DO NOT touch updated_at. Not every write path in a real
-- application maintains that column, and this is what makes the naive
-- `WHERE updated_at > last_run` extractor lose rows: the row genuinely changed, its
-- timestamp did not, and no error is raised. The change feed sees all 85; a timestamp
-- watermark sees 25.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #to_update;
GO
CREATE TABLE #to_update (customer_id INT PRIMARY KEY, touches_updated_at BIT);
INSERT INTO #to_update (customer_id, touches_updated_at)
SELECT TOP (85) c.customer_id,
       CASE WHEN ROW_NUMBER() OVER (ORDER BY c.customer_id) <= 25 THEN 1 ELSE 0 END
FROM dbo.customers c
WHERE c._seed_person_id LIKE 'P-SINGLE-%'
ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
         CONCAT('upd1:', CAST(c.customer_id AS VARCHAR(12))))));
GO

-- The honest write path: changes the data AND the timestamp.
UPDATE c
   SET c.employer_name = CONCAT(c.employer_name, N' (Div 2)'),
       c.updated_at    = '2026-08-19T08:30:00'
FROM dbo.customers c
JOIN #to_update u ON u.customer_id = c.customer_id
WHERE u.touches_updated_at = 1;
GO

-- THE LOSSY WRITE PATH: changes the data and LEAVES updated_at ALONE. 60 rows that a
-- timestamp-based extractor will never see again.
UPDATE c
   SET c.city = CONCAT(c.city, N' Metro')
FROM dbo.customers c
JOIN #to_update u ON u.customer_id = c.customer_id
WHERE u.touches_updated_at = 0;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 4. ADVANCE ACTIVITY -- 30 status transitions plus 20 new advances.
-- New statuses are written with mixed spellings, because that is what the source does.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #adv_upd;
GO
CREATE TABLE #adv_upd (advance_id INT PRIMARY KEY);
INSERT INTO #adv_upd (advance_id)
SELECT TOP (30) a.advance_id
FROM dbo.advances a
JOIN dbo.customers c ON c.customer_id = a.customer_id
WHERE c._seed_person_id LIKE 'P-SINGLE-%'
  AND LOWER(LTRIM(RTRIM(a.status))) = 'funded'
ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
         CONCAT('advu1:', CAST(a.advance_id AS VARCHAR(12))))));
GO

UPDATE a
   SET a.status      = 'Paid Off',
       a.paid_off_at = '2026-08-19T09:00:00',
       a.updated_at  = '2026-08-19T09:00:00'
FROM dbo.advances a
JOIN #adv_upd u ON u.advance_id = a.advance_id;
GO

SET IDENTITY_INSERT dbo.advances ON;
WITH newadv AS (
    SELECT TOP (20) c.customer_id,
           8000 + ROW_NUMBER() OVER (ORDER BY c.customer_id) AS advance_id,
           ABS(CHECKSUM(HASHBYTES('MD5',
             CONCAT('nadv1:', CAST(c.customer_id AS VARCHAR(12)))))) AS h
    FROM dbo.customers c
    WHERE c._seed_person_id LIKE 'P-SINGLE-%'
      AND NOT EXISTS (SELECT 1 FROM dbo.advances a WHERE a.customer_id = c.customer_id)
    ORDER BY c.customer_id
)
INSERT INTO dbo.advances
 (advance_id, customer_id, external_advance_id, status, principal_amount, fee_amount,
  funded_at, paid_off_at, repayment_account_hash, created_at, updated_at)
SELECT n.advance_id, n.customer_id, CONCAT('FND-', FORMAT(n.advance_id, '0000')),
       CASE n.h % 3 WHEN 0 THEN 'funded' WHEN 1 THEN 'Funded' ELSE 'FUNDED ' END,
       CAST(100 + (n.h % 900) AS FLOAT), CAST((100 + (n.h % 900)) * 0.05 AS FLOAT),
       '2026-08-19T09:15:00', NULL,
       LOWER(CONVERT(CHAR(64), HASHBYTES('SHA2_256',
             CONCAT('acct:', CAST(n.customer_id AS VARCHAR(12)))), 2)),
       '2026-08-19T09:15:00', '2026-08-19T09:15:00'
FROM newadv n;
SET IDENTITY_INSERT dbo.advances OFF;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 5. CARD ACTIVITY -- 30 expiry updates.
-- ─────────────────────────────────────────────────────────────────────────────────────
UPDATE k
   SET k.exp_year   = k.exp_year + 3,
       k.updated_at = '2026-08-19T09:30:00'
FROM dbo.cards k
WHERE k.card_id IN (
    SELECT TOP (30) card_id FROM dbo.cards
    ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
             CONCAT('cardu1:', CAST(card_id AS VARCHAR(12))))))
);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 6. TRANSACTIONS -- 2,800 inserts, 150 of them BACKDATED.
--
-- The 150 are the day's late settlements and back-posted adjustments. They carry the
-- day's highest transaction_ids and timestamps up to 90 days old, so a posted_at
-- watermark set at yesterday's maximum skips every one of them, permanently, silently.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #tnums;
GO
CREATE TABLE #tnums (n INT PRIMARY KEY);
INSERT INTO #tnums (n)
SELECT TOP (2800) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

DECLARE @maxtx BIGINT = (SELECT ISNULL(MAX(transaction_id), 0) FROM dbo.transactions);
DECLARE @base  DATETIME2(3) = '2026-08-19T10:00:00';

INSERT INTO dbo.transactions
 (customer_id, advance_id, direction, amount_cents, currency, posted_at, created_at)
SELECT
    (SELECT TOP 1 customer_id FROM dbo.customers
      WHERE customer_id >= 1 + (h.hc % 5000) ORDER BY customer_id),
    NULL,
    CASE h.hd % 5 WHEN 0 THEN 'disbursement' WHEN 1 THEN 'repayment'
                  WHEN 2 THEN 'fee' WHEN 3 THEN 'refund' ELSE 'adjustment' END,
    CASE WHEN h.hd % 5 IN (1, 3) THEN -(500 + (h.ha % 49500))
         ELSE (500 + (h.ha % 49500)) END,
    'USD',
    -- The last 150 rows of the day are backdated by 1-90 days.
    CASE WHEN h.n > 2650
         THEN DATEADD(DAY, -(1 + (h.hb % 90)), DATEADD(SECOND, h.n * 20, @base))
         ELSE DATEADD(SECOND, h.n * 20, @base)
    END,
    DATEADD(SECOND, h.n * 20, @base)
FROM (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1c:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1a:', CAST(n AS VARCHAR(12)))))) AS ha,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1d:', CAST(n AS VARCHAR(12)))))) AS hd,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1b:', CAST(n AS VARCHAR(12)))))) AS hb
    FROM #tnums
) h
ORDER BY h.n;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 7. customer_history -- 1,200 inserts. 6.67% growth in one day, the fastest in the
-- source, and every row keeps a copy of a former value forever.
-- ─────────────────────────────────────────────────────────────────────────────────────
INSERT INTO dbo.customer_history
 (customer_id, changed_column, old_value, new_value, changed_at, changed_by)
SELECT
    (SELECT TOP 1 customer_id FROM dbo.customers
      WHERE customer_id >= 1 + (h.hc % 5000) ORDER BY customer_id),
    CASE h.hk % 4 WHEN 0 THEN 'email' WHEN 1 THEN 'phone'
                  WHEN 2 THEN 'employer_name' ELSE 'city' END,
    CONCAT(N'old-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('d1o:', CAST(h.n AS VARCHAR(12)))), 2))),
    CONCAT(N'new-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('d1n:', CAST(h.n AS VARCHAR(12)))), 2))),
    DATEADD(SECOND, h.n * 40, CAST('2026-08-19T10:00:00' AS DATETIME2(3))),
    CASE h.hk % 10 WHEN 0 THEN 'dba_manual' WHEN 1 THEN 'ops_batch' ELSE 'app' END
FROM (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1hc:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d1hk:', CAST(n AS VARCHAR(12)))))) AS hk
    FROM #tnums WHERE n <= 1200
) h;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- What the day actually did, measured. The load that follows should see these numbers.
-- ─────────────────────────────────────────────────────────────────────────────────────
SELECT 'churn_day1' AS stage,
       (SELECT COUNT(*) FROM dbo.customers)        AS customers_now,
       (SELECT COUNT(*) FROM dbo.advances)         AS advances_now,
       (SELECT COUNT(*) FROM dbo.cards)            AS cards_now,
       (SELECT COUNT(*) FROM dbo.transactions)     AS transactions_now,
       (SELECT COUNT(*) FROM dbo.customer_history) AS history_now;
GO

-- The soft-delete trap, stated as a number: 13 customers were hard-deleted this day and
-- the is_deleted column still reads 0 for every surviving row. A pipeline keyed on that
-- flag has just missed 13 deletions and has no way to know.
SELECT 'soft_delete_trap' AS stage,
       SUM(CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END) AS rows_flagged_deleted,
       13 AS rows_actually_hard_deleted_today
FROM dbo.customers;
GO

-- Orphan cards created by deleting customers out from under them (no FK on cards).
SELECT 'orphan_cards' AS stage, COUNT(*) AS orphan_cards
FROM dbo.cards k
LEFT JOIN dbo.customers c ON c.customer_id = k.customer_id
WHERE c.customer_id IS NULL;
GO
