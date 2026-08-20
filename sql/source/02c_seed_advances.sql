-- ═══════════════════════════════════════════════════════════════════════════════════
-- SEED: dbo.advances -- 8,000 rows, ~1.6 per customer.
--
-- Three things this table has to deliver, all of which the run then measures:
--
--   1. THE FUNDED VETO NEEDS A POPULATION. Roughly 5,300 advances have moved money
--      (funded or paid off), so "a customer who has moved money always survives a merge"
--      is a rule with real reach rather than an anecdote about two rows.
--
--   2. STATUS IS FREE TEXT: 15 raw spellings for 7 actual meanings, including 'funded',
--      'FUNDED ' (trailing space) and 'Funded'. A case-sensitive funded check misses
--      members it must not miss, and the consequence is a wrong merge on a group the
--      rule was specifically written to refuse.
--
--   3. THE TWO BAD COLUMN CHOICES have to produce their consequences:
--      external_advance_id VARCHAR(MAX) -- unindexable, and 3 rows carry trailing spaces
--        so SQL Server and DuckDB disagree on COUNT(DISTINCT) for identical data.
--      principal_amount FLOAT -- disables exact sum reconciliation, so the value-parity
--        check omits it and prints why.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

DELETE FROM dbo.advances;
GO

DROP TABLE IF EXISTS #anums;
GO
CREATE TABLE #anums (n INT PRIMARY KEY);
INSERT INTO #anums (n)
SELECT TOP (8000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- THE HAND-BUILT ADVANCES. Every one of these is referenced by an identity exhibit, so
-- the ids are fixed and cited in SOLUTION.md.
--
-- repayment_account_hash is the FUNDING INSTRUMENT and it is the field that decides the
-- two-funded cases:
--   G02a -- both members on account bh_9...  => one person with two accounts. A human
--           confirms and merges. Evidence: identical account hash on both sides.
--   G02b -- members on bh_1... and bh_2..., funded windows overlapping => for a
--           cash-advance lender this is a first-party fraud signal, not a data-quality
--           ticket, and merging it would destroy the evidence of a possible loss event.
-- ─────────────────────────────────────────────────────────────────────────────────────
SET IDENTITY_INSERT dbo.advances ON;
GO

INSERT INTO dbo.advances
 (advance_id, customer_id, external_advance_id, status, principal_amount, fee_amount,
  funded_at, paid_off_at, repayment_account_hash, created_at, updated_at)
VALUES
 -- G01: the older Alicia record holds the PAID-OFF advance. "Most recent wins" loses it.
 (2210, 1041, 'FND-2210', 'Paid Off', 300.00, 15.00,
  '2025-10-01T09:00:00', '2025-10-29T09:00:00',
  REPLICATE('a', 64), '2025-09-28T09:00:00', '2025-10-29T09:00:00'),

 -- G02a: TWO FUNDED, SAME INSTRUMENT. Note 'FUNDED ' with a trailing space on one side.
 (455,  777,  'FND-0455', 'FUNDED ', 250.00, 12.50,
  '2026-07-30T09:00:00', NULL,
  REPLICATE('9', 64), '2026-07-29T09:00:00', '2026-07-30T09:00:00'),
 (6031, 3512, 'FND-6031', 'funded',  350.00, 17.50,
  '2026-08-05T14:00:00', NULL,
  REPLICATE('9', 64), '2026-08-04T14:00:00', '2026-08-05T14:00:00'),

 -- G02b: TWO FUNDED, DIFFERENT INSTRUMENTS, overlapping windows. The fraud signal.
 (1877, 1298, 'FND-1877', 'Funded', 400.00, 20.00,
  '2026-08-01T12:00:00', NULL,
  REPLICATE('1', 64), '2026-07-31T12:00:00', '2026-08-01T12:00:00'),
 (7402, 4903, 'FND-7402', 'funded', 500.00, 25.00,
  '2026-08-06T09:00:00', NULL,
  REPLICATE('2', 64), '2026-08-05T09:00:00', '2026-08-06T09:00:00'),

 -- G03: the MOTHER holds a paid-off advance. Promoting email to proof attaches this to
 -- her teenage son, who never borrowed anything.
 (3390, 2044, 'FND-3390', 'Paid Off', 200.00, 10.00,
  '2025-01-15T10:00:00', '2025-02-12T10:00:00',
  REPLICATE('3', 64), '2025-01-12T10:00:00', '2025-02-12T10:00:00'),

 -- G06: Harold SENIOR is funded. A Sr/Jr merge is therefore a money-movement error.
 (5510, 1503, 'FND-5510', 'funded', 275.00, 13.75,
  '2026-06-18T09:30:00', NULL,
  REPLICATE('6', 64), '2026-06-17T09:30:00', '2026-06-18T09:30:00'),

 -- G13: one of the three Marisol rows is funded, which decides survivorship on the
 -- initial load. C5002 arrives in churn and gains its own funded advance on day 2,
 -- at which point the group has two money-moved members and the merge is WITHDRAWN.
 (4408, 1777, 'FND-4408', 'funded', 425.00, 21.25,
  '2026-07-20T10:00:00', NULL,
  REPLICATE('d', 64), '2026-07-19T10:00:00', '2026-07-20T10:00:00'),

 -- G14: Priya is a real staff member with a real paid-off advance and real repayments.
 -- Excluding her as "test data" because of her @fundo.com address removes all six of
 -- her transactions -- $262.50 of genuine repayments against this $250.00 principal --
 -- from the book, silently.
 (91,   88,   'FND-0091', 'Paid Off', 250.00, 12.50,
  '2026-04-01T09:00:00', '2026-04-29T09:00:00',
  REPLICATE('e', 64), '2026-03-29T09:00:00', '2026-04-29T09:00:00'),

 -- G15: Marcus TESTERMAN is funded. `email LIKE '%test%'` deletes a funded advance.
 (403,  402,  'FND-0403', 'FUNDED ', 325.00, 16.25,
  '2026-08-11T10:00:00', NULL,
  REPLICATE('f', 64), '2026-08-10T10:00:00', '2026-08-11T10:00:00'),

 -- TEST-DATA DETECTION RULE C: canonical artifact amounts. A principal of exactly 0.00
 -- or 999999.99 is not a loan, it is somebody exercising a form. These two are the only
 -- signal available for those accounts -- rules A and B cannot see them.
 (7990, 4973, 'FND-7990', 'funded', 0.00, 0.00,
  '2026-01-02T09:13:00', NULL,
  REPLICATE('0', 64), '2026-01-02T09:13:00', '2026-01-02T09:13:00'),
 (7991, 4974, 'FND-7991', 'funded', 999999.99, 0.00,
  '2026-01-02T09:14:00', NULL,
  REPLICATE('0', 64), '2026-01-02T09:14:00', '2026-01-02T09:14:00'),

 -- THE TRAILING-SPACE ROWS -- the entire point of the VARCHAR(MAX)-as-identifier
 -- exhibit, and they only work if they COLLIDE with a value that already exists.
 --
 -- These three deliberately duplicate FND-0100, FND-0200 and FND-0300, which the bulk
 -- insert below also generates, differing ONLY by trailing whitespace. The consequence:
 --   SQL Server pads on comparison, so it sees 6 rows carrying 3 distinct values
 --     -> COUNT(DISTINCT external_advance_id) = 7,997
 --   DuckDB does not pad, so it sees 6 distinct values
 --     -> COUNT(DISTINCT external_advance_id) = 8,000
 -- Byte-identical data, two different answers, and NEITHER engine is wrong -- they
 -- implement different (both documented) comparison semantics. Reported as a finding;
 -- value parity reconciles on advance_id instead, which is a key the database can
 -- actually enforce.
 --
 -- An earlier version gave these unique base values, which made the exhibit inert: with
 -- nothing to collide with, padding changes no count and the whole demonstration
 -- silently proves nothing.
 (7992, 1188, 'FND-0100 ',  'pending',  150.00, 7.50, NULL, NULL, NULL,
  '2026-06-01T09:00:00', '2026-06-01T09:00:00'),
 (7993, 2733, 'FND-0200  ', 'approved', 175.00, 8.75, NULL, NULL, NULL,
  '2026-06-02T09:00:00', '2026-06-02T09:00:00'),
 (7994, 3560, 'FND-0300 ',  'declined', 125.00, 6.25, NULL, NULL, NULL,
  '2026-06-03T09:00:00', '2026-06-03T09:00:00');
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- BULK ADVANCES -- fill to 8,000.
--
-- Status distribution is weighted so that ~66% of advances have moved money, which is
-- what makes the funded veto a real constraint. The 15 spellings are dealt out
-- deterministically so the "same meaning, different text" problem is reproducible.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #status;
GO
CREATE TABLE #status (
    i       INT IDENTITY(0,1) PRIMARY KEY,
    raw     VARCHAR(50),
    meaning VARCHAR(20),
    moved   BIT              -- money has moved: this is what the funded veto reads
);
INSERT INTO #status (raw, meaning, moved) VALUES
 ('funded',    'funded',    1),
 ('FUNDED ',   'funded',    1),   -- trailing space: defeats a case/exact-match check
 ('Funded',    'funded',    1),
 ('paid_off',  'paid_off',  1),
 ('Paid Off',  'paid_off',  1),
 ('PAID OFF',  'paid_off',  1),
 ('pending',   'pending',   0),
 ('Pending',   'pending',   0),
 ('approved',  'approved',  0),
 ('APPROVED',  'approved',  0),
 ('declined',  'declined',  0),
 ('Declined',  'declined',  0),
 ('cancelled', 'cancelled', 0),
 ('canceled',  'cancelled', 0),   -- two real spellings, one meaning
 ('expired',   'expired',   0);
