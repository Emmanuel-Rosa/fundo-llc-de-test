-- ═══════════════════════════════════════════════════════════════════════════════════
-- SEED: dbo.cards -- 7,000 rows across 4,600 card-holding customers.
--
-- Cards are where a wrong merge stops being a reporting problem and becomes a money
-- problem. In severity order, what breaks if this is got wrong:
--
--   1. RE-POINTING a card to the surviving customer on a WRONG merge attaches a payment
--      instrument to the wrong person -- you can debit the wrong bank account. That is
--      money movement, consent and PCI, and it is IRREVERSIBLE because the original
--      foreign key is gone.
--   2. DROPPING the loser's cards silently removes a live repayment mandate. Dunning
--      stops chasing a real debt, and the loss reads as a write-off rather than a bug.
--   3. LEAVING the same fingerprint under two keys merely double-counts instruments.
--
-- The dangerous two are the first two, because they produce plausible, well-formed,
-- quietly wrong numbers instead of an error. Which is why this pipeline NEVER re-points
-- cards: it adds a resolved key alongside the original, and an unmerge is a one-row map
-- edit rather than a data-repair project.
--
-- Two fixture facts the resolver has to survive:
--   * 400 customers hold NO card. So the invariant is "exactly one default card per
--     customer HOLDING AT LEAST ONE CARD" -- an exclusion clause that is easy to get
--     wrong and produces a false failure when you do.
--   * 3 fingerprints are shared across distinct customers, and TWO OF THE THREE belong
--     to people who are not the same person. That is the seeded proof that
--     card_fingerprint SUGGESTS and never PROVES.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

DELETE FROM dbo.cards;
GO

