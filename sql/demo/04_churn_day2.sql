-- ═══════════════════════════════════════════════════════════════════════════════════
-- CHURN DAY 2 -- a quieter day, and the one that carries the thesis.
--
-- ~1,340 changed rows (0.46%), and one of them matters far more than the rest:
--
--   C5002 -- the fourth Marisol Duarte record, which arrived yesterday and was merged
--   automatically on proof -- GAINS A FUNDED ADVANCE. The group now holds two
--   money-moved members, so it is no longer safely resolvable and the merge must be
--   WITHDRAWN: three aliases revert to canonical and the group goes to the review queue.
--
-- That withdrawal is the whole argument for merging by INDIRECTION rather than mutation.
-- No source row is rewritten, no foreign key is re-pointed, no card changes hands. The
-- merge was a claim recorded in a map; withdrawing it is a map recompute, and it costs a
-- single row edit rather than a data-repair project.
--
-- Note the deliberate ASYMMETRY it demonstrates: the resolver may automatically UNMERGE
-- (it is withdrawing a claim it made) and may NEVER automatically merge from the
-- suggestive tier (that would be adding one). Withdrawal is always safe; assertion is not.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 1. THE ONE THAT MATTERS -- C5002 gets funded.
-- ─────────────────────────────────────────────────────────────────────────────────────
SET IDENTITY_INSERT dbo.advances ON;
INSERT INTO dbo.advances
 (advance_id, customer_id, external_advance_id, status, principal_amount, fee_amount,
  funded_at, paid_off_at, repayment_account_hash, created_at, updated_at)
VALUES
 -- A DIFFERENT repayment account from A4408 (which is on the 'd' hash), so this lands in
 -- the "different instruments, overlapping funded windows" bucket -- the first-party
 -- fraud signal, not a data-quality ticket. Same tuple, two live advances, two accounts.
 (8500, 5002, 'FND-8500', 'funded', 375.00, 18.75,
  '2026-08-20T09:00:00', NULL, REPLICATE('c', 64),
  '2026-08-20T08:45:00', '2026-08-20T09:00:00');
SET IDENTITY_INSERT dbo.advances OFF;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 2. A CUSTOMER LOSES THEIR ONLY CARD.
--
-- This drives a card-holding customer to zero cards, which is what exercises the
-- exclusion clause in "exactly one default card per customer HOLDING AT LEAST ONE CARD".
-- A check written without that clause reports a false failure here.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #solo;
GO
CREATE TABLE #solo (customer_id INT PRIMARY KEY, card_id INT);
INSERT INTO #solo (customer_id, card_id)
SELECT TOP (1) k.customer_id, MIN(k.card_id)
FROM dbo.cards k
JOIN dbo.customers c ON c.customer_id = k.customer_id
WHERE c._seed_person_id LIKE 'P-SINGLE-%'
GROUP BY k.customer_id
HAVING COUNT(*) = 1
ORDER BY k.customer_id;
GO

DELETE FROM dbo.cards WHERE card_id IN (SELECT card_id FROM #solo);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 3. 4 MORE HARD DELETES and 20 customer updates -- a normal quiet day.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #del2;
GO
CREATE TABLE #del2 (customer_id INT PRIMARY KEY);
INSERT INTO #del2 (customer_id)
SELECT TOP (4) c.customer_id
FROM dbo.customers c
WHERE c._seed_person_id LIKE 'P-SINGLE-%'
  AND NOT EXISTS (SELECT 1 FROM dbo.advances a WHERE a.customer_id = c.customer_id)
ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
         CONCAT('del2:', CAST(c.customer_id AS VARCHAR(12))))));
GO