GO

-- ELIGIBLE CUSTOMERS FOR BULK ADVANCES: SINGLETONS ONLY.
--
-- This restriction is load-bearing, and the first version got it wrong. Spreading bulk
-- advances across all 5,000 customers let duplicate-group members randomly pick up
-- funded advances, and the measured result was 18 groups with two money-moved members
-- where the design intends exactly TWO (G02a and G02b).
--
-- That is not a cosmetic problem. The refusal rule sends any group with two funded
-- members to a human, so 18 accidental ones would suppress 18 legitimate merges and make
-- both the recall figure and the two-funded exhibit meaningless -- the reader could no
-- longer tell a designed refusal from a coincidence.
--
-- Every advance belonging to a duplicate-group member is therefore hand-assigned above,
-- and the funded-veto population is exactly what the write-up claims it is.
DROP TABLE IF EXISTS #eligible;
GO
CREATE TABLE #eligible (seq INT PRIMARY KEY, customer_id INT);
INSERT INTO #eligible (seq, customer_id)
SELECT ROW_NUMBER() OVER (ORDER BY customer_id), customer_id
FROM dbo.customers
WHERE _seed_person_id LIKE 'P-SINGLE-%';   -- excludes exhibits, families AND test accounts
GO

DECLARE @eligible_count INT = (SELECT COUNT(*) FROM #eligible);

WITH gap AS (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('adv:',  CAST(n AS VARCHAR(12)))))) AS h,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('cust:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('amt:',  CAST(n AS VARCHAR(12)))))) AS ha,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('sts:',  CAST(n AS VARCHAR(12)))))) AS hs
    FROM #anums
    WHERE n NOT IN (SELECT advance_id FROM dbo.advances)
),
picked AS (
    SELECT g.*,
           -- Weighted toward the money-moved statuses (indexes 0-5 of 15) so roughly
           -- two thirds of the book has moved money.
           CASE WHEN g.hs % 100 < 66 THEN g.hs % 6 ELSE 6 + (g.hs % 9) END AS status_ix,
           (SELECT e.customer_id FROM #eligible e
             WHERE e.seq = 1 + (g.hc % @eligible_count)) AS cust_id
    FROM gap g
)
INSERT INTO dbo.advances
 (advance_id, customer_id, external_advance_id, status, principal_amount, fee_amount,
  funded_at, paid_off_at, repayment_account_hash, created_at, updated_at)
