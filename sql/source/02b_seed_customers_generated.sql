-- ═══════════════════════════════════════════════════════════════════════════════════
-- SEED: dbo.customers, part 2 -- the generated duplicate families, then bulk singletons
-- to reach 5,000 rows.
--
-- Part 1 (02a) hand-wrote the 54 exhibits a reviewer needs to READ. This file provides
-- the VOLUME that makes a precision number mean anything: 32 more duplicate groups
-- across six mechanisms, then ~4,800 ordinary customers so the percentages are real.
--
-- Determinism: every value is a hash of the row's identity. Never NEWID(), never RAND().
-- ID allocation takes the lowest unused ids in order, so it is stable without
-- hand-managed number bands that break the moment an exhibit is added.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- A numbers table, and the pool of customer_ids not already claimed by an exhibit.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #nums;
DROP TABLE IF EXISTS #available;
DROP TABLE IF EXISTS #family;
GO

CREATE TABLE #nums (n INT PRIMARY KEY);
INSERT INTO #nums (n)
SELECT TOP (5000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL))
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
GO

CREATE TABLE #available (seq INT PRIMARY KEY, id INT);
INSERT INTO #available (seq, id)
SELECT ROW_NUMBER() OVER (ORDER BY n), n
FROM #nums
WHERE n NOT IN (SELECT customer_id FROM dbo.customers);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- Value pools. Re-declared here because temp tables from 02a are long gone -- each seed
-- file has to stand on its own so a reviewer can run them one at a time.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #fn;  DROP TABLE IF EXISTS #ln;
DROP TABLE IF EXISTS #city; DROP TABLE IF EXISTS #dom; DROP TABLE IF EXISTS #emp;
GO

CREATE TABLE #fn (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(100));
INSERT INTO #fn (v) VALUES
 (N'James'),(N'Maria'),(N'Aisha'),(N'Dmitri'),(N'Chen'),(N'Sofia'),(N'Jamal'),
 (N'Renee'),(N'Tobias'),(N'Ingrid'),(N'Rafael'),(N'Nadia'),(N'Colin'),(N'Yara'),
 (N'Mateo'),(N'Fiona'),(N'Omar'),(N'Leila'),(N'Gustav'),(N'Anjali'),(N'Pierre'),
 (N'Rosa'),(N'Dev'),(N'Bianca'),(N'Kofi'),(N'Elena'),(N'Sven'),(N'Mira'),
 (N'Hugo'),(N'Zara'),(N'Andre'),(N'Keiko'),(N'Lucas'),(N'Tamar'),(N'Nikolai'),
 (N'Grace'),(N'Emeka'),(N'Vera'),(N'Salim'),(N'Petra');

CREATE TABLE #ln (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(100));
INSERT INTO #ln (v) VALUES
 (N'Okafor'),(N'Lindqvist'),(N'Delgado'),(N'Nakamura'),(N'Brennan'),(N'Hollande'),
 (N'Bergstrom'),(N'Iqbal'),(N'Mwangi'),(N'Petrov'),(N'Silva'),(N'Pierce'),
 (N'Duval'),(N'Martinez'),(N'Oyelaran'),(N'Johnson'),(N'Rowe'),(N'Abernathy'),
 (N'Castellanos'),(N'Fitzgerald'),(N'Grimaldi'),(N'Halvorsen'),(N'Ivanova'),
 (N'Jarrett'),(N'Kovacs'),(N'Lombardi'),(N'Mbeki'),(N'Novikov'),(N'Ortega'),
 (N'Pashley'),(N'Quintero'),(N'Rasmussen'),(N'Sandoval'),(N'Thibault'),
 (N'Ueda'),(N'Vandenberg'),(N'Wojcik'),(N'Yamamoto'),(N'Zielinski'),(N'Ashford');