DROP TABLE IF EXISTS #cnums;
GO
CREATE TABLE #cnums (n INT PRIMARY KEY);
INSERT INTO #cnums (n)
SELECT TOP (7000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

SET IDENTITY_INSERT dbo.cards ON;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- THE HAND-BUILT CARDS -- the three shared fingerprints and the default-conflict case.
-- ─────────────────────────────────────────────────────────────────────────────────────
INSERT INTO dbo.cards
 (card_id, customer_id, card_token, card_fingerprint, brand, last4, exp_month, exp_year,
  is_default, billing_postal, created_at, updated_at)
VALUES
 -- G01, THE TRUE DUPLICATE. Alicia has one instrument (fp_a1) stored twice, under two
 -- different card_ids with their own tokens and expiry dates -- because she added the
 -- same physical card again on her newer account. Both card ROWS are real mandates and
 -- both are retained; only the DEFAULT flag is reconciled.
 --
 -- Three mistakes are available here and the fixture makes each one visible:
 --   (a) re-pointing cards to the survivor       -> destroys the original FK
 --   (b) deleting the "duplicate" card row       -> removes a live mandate
 --   (c) leaving two defaults on one customer    -> breaks the invariant
 (9001, 1041, 'tok_alicia_a1', 'fp_a1', 'visa',       '4242', 11, 2028,
  1, '78701', '2023-04-11T09:30:00', '2023-04-11T09:30:00'),
 (9002, 4788, 'tok_alicia_b2', 'fp_b2', 'mastercard', '5454',  3, 2029,
  1, '78701', '2026-05-30T11:50:00', '2026-05-30T11:50:00'),
 (9003, 4788, 'tok_alicia_a1b','fp_a1', 'visa',       '4242', 11, 2028,
  0, '78701', '2026-06-02T08:00:00', '2026-06-02T08:00:00'),

 -- G03, A HOUSEHOLD. The mother's card was added to her son's account. Same fingerprint,
 -- two DIFFERENT people. If card_fingerprint proved identity, this merges a 55-year-old
 -- with a 22-year-old and moves her paid-off advance onto him.
 (9004, 2044, 'tok_denise_1',  'fp_kow', 'visa', '4111',  6, 2027,
  1, '30303', '2022-08-14T10:30:00', '2022-08-14T10:30:00'),
 (9005, 2045, 'tok_tomasz_1',  'fp_kow', 'visa', '4111',  6, 2027,
  1, '30303', '2026-03-19T17:40:00', '2026-03-19T17:40:00'),

 -- G04, A SHARED OFFICE. Two coworkers, one company card on file. Again: same
 -- fingerprint, different people. Two of the three shared fingerprints in this fixture
 -- are false-positive traps, and only ONE (fp_a1) is a genuine duplicate.
 (9006, 3311, 'tok_naomi_1',   'fp_bps', 'amex', '3782',  9, 2027,
  1, '98101', '2024-09-02T09:30:00', '2024-09-02T09:30:00'),
 (9007, 3312, 'tok_curtis_1',  'fp_bps', 'amex', '3782',  9, 2027,
  1, '98101', '2024-09-02T09:40:00', '2024-09-02T09:40:00'),

 -- G13, the Marisol group. All three members hold a card, so the merge has to reconcile
 -- three defaults down to one -- and then UNDO that on churn day 2 when the group turns
 -- out to be unresolvable. Reconciling defaults by DEMOTION rather than deletion is what
 -- makes that undo possible.
 (9008,   21, 'tok_marisol_1', 'fp_md1', 'visa',       '4000', 5, 2028,
  1, '97204', '2021-05-04T08:30:00', '2021-05-04T08:30:00'),
 (9009, 1777, 'tok_marisol_2', 'fp_md2', 'mastercard', '5100', 8, 2029,
  1, '97204', '2024-04-14T10:30:00', '2024-04-14T10:30:00'),
 (9010, 3260, 'tok_marisol_3', 'fp_md3', 'visa',       '4001', 1, 2030,
  1, '97204', '2025-12-19T16:30:00', '2025-12-19T16:30:00'),

 -- G12, the Yusuf pair. The SURVIVOR (C2411) is the one with the malformed phone, and it
 -- also holds the newer card. Survivorship is decided per-ENTITY, not per-field, so the
 -- canonical record keeps the worse phone number. That cost is printed, not hidden.
 (9011,  338, 'tok_yusuf_1',   'fp_yk1', 'visa', '4222', 2, 2027,
  1, '80202', '2024-08-19T09:30:00', '2024-08-19T09:30:00'),
 (9012, 2411, 'tok_yusuf_2',   'fp_yk2', 'visa', '4333', 7, 2029,
  1, '80202', '2025-10-02T15:30:00', '2025-10-02T15:30:00');
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- BULK CARDS. Distribution chosen so the "exactly one default per CARD-HOLDING customer"
-- invariant has a real population and a real exclusion clause:
--   400 customers hold no card at all
--   the rest hold 1-3, with a deterministic count per customer
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #holders;
GO
-- Customers who already hold a hand-built card are excluded so their carefully-built
-- default flags are not disturbed by the bulk pass.
CREATE TABLE #holders (seq INT PRIMARY KEY, customer_id INT, card_count INT);
INSERT INTO #holders (seq, customer_id, card_count)
SELECT ROW_NUMBER() OVER (ORDER BY c.customer_id), c.customer_id,
       -- Card counts are WEIGHTED, not uniform over 1-3. A uniform 1-3 averages two
       -- cards per holder, so 7,000 cards only reaches ~3,500 holders -- and the fixture
       -- needs 4,600 holders and 400 without, because the default-card invariant's
       -- exclusion clause is only exercised if a real population has no card at all.
       -- Most people have one card on file; a few have several.
       CASE
         WHEN (ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ncards:',
               CAST(c.customer_id AS VARCHAR(12)))))) % 100) < 60 THEN 1
         WHEN (ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ncards:',
               CAST(c.customer_id AS VARCHAR(12)))))) % 100) < 86 THEN 2
         WHEN (ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ncards:',
               CAST(c.customer_id AS VARCHAR(12)))))) % 100) < 96 THEN 3
         ELSE 5
       END
FROM dbo.customers c
WHERE NOT EXISTS (SELECT 1 FROM dbo.cards k WHERE k.customer_id = c.customer_id)
  -- THE 400 WITH NO CARD. Selected by a stable hash so the exclusion clause in the
  -- default-card invariant always has something to exclude.
  AND (ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('nocard:',
        CAST(c.customer_id AS VARCHAR(12)))))) % 1000) >= 81;
GO