SELECT
    p.n,
    p.cust_id,
    CONCAT('FND-', FORMAT(p.n, '0000')),
    (SELECT raw FROM #status WHERE i = p.status_ix),
    -- Money in a FLOAT, deliberately. These values are chosen to be exactly the kind
    -- that a float cannot hold exactly, which is why chk_value_parity refuses to
    -- reconcile this column and says so instead of reconciling it badly.
    CAST(50 + (p.ha % 951) AS FLOAT) + 0.01 * (p.ha % 100),
    CAST((50 + (p.ha % 951)) * 0.05 AS FLOAT),
    CASE WHEN (SELECT moved FROM #status WHERE i = p.status_ix) = 1
         THEN DATEADD(DAY, -(p.h % 900), CAST('2026-08-18T09:00:00' AS DATETIME2(3)))
    END,
    CASE WHEN (SELECT meaning FROM #status WHERE i = p.status_ix) = 'paid_off'
         THEN DATEADD(DAY, -(p.h % 900) + 28, CAST('2026-08-18T09:00:00' AS DATETIME2(3)))
    END,
    CASE WHEN (SELECT moved FROM #status WHERE i = p.status_ix) = 1
         THEN LOWER(CONVERT(CHAR(64), HASHBYTES('SHA2_256',
                     CONCAT('acct:', CAST(p.cust_id AS VARCHAR(12)))), 2))
    END,
    DATEADD(DAY, -(p.h % 900) - 2, CAST('2026-08-18T09:00:00' AS DATETIME2(3))),
    DATEADD(DAY, -(p.h % 900),     CAST('2026-08-18T09:00:00' AS DATETIME2(3)))
FROM picked p;
GO

-- IDENTITY_INSERT stays ON across BOTH inserts: the bulk statement supplies advance_id
-- explicitly too, so that the ids cited in SOLUTION.md are stable rather than an
-- artefact of insertion order.
SET IDENTITY_INSERT dbo.advances OFF;
GO

-- NOTE THE COLLATION, because the obvious version of this query lies.
--
-- COUNT(DISTINCT status) returns 9, not 15. SQL Server's default collation is
-- case-INSENSITIVE and pads trailing spaces, so 'funded', 'FUNDED ' and 'Funded' are ONE
-- value to it. The 15 raw spellings are genuinely in the table; the server simply cannot
-- see them with a default comparison.
--
-- This is the whole reason the status mess is dangerous rather than merely ugly. A naive
-- `WHERE status = 'funded'` WORKS here -- it accidentally catches all three spellings --
-- so the bug is INVISIBLE at the source and appears only once the same logic is
-- re-implemented in the warehouse, where DuckDB compares case-sensitively and does not
-- pad. The defect is not the SQL; it is the unexamined dependency on a collation.
SELECT 'advances_seeded' AS stage,
       COUNT(*) AS total,
       SUM(CASE WHEN LOWER(LTRIM(RTRIM(status))) IN ('funded','paid_off','paid off')
                THEN 1 ELSE 0 END) AS money_moved,
       COUNT(DISTINCT status) AS spellings_sqlserver_can_see,
       COUNT(DISTINCT status COLLATE Latin1_General_BIN2) AS spellings_actually_present,
       COUNT(DISTINCT customer_id) AS customers_with_an_advance
FROM dbo.advances;
GO

-- The trailing-space count, measured with DATALENGTH rather than a string comparison.
--
-- This is not pedantry: `external_advance_id <> RTRIM(external_advance_id)` returns ZERO
-- rows here, because SQL Server pads before comparing and therefore considers the padded
-- and unpadded values EQUAL. The obvious way to detect trailing whitespace is defeated
-- by the exact behaviour being detected. DATALENGTH compares bytes and cannot be padded
-- out of the answer.
SELECT 'trailing_space_exhibit' AS stage,
       COUNT(DISTINCT external_advance_id) AS sqlserver_count_distinct,
       SUM(CASE WHEN DATALENGTH(external_advance_id)
                   <> DATALENGTH(RTRIM(external_advance_id)) THEN 1 ELSE 0 END)
         AS rows_with_trailing_space,
       COUNT(*) AS total_rows
FROM dbo.advances;
GO

-- The case-sensitivity exhibit, measured rather than asserted. SQL Server's default
-- collation is case-INSENSITIVE, so a naive `status = 'funded'` accidentally catches
-- 'FUNDED ' and 'Funded' too -- meaning the bug is INVISIBLE here and appears the moment
-- the same logic is re-implemented in the warehouse, where DuckDB compares
-- case-SENSITIVELY and does not pad. That collation dependency is the finding.
SELECT 'status_spelling_spread' AS stage,
       QUOTENAME(status, '''')                          AS raw_spelling,
       DATALENGTH(status)                               AS bytes,
       COUNT(*)                                         AS n
FROM dbo.advances
GROUP BY status COLLATE Latin1_General_BIN2, status
ORDER BY MIN(LOWER(LTRIM(RTRIM(status)))), bytes;
GO