DELETE FROM dbo.customers WHERE customer_id IN (SELECT customer_id FROM #del2);
GO

UPDATE c
   SET c.postal_code = RIGHT(CONCAT('0', c.postal_code), 5),
       c.updated_at  = '2026-08-20T09:30:00'
FROM dbo.customers c
WHERE c.customer_id IN (
    SELECT TOP (20) customer_id FROM dbo.customers
    WHERE _seed_person_id LIKE 'P-SINGLE-%'
    ORDER BY ABS(CHECKSUM(HASHBYTES('MD5',
             CONCAT('upd2:', CAST(customer_id AS VARCHAR(12))))))
);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- 4. TRANSACTIONS +900, customer_history +400.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #t2;
GO
CREATE TABLE #t2 (n INT PRIMARY KEY);
INSERT INTO #t2 (n)
SELECT TOP (900) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

DECLARE @base DATETIME2(3) = '2026-08-20T10:00:00';

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
    -- 45 backdated rows again -- about 5%, the same rate as every other day. A trap that
    -- only fires once looks like a one-off; one that fires every day is a property.
    CASE WHEN h.n > 855
         THEN DATEADD(DAY, -(1 + (h.hb % 90)), DATEADD(SECOND, h.n * 20, @base))
         ELSE DATEADD(SECOND, h.n * 20, @base)
    END,
    DATEADD(SECOND, h.n * 20, @base)
FROM (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2c:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2a:', CAST(n AS VARCHAR(12)))))) AS ha,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2d:', CAST(n AS VARCHAR(12)))))) AS hd,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2b:', CAST(n AS VARCHAR(12)))))) AS hb
    FROM #t2
) h
ORDER BY h.n;
GO

INSERT INTO dbo.customer_history
 (customer_id, changed_column, old_value, new_value, changed_at, changed_by)
SELECT
    (SELECT TOP 1 customer_id FROM dbo.customers
      WHERE customer_id >= 1 + (h.hc % 5000) ORDER BY customer_id),
    CASE h.hk % 4 WHEN 0 THEN 'email' WHEN 1 THEN 'phone'
                  WHEN 2 THEN 'postal_code' ELSE 'city' END,
    CONCAT(N'old-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('d2o:', CAST(h.n AS VARCHAR(12)))), 2))),
    CONCAT(N'new-', LOWER(CONVERT(VARCHAR(16),
        HASHBYTES('MD5', CONCAT('d2n:', CAST(h.n AS VARCHAR(12)))), 2))),
    DATEADD(SECOND, h.n * 60, CAST('2026-08-20T10:00:00' AS DATETIME2(3))),
    'app'
FROM (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2hc:', CAST(n AS VARCHAR(12)))))) AS hc,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('d2hk:', CAST(n AS VARCHAR(12)))))) AS hk
    FROM #t2 WHERE n <= 400
) h;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- What day 2 did, and the state of the group that drives the unmerge.
-- ─────────────────────────────────────────────────────────────────────────────────────
SELECT 'churn_day2' AS stage,
       (SELECT COUNT(*) FROM dbo.customers)        AS customers_now,
       (SELECT COUNT(*) FROM dbo.advances)         AS advances_now,
       (SELECT COUNT(*) FROM dbo.cards)            AS cards_now,
       (SELECT COUNT(*) FROM dbo.transactions)     AS transactions_now,
       (SELECT COUNT(*) FROM dbo.customer_history) AS history_now;
GO

-- THE UNMERGE TRIGGER, stated as data: the Marisol group now has two distinct
-- money-moved members on two different repayment instruments.
SELECT 'unmerge_trigger' AS stage,
       c.customer_id, a.advance_id, a.status,
       LEFT(a.repayment_account_hash, 8) AS instrument,
       CONVERT(VARCHAR(19), a.funded_at, 126) AS funded_at
FROM dbo.customers c
JOIN dbo.advances a ON a.customer_id = c.customer_id
WHERE c._seed_person_id = 'P-MARISOL'
  AND LOWER(LTRIM(RTRIM(a.status))) IN ('funded', 'paid_off', 'paid off')
ORDER BY c.customer_id;
GO

-- The zero-card customer, which the default-card invariant must EXCLUDE rather than fail.
SELECT 'zero_card_customer' AS stage, s.customer_id,
       (SELECT COUNT(*) FROM dbo.cards k WHERE k.customer_id = s.customer_id) AS cards_now
FROM #solo s;
GO