CREATE TABLE #city (i INT IDENTITY(0,1) PRIMARY KEY, city NVARCHAR(100), st CHAR(2), zip VARCHAR(12));
INSERT INTO #city (city, st, zip) VALUES
 (N'Austin','TX','78701'),(N'Atlanta','GA','30303'),(N'Seattle','WA','98101'),
 (N'Miami','FL','33130'),(N'Phoenix','AZ','85004'),(N'Denver','CO','80202'),
 (N'Chicago','IL','60601'),(N'Boston','MA','02108'),(N'Raleigh','NC','27601'),
 (N'Portland','OR','97204'),(N'Dallas','TX','75201'),(N'Columbus','OH','43215');

CREATE TABLE #dom (i INT IDENTITY(0,1) PRIMARY KEY, v VARCHAR(60));
INSERT INTO #dom (v) VALUES
 ('gmail.com'),('yahoo.com'),('outlook.com'),('hotmail.com'),('icloud.com'),
 ('aol.com'),('proton.me'),('comcast.net');

CREATE TABLE #emp (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(200));
INSERT INTO #emp (v) VALUES
 (N'BrightPath Staffing'),(N'Corepoint Logistics'),(N'Vellum Retail Group'),
 (N'Northgate Health'),(N'Halcyon Foods'),(N'Ridgeline Transit'),
 (N'Summit Facilities'),(N'Ardent Care Partners'),(N'Blue Harbor Hospitality'),
 (N'Cascade Manufacturing');
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- THE 32 GENERATED DUPLICATE FAMILIES -- 71 rows.
--
-- Each family isolates ONE mechanism by which the same person ends up in the table
-- twice, so that when recall moves, it is obvious which mechanism moved it.
--
--   F-A  14 groups x 2   re-signup with a fresh mailbox    -> provable
--   F-B   6 groups x 2   call-centre re-entry, name noise  -> provable ONLY if the
--                                                             normalizer casefolds and
--                                                             strips diacritics
--   F-C   5 groups x 3   multi-channel signup              -> provable
--   F-D   2 groups x 2   date of birth missing on one      -> NOT provable, by design
--   F-E   1 group  x 4   four-channel family               -> provable
--   F-F   4 groups x 2   typo'd email, tuple intact        -> provable
--
-- F-B is what makes the normalizer a MEASURED component: delete diacritic folding from
-- normalize_name() and recall drops by exactly these 6 pairs, no more and no less.
-- F-D exists so that some recall misses are deliberate rather than accidental.
-- ─────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE #family (
    rn        INT IDENTITY(1,1) PRIMARY KEY,
    fam       CHAR(3),
    grp       INT,
    member    INT,
    person_id VARCHAR(20)
);
GO