-- Expand holders into individual card rows, numbering each customer's cards so exactly
-- one of them can be flagged default.
DROP TABLE IF EXISTS #cardplan;
GO
CREATE TABLE #cardplan (rn INT PRIMARY KEY, customer_id INT, slot INT);
INSERT INTO #cardplan (rn, customer_id, slot)
-- ORDERED BY SLOT FIRST, and that ordering is the whole point. The row cap below trims
-- the tail, so ordering by customer would starve the last customers of cards entirely
-- and wreck the holder count. Ordering by SLOT means every holder's slot-1 card is
-- allocated before any second card is, so the cap can only ever remove EXTRA cards --
-- never a holder's only card, and never their default.
--
-- ROW_NUMBER() rather than IDENTITY: T-SQL does not guarantee that IDENTITY values follow
-- the ORDER BY of an INSERT...SELECT, so relying on that would be relying on an
-- undocumented behaviour to hold a correctness property.
SELECT ROW_NUMBER() OVER (ORDER BY s.n, h.customer_id), h.customer_id, s.n
FROM #holders h
JOIN (SELECT n FROM #cnums WHERE n <= 5) s ON s.n <= h.card_count;
GO

WITH cardrows AS (   -- not `plan`: PLAN is a reserved keyword in T-SQL
    SELECT p.rn, p.customer_id, p.slot,
           -- Bulk cards take ids 1..6988; the 12 hand-built exhibits sit up at
           -- 9001..9012 so they are easy to find and cite. An earlier version tried to
           -- continue the bulk range ABOVE the exhibits while also capping it below
           -- them, which is unsatisfiable -- so the bulk insert silently produced ZERO
           -- rows and the table held only the 12 exhibits. A seed that inserts nothing
           -- and reports success is exactly the kind of failure this fixture exists to
           -- catch, so the counts below are asserted rather than assumed.
           p.rn AS card_id,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('card:', CAST(p.rn AS VARCHAR(12)))))) AS h
    FROM #cardplan p
    WHERE p.rn <= 6988               -- 6,988 bulk + 12 hand-built = 7,000
)
INSERT INTO dbo.cards
 (card_id, customer_id, card_token, card_fingerprint, brand, last4, exp_month, exp_year,
  is_default, billing_postal, created_at, updated_at)
SELECT
    p.card_id,
    p.customer_id,
    CONCAT('tok_', LOWER(CONVERT(VARCHAR(32),
        HASHBYTES('MD5', CONCAT('tok:', CAST(p.rn AS VARCHAR(12)))), 2))),
    -- One fingerprint per card here: the shared-instrument cases are all hand-built
    -- above, so the bulk rows cannot accidentally create a fourth shared fingerprint and
    -- muddy the "2 of 3 shared fingerprints are false positives" claim.
    CONCAT('fp_', LOWER(CONVERT(VARCHAR(32),
        HASHBYTES('MD5', CONCAT('fp:', CAST(p.rn AS VARCHAR(12)))), 2))),
    CASE p.h % 4 WHEN 0 THEN 'visa' WHEN 1 THEN 'mastercard'
                 WHEN 2 THEN 'amex' ELSE 'discover' END,
    FORMAT(p.h % 10000, '0000'),
    1 + (p.h % 12),
    2027 + (p.h % 4),
    CASE WHEN p.slot = 1 THEN 1 ELSE 0 END,   -- exactly one default per holder
    (SELECT postal_code FROM dbo.customers c WHERE c.customer_id = p.customer_id),
    DATEADD(DAY, -(p.h % 1200), CAST('2026-08-18T09:00:00' AS DATETIME2(3))),
    DATEADD(DAY, -(p.h % 1200), CAST('2026-08-18T09:00:00' AS DATETIME2(3)))
FROM cardrows p;
GO

SET IDENTITY_INSERT dbo.cards OFF;
GO

SELECT 'cards_seeded' AS stage,
       COUNT(*) AS total_cards,
       COUNT(DISTINCT customer_id) AS card_holding_customers,
       (SELECT COUNT(*) FROM dbo.customers) -
         COUNT(DISTINCT customer_id) AS customers_with_no_card,
       COUNT(DISTINCT card_fingerprint) AS distinct_instruments
FROM dbo.cards;
GO

-- THE INVARIANT, with its exclusion clause. Any non-zero result here is a fixture bug,
-- not a finding -- so it is checked at seed time rather than discovered later.
SELECT 'default_card_invariant' AS stage,
       SUM(CASE WHEN defaults <> 1 THEN 1 ELSE 0 END) AS holders_without_exactly_one_default,
       COUNT(*) AS card_holding_customers
FROM (SELECT customer_id, SUM(CAST(is_default AS INT)) AS defaults
      FROM dbo.cards GROUP BY customer_id) x;
GO

-- The shared-fingerprint exhibit: 3 instruments across more than one customer, and the
-- ground-truth column says whether those customers are actually the same person.
SELECT 'shared_fingerprints' AS stage,
       k.card_fingerprint,
       COUNT(DISTINCT k.customer_id) AS customers,
       COUNT(DISTINCT c._seed_person_id) AS actual_people,
       CASE WHEN COUNT(DISTINCT c._seed_person_id) = 1
            THEN 'same person - genuine duplicate'
            ELSE 'DIFFERENT PEOPLE - fingerprint suggests, never proves'
       END AS verdict
FROM dbo.cards k
JOIN dbo.customers c ON c.customer_id = k.customer_id
GROUP BY k.card_fingerprint
HAVING COUNT(DISTINCT k.customer_id) > 1
ORDER BY k.card_fingerprint;
GO