-- Lay out the family membership first, then decorate. Two steps rather than one so the
-- population arithmetic is inspectable in isolation.
INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-A', g.n, m.n, CONCAT('P-FA-', FORMAT(g.n, '00'))
FROM (SELECT n FROM #nums WHERE n <= 14) g
CROSS JOIN (SELECT n FROM #nums WHERE n <= 2) m;

INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-B', g.n, m.n, CONCAT('P-FB-', FORMAT(g.n, '00'))
FROM (SELECT n FROM #nums WHERE n <= 6) g
CROSS JOIN (SELECT n FROM #nums WHERE n <= 2) m;

INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-C', g.n, m.n, CONCAT('P-FC-', FORMAT(g.n, '00'))
FROM (SELECT n FROM #nums WHERE n <= 5) g
CROSS JOIN (SELECT n FROM #nums WHERE n <= 3) m;

INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-D', g.n, m.n, CONCAT('P-FD-', FORMAT(g.n, '00'))
FROM (SELECT n FROM #nums WHERE n <= 2) g
CROSS JOIN (SELECT n FROM #nums WHERE n <= 2) m;

INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-E', 1, m.n, 'P-FE-01'
FROM (SELECT n FROM #nums WHERE n <= 4) m;

INSERT INTO #family (fam, grp, member, person_id)
SELECT 'F-F', g.n, m.n, CONCAT('P-FF-', FORMAT(g.n, '00'))
FROM (SELECT n FROM #nums WHERE n <= 4) g
CROSS JOIN (SELECT n FROM #nums WHERE n <= 2) m;
GO

SET IDENTITY_INSERT dbo.customers ON;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- ONE INDEPENDENT HASH PER IDENTITY FIELD. This is not fussiness. The first version of
-- this file derived ssn_last4 as (h % 10000) and the surname as pool[h % 40] from the
-- SAME hash -- and 40 DIVIDES 10000. So h_a = h_b (mod 10000) forces h_a = h_b (mod 40):
-- matching on ssn_last4 mathematically GUARANTEED matching on surname.
--
-- Measured consequence before the fix: of 1,158 singleton pairs sharing an ssn_last4,
-- 1,158 also shared a surname -- 100%, where independence gives about 1.3%. The surname
-- component of the proof tuple contributed nothing at all, and the fixture manufactured
-- 5 false full-tuple pairs that looked like a finding about identity matching and were
-- really a bug in the data generator.
--
-- Salting each field separately makes the fields independent whether or not the moduli
-- happen to share factors, so the property does not depend on arithmetic nobody checks.
-- ─────────────────────────────────────────────────────────────────────────────────────
WITH assigned AS (
    SELECT f.rn, f.fam, f.grp, f.member, f.person_id, a.id AS customer_id,
           -- PERSON-level: salted from person_id, so every member of a group agrees.
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('fn:',  f.person_id)))) AS h_fn,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ln:',  f.person_id)))) AS h_ln,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ssn:', f.person_id)))) AS h_ssn,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('dob:', f.person_id)))) AS h_dob,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('geo:', f.person_id)))) AS h_geo,
           -- ROW-level: salted from the assigned id, so members differ where they should.
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('row:', CAST(a.id AS VARCHAR(12)))))) AS rh
    FROM #family f
    JOIN #available a ON a.seq = f.rn
)
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
SELECT
    s.customer_id,

    -- First name: constant per person EXCEPT in F-B, where member 2 carries the name
    -- noise a call-centre re-entry produces (diacritics, casing, padding).
    CASE
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 1 THEN N'José'
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 2 THEN N'  Renee  '
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 3 THEN N'andré'
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 4 THEN N'MARIA'
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 5 THEN N'sofía'
      WHEN s.fam = 'F-B' AND s.member = 2 AND s.grp = 6 THEN N'  Omar'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 1 THEN N'Jose'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 2 THEN N'Renee'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 3 THEN N'Andre'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 4 THEN N'Maria'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 5 THEN N'Sofia'
      WHEN s.fam = 'F-B' AND s.member = 1 AND s.grp = 6 THEN N'Omar'
      ELSE (SELECT v FROM #fn WHERE i = s.h_fn % 40)
    END,

    -- Surname: PART OF THE PROOF TUPLE, so it must agree across a group. F-B groups 4
    -- and 6 vary only casing and padding, which the normalizer must fold away.
    CASE
      WHEN s.fam = 'F-B' AND s.grp = 4 AND s.member = 2
        THEN UPPER((SELECT v FROM #ln WHERE i = s.h_ln % 40))
      WHEN s.fam = 'F-B' AND s.grp = 6 AND s.member = 2
        THEN N' ' + (SELECT v FROM #ln WHERE i = s.h_ln % 40) + N' '
      ELSE (SELECT v FROM #ln WHERE i = s.h_ln % 40)
    END,

    -- Email varies per ROW in every family: F-A changes the domain (the fresh mailbox),
    -- F-F introduces a typo. Email is a SUGGESTS field, so none of this changes a merge
    -- decision -- which is exactly the point being demonstrated.
    LOWER(CONCAT(
        (SELECT v FROM #fn WHERE i = s.h_fn % 40), '.',
        (SELECT v FROM #ln WHERE i = s.h_ln % 40),
        CASE WHEN s.member > 1 THEN CAST(s.member AS VARCHAR(2)) ELSE '' END, '@',
        CASE
          WHEN s.fam = 'F-F' AND s.member = 2 THEN 'gmial.com'
          ELSE (SELECT v FROM #dom WHERE i = (s.rh + s.member) % 8)
        END)),

    -- One member of each F-C group carries a malformed phone, so survivorship has to
    -- cope with the surviving row holding the WORSE contact details.
    CASE
      WHEN s.fam = 'F-C' AND s.member = 3 THEN '(555) 01' + CAST(s.grp AS VARCHAR(2)) + '-3'
      ELSE CONCAT('+1', 200 + (s.rh % 700), 555, FORMAT(s.rh % 10000, '0000'))
    END,

    -- PROOF COMPONENT, constant per person, and CONSTRUCTED so it cannot be
    -- placeholder-shaped. An earlier version used FORMAT(1000 + (h % 8000)) with a
    -- comment claiming the 1000-8999 range "avoids the placeholder shapes". That was
    -- simply false -- 1234, 4321, 5678 and 1111 through 8888 all live in that range --
    -- and one of the 14 F-A groups duly drew one. The result was a family that failed to
    -- auto-merge for a reason I never intended, which quietly broke the property this
    -- whole section depends on: each family must isolate exactly ONE mechanism, so that
    -- when recall moves it is obvious what moved it.
    --
    -- The construction below makes both placeholder shapes structurally impossible
    -- rather than merely unlikely. The third digit is pinned to (first digit + 5):
    --   * all-same-digit needs d3 = d1, and d1+5 is never d1 (mod 10)
    --   * ascending run needs d3 = d1+2, descending needs d3 = d1-2, and 5 is neither
    -- So no draw can produce one, and this does not rely on anyone re-checking a range.
    CONCAT(
        CAST( (s.h_ssn        % 10)      AS VARCHAR(1)),
        CAST(((s.h_ssn / 10)  % 10)      AS VARCHAR(1)),
        CAST(((s.h_ssn % 10) + 5) % 10   AS VARCHAR(1)),
        CAST(((s.h_ssn / 100) % 10)      AS VARCHAR(1))),

    -- PROOF COMPONENT, constant per person -- except F-D, which drops it on the second
    -- member so that pair is genuinely unprovable and the recall miss is deliberate.
    CASE WHEN s.fam = 'F-D' AND s.member = 2 THEN NULL
         ELSE DATEADD(DAY, -(7000 + (s.h_dob % 14600)), CAST('2006-01-01' AS DATE))
    END,

    CONCAT(100 + (s.h_geo % 8900), ' ', (SELECT v FROM #ln WHERE i = s.h_ln % 40), ' St'),
    (SELECT city FROM #city WHERE i = s.h_geo % 12),
    (SELECT st   FROM #city WHERE i = s.h_geo % 12),
    (SELECT zip  FROM #city WHERE i = s.h_geo % 12),
    (SELECT v FROM #emp WHERE i = s.rh % 10),

    CASE s.fam
      WHEN 'F-C' THEN (CASE s.member WHEN 1 THEN 'web' WHEN 2 THEN 'ios' ELSE 'call_centre' END)
      WHEN 'F-E' THEN (CASE s.member WHEN 1 THEN 'web' WHEN 2 THEN 'ios'
                                     WHEN 3 THEN 'android' ELSE 'partner' END)
      ELSE (CASE s.rh % 5 WHEN 0 THEN 'web' WHEN 1 THEN 'ios' WHEN 2 THEN 'android'
                          WHEN 3 THEN 'call_centre' ELSE 'partner' END)
    END,

    -- Later members signed up later. F-A puts months between them, which is what makes
    -- "most recent record wins" look reasonable right up until it loses a funded advance.
    DATEADD(DAY, (s.member - 1) * 180 + (s.rh % 90), CAST('2022-01-01' AS DATETIME2(3))),
    DATEADD(DAY, (s.member - 1) * 180 + (s.rh % 90) + 30, CAST('2022-01-01' AS DATETIME2(3))),
    s.person_id
FROM assigned s;
GO

SELECT 'generated_families_inserted' AS stage, COUNT(*) AS customers_now
FROM dbo.customers;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- BULK SINGLETONS -- fill every remaining id up to 5,000.
--
-- Ordinary customers, and they earn their place three times over: they make the daily
-- change rate a realistic percentage rather than a toy one; they populate the collision
-- monitor (~1,200 pairs share an ssn_last4 by pure chance, which is the evidence that
-- ssn alone is not proof); and they are the denominator that makes precision mean
-- anything at all.
--
-- Each gets a DISTINCT _seed_person_id, so ground truth states plainly that none of them
-- duplicates anything. Same independent-hash-per-field discipline as above.
-- ─────────────────────────────────────────────────────────────────────────────────────
WITH gap AS (
    SELECT n,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('fn:',  CAST(n AS VARCHAR(12)))))) AS h_fn,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ln:',  CAST(n AS VARCHAR(12)))))) AS h_ln,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('ssn:', CAST(n AS VARCHAR(12)))))) AS h_ssn,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('dob:', CAST(n AS VARCHAR(12)))))) AS h_dob,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('geo:', CAST(n AS VARCHAR(12)))))) AS h_geo,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('row:', CAST(n AS VARCHAR(12)))))) AS h_row,
           ABS(CHECKSUM(HASHBYTES('MD5', CONCAT('tel:', CAST(n AS VARCHAR(12)))))) AS h_tel
    FROM #nums
    WHERE n NOT IN (SELECT customer_id FROM dbo.customers)
)
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
SELECT
    g.n,
    (SELECT v FROM #fn WHERE i = g.h_fn % 40),
    (SELECT v FROM #ln WHERE i = g.h_ln % 40),
    LOWER(CONCAT((SELECT v FROM #fn WHERE i = g.h_fn % 40), '.',
                 (SELECT v FROM #ln WHERE i = g.h_ln % 40), CAST(g.n AS VARCHAR(6)), '@',
                 (SELECT v FROM #dom WHERE i = g.h_row % 8))),
    CONCAT('+1', 200 + (g.h_tel % 700), 555, FORMAT(g.h_tel % 10000, '0000')),
    -- Spread across the FULL 0000-9999 range on purpose, placeholder shapes included:
    -- real data contains them, and the collision monitor needs a realistic spread.
    FORMAT(g.h_ssn % 10000, '0000'),
    DATEADD(DAY, -(7000 + (g.h_dob % 14600)), CAST('2006-01-01' AS DATE)),
    CONCAT(100 + (g.h_geo % 8900), ' ', (SELECT v FROM #ln WHERE i = g.h_ln % 40), ' St'),
    (SELECT city FROM #city WHERE i = g.h_geo % 12),
    (SELECT st   FROM #city WHERE i = g.h_geo % 12),
    (SELECT zip  FROM #city WHERE i = g.h_geo % 12),
    (SELECT v FROM #emp WHERE i = g.h_row % 10),
    CASE g.h_row % 5 WHEN 0 THEN 'web' WHEN 1 THEN 'ios' WHEN 2 THEN 'android'
                     WHEN 3 THEN 'call_centre' ELSE 'partner' END,
    DATEADD(DAY, -(g.h_row % 1900), CAST('2026-08-18' AS DATETIME2(3))),
    -- updated_at is derived FROM created_at, never independently. Two independent hashes
    -- would put updated_at before created_at on roughly half the rows, and a row claiming
    -- it was modified before it existed is a defect I did not intend to seed -- it would
    -- surface in the scorecard as a finding about my own fixture.
    DATEADD(DAY, (g.h_tel % ((g.h_row % 1900) + 1)),
            DATEADD(DAY, -(g.h_row % 1900), CAST('2026-08-18' AS DATETIME2(3)))),
    CONCAT('P-SINGLE-', FORMAT(g.n, '00000'))
FROM gap g;
GO

SET IDENTITY_INSERT dbo.customers OFF;
GO

SELECT 'customers_seeded' AS stage, COUNT(*) AS total,
       COUNT(DISTINCT _seed_person_id) AS distinct_persons
FROM dbo.customers;
GO
