-- ═══════════════════════════════════════════════════════════════════════════════════
-- SEED: dbo.customers -- 5,000 rows.
--
-- THE SEED IS THE MEASURING INSTRUMENT. Every identity number in SOLUTION.md is an
-- output of this file, so it must reproduce byte-for-byte on the reviewer's machine.
-- That is why every "random" value here is a hash of a row ordinal and never NEWID()
-- or RAND(): both are non-deterministic, and a fixture that differs between runs
-- cannot score anything.
--
-- Customer ids are assigned EXPLICITLY via IDENTITY_INSERT rather than left to the
-- IDENTITY sequence. The write-up cites specific ids as exhibits (C1041 is the Alicia
-- Moreau case), so those ids are part of the deliverable and cannot be an accident of
-- insertion order.
--
-- GROUND TRUTH lives in _seed_person_id: rows sharing a value ARE the same human
-- being. It is absent from tables.yml, so it reaches no warehouse table and the
-- resolver physically cannot read it. That absence is the only reason the precision
-- number means anything -- and even then it is a self-consistency check rather than a
-- measurement, because the same person authored both this fixture and the rule that
-- reads it. SOLUTION.md says so in those words.
--
-- Composition is DERIVED, NOT ASSERTED. This file lays out an explicit fixture; the
-- scorer counts the groups, pairs, precision and recall at run time and prints them.
-- An earlier draft of the plan asserted 92 rows / 63 pairs and those totals did not
-- reconcile with the groups actually enumerated. Counting beats claiming.
-- ═══════════════════════════════════════════════════════════════════════════════════

USE fundo_src;
GO

SET NOCOUNT ON;
GO

-- Idempotent: a re-run rebuilds the fixture rather than doubling it. Child tables are
-- cleared by their own seed files, which run after this one; the delete order here
-- respects the single FK (advances -> customers).
DELETE FROM dbo.advances;
DELETE FROM dbo.customers;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- Deterministic value pools. Indexed by a hash of the row ordinal, so row N always
-- gets the same name, city and domain on every machine and every run.
-- ─────────────────────────────────────────────────────────────────────────────────────
DROP TABLE IF EXISTS #first_names;
DROP TABLE IF EXISTS #last_names;
DROP TABLE IF EXISTS #cities;
DROP TABLE IF EXISTS #domains;
DROP TABLE IF EXISTS #employers;
GO

CREATE TABLE #first_names (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(100));
INSERT INTO #first_names (v) VALUES
 (N'James'),(N'Maria'),(N'Aisha'),(N'Dmitri'),(N'Chen'),(N'Sofia'),(N'Jamal'),
 (N'Renee'),(N'Tobias'),(N'Ingrid'),(N'Rafael'),(N'Nadia'),(N'Colin'),(N'Yara'),
 (N'Mateo'),(N'Fiona'),(N'Omar'),(N'Leila'),(N'Gustav'),(N'Anjali'),(N'Pierre'),
 (N'Rosa'),(N'Dev'),(N'Bianca'),(N'Kofi'),(N'Elena'),(N'Sven'),(N'Mira'),
 (N'Hugo'),(N'Zara'),(N'Andre'),(N'Keiko'),(N'Lucas'),(N'Tamar'),(N'Nikolai'),
 (N'Grace'),(N'Emeka'),(N'Vera'),(N'Salim'),(N'Petra');
GO

CREATE TABLE #last_names (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(100));
INSERT INTO #last_names (v) VALUES
 (N'Okafor'),(N'Lindqvist'),(N'Moreau'),(N'Delgado'),(N'Nakamura'),(N'Brennan'),
 (N'Adeyemi'),(N'Kowalczyk'),(N'Raghavan'),(N'Fletcher'),(N'Villalobos'),(N'Whitfield'),
 (N'Vasquez'),(N'Boateng'),(N'Nowak'),(N'Ferraro'),(N'Okonkwo'),(N'Ellison'),
 (N'Karim'),(N'Duarte'),(N'Nadkarni'),(N'Barlowe'),(N'Aziz'),(N'Varma'),
 (N'Sow'),(N'Testani'),(N'Pierce'),(N'Duval'),(N'Martinez'),(N'Oyelaran'),
 (N'Johnson'),(N'Chan'),(N'Rowe'),(N'Smith'),(N'Hollande'),(N'Bergstrom'),
 (N'Iqbal'),(N'Mwangi'),(N'Petrov'),(N'Silva');
GO

CREATE TABLE #cities (i INT IDENTITY(0,1) PRIMARY KEY, city NVARCHAR(100), st CHAR(2), zip VARCHAR(12));
INSERT INTO #cities (city, st, zip) VALUES
 (N'Austin','TX','78701'),(N'Atlanta','GA','30303'),(N'Seattle','WA','98101'),
 (N'Miami','FL','33130'),(N'Phoenix','AZ','85004'),(N'Denver','CO','80202'),
 (N'Chicago','IL','60601'),(N'Boston','MA','02108'),(N'Raleigh','NC','27601'),
 (N'Portland','OR','97204'),(N'Dallas','TX','75201'),(N'Columbus','OH','43215');
GO

CREATE TABLE #domains (i INT IDENTITY(0,1) PRIMARY KEY, v VARCHAR(60));
INSERT INTO #domains (v) VALUES
 ('gmail.com'),('yahoo.com'),('outlook.com'),('hotmail.com'),('icloud.com'),
 ('aol.com'),('proton.me'),('comcast.net');
GO

CREATE TABLE #employers (i INT IDENTITY(0,1) PRIMARY KEY, v NVARCHAR(200));
INSERT INTO #employers (v) VALUES
 (N'BrightPath Staffing'),(N'Corepoint Logistics'),(N'Vellum Retail Group'),
 (N'Northgate Health'),(N'Halcyon Foods'),(N'Ridgeline Transit'),
 (N'Summit Facilities'),(N'Ardent Care Partners'),(N'Blue Harbor Hospitality'),
 (N'Cascade Manufacturing');
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- SECTION 1 -- THE HAND-BUILT EXHIBITS.
--
-- These 38 rows are the whole identity argument. Each group is annotated with the
-- correct answer and the specific naive rule it defeats. They are written out longhand
-- rather than generated because a reviewer needs to read them.
-- ─────────────────────────────────────────────────────────────────────────────────────
SET IDENTITY_INSERT dbo.customers ON;
GO

-- G01 -- THE INTERESTING CASE. Same person, and "most recent record wins" loses the
-- money. C1041 is older and holds a PAID-OFF advance; C4788 looks fresher. The funded
-- veto must beat updated_at. C4788 also holds a card whose fingerprint duplicates
-- C1041's, which tempts a third mistake (deleting a card row rather than demoting it).
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1041, N'Alicia', N'Moreau', N'alicia.moreau@gmail.com', '+15125550142', '3392',
  '1989-03-14', N'901 Rio Grande St', N'Austin', 'TX', '78701', N'Halcyon Foods',
  'web', '2023-04-11T09:22:14', '2025-11-02T16:04:00', 'P-ALICIA'),
 (4788, N'Alicia', N'Moreau', N'a.moreau+new@gmail.com', '+15125550142', '3392',
  '1989-03-14', N'901 Rio Grande St', N'Austin', 'TX', '78701', N'Halcyon Foods',
  'ios', '2026-05-30T11:41:02', '2026-08-16T08:15:33', 'P-ALICIA');

-- G02a -- UNRESOLVABLE, SAME INSTRUMENT. Both members have a FUNDED advance, so the
-- rule refuses to merge and routes to review with the evidence attached. Statuses are
-- spelled 'FUNDED ' and 'funded': a case-sensitive predicate sees only one funded
-- member and merges a group it should have refused, destroying the evidence.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (777,  N'Marcus', N'Adeyemi', N'marcus.adeyemi@gmail.com', '+14045550190', '8130',
  '1994-07-22', N'55 Peachtree Pl', N'Atlanta', 'GA', '30303', N'Corepoint Logistics',
  'web', '2024-02-09T13:10:00', '2026-07-30T09:00:00', 'P-MARCUS-A'),
 (3512, N'Marcus', N'Adeyemi', N'm.adeyemi88@yahoo.com', '+14045550191', '8130',
  '1994-07-22', N'55 Peachtree Pl', N'Atlanta', 'GA', '30303', N'Corepoint Logistics',
  'android', '2026-01-15T10:05:00', '2026-08-05T14:22:00', 'P-MARCUS-A');

-- G02b -- UNRESOLVABLE, DIFFERENT INSTRUMENTS. Same person by the proof tuple, but two
-- funded advances on DIFFERENT repayment accounts with overlapping funded windows.
-- For a cash-advance lender that is a first-party fraud signal, not a data-quality
-- ticket, and merging it destroys the trace of a possible loss event.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1298, N'Priyanka', N'Raghavan', N'priyanka.r@gmail.com', '+15125550233', '5501',
  '1990-12-01', N'404 Congress Ave', N'Austin', 'TX', '78701', N'Northgate Health',
  'web', '2024-06-01T08:00:00', '2026-08-01T12:00:00', 'P-PRIYANKA'),
 (4903, N'Priyanka', N'Raghavan', N'p.raghavan@outlook.com', '+12145550233', '5501',
  '1990-12-01', N'1200 Elm St', N'Dallas', 'TX', '75201', N'Northgate Health',
  'call_centre', '2026-06-20T15:30:00', '2026-08-06T09:45:00', 'P-PRIYANKA');

-- G03 -- HOUSEHOLD MAILBOX. FOUR suggestive fields agree (email, phone, address,
-- surname) and these are TWO DIFFERENT PEOPLE: a mother born 1971 and her son born
-- 2004. This is the false positive that carries money -- promoting email to proof
-- attaches C2044's paid-off advance to a different human being.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (2044, N'Denise', N'Kowalczyk', N'kowalczyk.home@gmail.com', '+14045550188', '6612',
  '1971-05-09', N'118 Larkspur Ln', N'Atlanta', 'GA', '30303', N'Ardent Care Partners',
  'web', '2022-08-14T10:00:00', '2025-03-02T10:00:00', 'P-DENISE'),
 (2045, N'Tomasz', N'Kowalczyk', N'kowalczyk.home@gmail.com', '+14045550188', '4487',
  '2004-02-17', N'118 Larkspur Ln', N'Atlanta', 'GA', '30303', NULL,
  'ios', '2026-03-19T17:20:00', '2026-03-19T17:20:00', 'P-TOMASZ');

-- G04 -- SHARED OFFICE MAILBOX. Two coworkers behind one accounts-receivable inbox and
-- one switchboard number. Both email-as-proof and phone-as-proof fire here. Note also
-- that a shared employer domain is NOT a test-account signal (see C0088 below).
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (3311, N'Naomi', N'Fletcher', N'ar@brightpath-staffing.com', '+12065550110', '7781',
  '1986-11-23', N'2200 1st Ave', N'Seattle', 'WA', '98101', N'BrightPath Staffing',
  'partner', '2024-09-02T09:00:00', '2026-02-11T09:00:00', 'P-NAOMI'),
 (3312, N'Curtis', N'Vale', N'ar@brightpath-staffing.com', '+12065550110', '2094',
  '1979-04-08', N'2200 1st Ave', N'Seattle', 'WA', '98101', N'BrightPath Staffing',
  'partner', '2024-09-02T09:12:00', '2026-02-11T09:12:00', 'P-CURTIS');

-- G05 -- ROOMMATES. Phone and address agree; NAMES DO NOT. This one exists to kill any
-- rule of the form "two agreeing weak fields is enough to merge", outright.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (912,  N'Ibrahim', N'Sow', N'ibrahim.sow@gmail.com', '+13125550177', '4410',
  '1995-06-30', N'44 Delaney St Apt 3', N'Chicago', 'IL', '60601', N'Summit Facilities',
  'web', '2025-01-20T11:00:00', '2026-04-04T11:00:00', 'P-IBRAHIM'),
 (4650, N'Renata', N'Villalobos', N'renata.v@yahoo.com', '+13125550177', '9963',
  '1992-10-12', N'44 Delaney St Apt 3', N'Chicago', 'IL', '60601', N'Halcyon Foods',
  'android', '2026-07-01T14:00:00', '2026-07-01T14:00:00', 'P-RENATA');

-- G06 -- SR / JR. The highest suggestive agreement in the whole fixture: same first
-- name, same surname, same address, same phone, near-identical email. Different people.
-- Defeats name-similarity thresholds even when confirmed by address, and defeats any
-- fuzzy-email rule. C1503 is funded, so the wrong merge is a money-movement error.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1503, N'Harold', N'Whitfield', N'harold.whitfield@outlook.com', '+19195550123', '2210',
  '1958-08-30', N'7 Cedar Bluff', N'Raleigh', 'NC', '27601', N'Ridgeline Transit',
  'call_centre', '2023-11-05T09:30:00', '2026-06-18T09:30:00', 'P-HAROLD-SR'),
 (4102, N'Harold', N'Whitfield', N'harold.whitfield.jr@outlook.com', '+19195550123', '7745',
  '1986-04-03', N'7 Cedar Bluff', N'Raleigh', 'NC', '27601', N'Cascade Manufacturing',
  'web', '2026-04-22T16:00:00', '2026-04-22T16:00:00', 'P-HAROLD-JR');

-- G07 -- UNPROVABLE: NULL ssn. Same person, but the proof tuple is incomplete because
-- a pre-2019 migration lost the SSN. dob + surname + email agree, which is exactly the
-- combination it is tempting to promote to proof -- counterfactual CF-1 prices that.
-- C0203 is also the only row that is BOTH a duplicate AND a NULL-updated_at legacy row,
-- so a naive design misses it twice.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (203,  N'Elena', N'Vasquez', N'elena.vasquez@yahoo.com', '+16025550144', NULL,
  '1983-09-27', N'88 Roosevelt St', N'Phoenix', 'AZ', '85004', N'Vellum Retail Group',
  'call_centre', '2018-03-12T10:15:00', NULL, 'P-ELENA'),
 (3844, N'Elena', N'Vasquez', N'elena.vasquez@yahoo.com', '+16025550145', '9014',
  '1983-09-27', N'88 Roosevelt St', N'Phoenix', 'AZ', '85004', N'Vellum Retail Group',
  'web', '2025-08-08T13:00:00', '2026-05-19T13:00:00', 'P-ELENA');

-- G08 -- UNPROVABLE: TRANSPOSED DOB. 1987-04-11 vs 1987-11-04, entered at a call
-- centre. ssn + surname + email + phone all agree. A transposition-tolerant date
-- comparator would recover this; it is deliberately not built, and is named in
-- SOLUTION.md as the cheapest available recall win, gated on ssn+surname also matching.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1120, N'Kwame', N'Boateng', N'kwame.boateng@gmail.com', '+17705550166', '3376',
  '1987-04-11', N'19 Ponce Way', N'Atlanta', 'GA', '30303', N'Blue Harbor Hospitality',
  'web', '2024-01-30T08:45:00', '2026-01-05T08:45:00', 'P-KWAME'),
 (2957, N'Kwame', N'Boateng', N'kwame.boateng@gmail.com', '+17705550166', '3376',
  '1987-11-04', N'19 Ponce Way', N'Atlanta', 'GA', '30303', N'Blue Harbor Hospitality',
  'call_centre', '2025-09-14T12:30:00', '2026-02-02T12:30:00', 'P-KWAME');

-- G09 -- UNPROVABLE: SURNAME CHANGE. Nowak -> Nowak-Brennan on marriage. ssn + dob +
-- phone agree; the surname does not, and the emails differ even after dot/plus folding.
-- THIS ROW IS THE MEASURED COST of putting surname in the proof tuple. Counterfactual
-- CF-3 drops surname, recovers this pair, and pays for it with G10 below. Both sides of
-- that trade get printed rather than argued.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (2680, N'Hanna', N'Nowak', N'hanna.nowak@gmail.com', '+16025550101', '1188',
  '1992-01-19', N'250 Camelback Rd', N'Phoenix', 'AZ', '85004', N'Northgate Health',
  'web', '2024-05-17T09:00:00', '2025-12-01T09:00:00', 'P-HANNA'),
 (4471, N'Hanna', N'Nowak-Brennan', N'h.nowak@gmail.com', '+16025550101', '1188',
  '1992-01-19', N'250 Camelback Rd', N'Phoenix', 'AZ', '85004', N'Northgate Health',
  'ios', '2026-06-05T18:00:00', '2026-06-05T18:00:00', 'P-HANNA');

-- G10 -- CHANCE COLLISION. ssn_last4 AND date_of_birth both agree, and these are two
-- unrelated people in different states. The seeded proof that (ssn_last4, dob) alone is
-- not identity. Surname is what blocks it -- which is the counterweight to G09.
-- Measured cost at 5,000 rows: 1 false pair. ESTIMATED at 500,000 customers:
-- 1.25e11 pairs / 1.46e8 buckets ~= 856 false pairs. Labelled as an estimate.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (654,  N'Diego', N'Ferraro', N'diego.ferraro@icloud.com', '+13055550171', '4417',
  '1991-06-02', N'700 Brickell Ave', N'Miami', 'FL', '33130', N'Halcyon Foods',
  'web', '2024-03-03T10:00:00', '2026-03-03T10:00:00', 'P-DIEGO'),
 (3078, N'Amara', N'Okonkwo', N'amara.okonkwo@gmail.com', '+12065550172', '4417',
  '1991-06-02', N'315 Pine St', N'Seattle', 'WA', '98101', N'Cascade Manufacturing',
  'android', '2025-11-11T11:00:00', '2026-04-27T11:00:00', 'P-AMARA');

-- G11 -- UNPROVABLE: NULL ssn + NICKNAME. Robert / Bobby Ellison. The nickname is a
-- red herring: dropping first name from the proof tuple already handles it. The actual
-- blocker is the missing ssn_last4 on C1866. Kept as a teaching row.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1866, N'Robert', N'Ellison', N'r.ellison@comcast.net', '+16175550199', NULL,
  '1978-10-05', N'12 Beacon St', N'Boston', 'MA', '02108', N'Summit Facilities',
  'call_centre', '2018-07-22T14:00:00', NULL, 'P-ROBERT'),
 (4290, N'Bobby', N'Ellison', N'bobby.ellison@gmail.com', '+16175550199', '5528',
  '1978-10-05', N'12 Beacon St', N'Boston', 'MA', '02108', N'Summit Facilities',
  'web', '2026-02-14T10:30:00', '2026-02-14T10:30:00', 'P-ROBERT');

-- G12 -- TRIPLE-LOOKING GROUP WITH A PLACEHOLDER SSN, and the survivorship cost.
--   C0338 + C2411 are one person and DO merge (full proof tuple, no funded member, so
--     the survivor is the freshest: C2411).
--   C4855 has ssn_last4 = '0000' -- a PLACEHOLDER, not a value. Treating it as a value
--     merges a third, different human being into the group.
-- The survivorship cost, deliberately visible: the survivor C2411 carries a MALFORMED
-- 8-digit phone -- the seeded value is '(555) 012-33' -- while the loser C0338 had a
-- valid one. Because field-level coalescing is refused,
-- canonical_customers_with_invalid_contact is a non-zero printed number rather than a
-- hidden repair. That is stated as a cost, not concealed.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (338,  N'Yusuf', N'Karim', N'yusuf.karim@gmail.com', '+13035550155', '7723',
  '1996-11-30', N'1600 Blake St', N'Denver', 'CO', '80202', N'Ridgeline Transit',
  'web', '2024-08-19T09:00:00', '2026-06-01T09:00:00', 'P-YUSUF'),
 (2411, N'Yusuf', N'Karim', N'y.karim@outlook.com', '(555) 012-33', '7723',
  '1996-11-30', N'1600 Blake St', N'Denver', 'CO', '80202', N'Ridgeline Transit',
  'call_centre', '2025-10-02T15:00:00', '2026-08-10T15:00:00', 'P-YUSUF'),
 (4855, N'Yousuf', N'Karim', N'yousuf.k@yahoo.com', '+13035550156', '0000',
  '1996-11-30', N'2 Larimer Sq', N'Denver', 'CO', '80202', N'Halcyon Foods',
  'android', '2026-05-11T12:00:00', '2026-07-15T12:00:00', 'P-YOUSUF');

-- G13 -- DISCOVERED ACROSS RUNS. Three Marisol Duarte rows at initial load; a FOURTH
-- (C5002) arrives in churn day 1 and must be aliased on ingest rather than reaching a
-- mart as a separate customer. Then on churn day 2 C5002 gains a 'funded' advance, so
-- the group acquires a second money-moved member and the map is RE-DERIVED: three
-- aliases automatically revert to canonical and the group goes to review.
--
-- That day-2 twist is the thesis of the whole design. Because the merge is INDIRECTION
-- and never mutation, an unmerge is a map recompute with zero source-data repair. And
-- the asymmetry is deliberate: the resolver may automatically UNmerge (it is withdrawing
-- a claim) and may never automatically merge from the suggestive tier (that would be
-- adding one).
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (21,   N'Marisol', N'Duarte', N'marisol.duarte@gmail.com', '+15035550120', '6640',
  '1985-02-25', N'800 SW 6th Ave', N'Portland', 'OR', '97204', N'Vellum Retail Group',
  'web', '2021-05-04T08:00:00', '2024-09-09T08:00:00', 'P-MARISOL'),
 (1777, N'Marisol', N'Duarte', N'm.duarte@yahoo.com', '+15035550121', '6640',
  '1985-02-25', N'800 SW 6th Ave', N'Portland', 'OR', '97204', N'Vellum Retail Group',
  'call_centre', '2024-04-14T10:00:00', '2026-07-20T10:00:00', 'P-MARISOL'),
 (3260, N'Marisol', N'Duarte', N'marisol.d@outlook.com', '+15035550122', '6640',
  '1985-02-25', N'800 SW 6th Ave', N'Portland', 'OR', '97204', N'Halcyon Foods',
  'android', '2025-12-19T16:00:00', '2026-01-30T16:00:00', 'P-MARISOL');

-- G14 -- GENUINE STAFF MEMBER ON THE INTERNAL DOMAIN. Real person, real paid-off
-- advance, six real transactions: a $250.00 disbursement, a $12.50 fee and FOUR
-- repayments totalling $262.50, netting exactly $0.00. is_test = FALSE.
-- `email LIKE '%@fundo.com'` removes her and all six of those rows from the book,
-- silently. The shipped rule requires internal domain AND a local part matching
-- ^(test|qa|demo|dev|staging|automation)[._-]?[0-9]*$ -- 'priya.n' fails that.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (88,   N'Priya', N'Nadkarni', N'priya.n@fundo.com', '+15125550188', '4471',
  '1993-08-08', N'110 Guadalupe St', N'Austin', 'TX', '78701', N'Fundo LLC',
  'web', '2023-01-16T09:00:00', '2026-05-05T09:00:00', 'P-PRIYA');

-- G15 -- THE TESTERMAN TRAP. A real customer with a FUNDED advance whose surname
-- contains "test". `last_name LIKE '%test%' OR email LIKE '%test%'` deletes a funded
-- advance, and that is what this row is for: it prices the naive filter.
--
-- WHAT IT IS NOT, corrected after checking the code against the comment: it is not the
-- exhibit for the money-outranks-test-data precedence. No SHIPPED rule fires on C0402 --
-- rule A requires the internal domain and he is on gmail.com -- so he never reaches the
-- blocked_by_money branch and never produces a review row. He motivates the precedence;
-- C4973 and C4974 in 02e are what exercise it, being artifact rows with funded advances.
-- The precedence itself still holds and is still the brief's unstated question: a
-- money-moved customer is never auto-excluded as test data, it goes to review instead.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (402,  N'Marcus', N'Testerman', N'marcus.testerman@gmail.com', '+16145550177', '8890',
  '1990-02-19', N'400 High St', N'Columbus', 'OH', '43215', N'Corepoint Logistics',
  'web', '2025-04-08T10:00:00', '2026-08-11T10:00:00', 'P-MARCUS-T');

-- G15b -- MORE NAIVE FALSE POSITIVES. The substring "test" inside a longer real word,
-- and inside two real surnames. Four addresses plus two surnames; with C0402 that is
-- SEVEN real people the naive pattern flags, one of them funded -> precision 0.500.
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 (1188, N'Greta', N'Lindqvist', N'greatest.deals@hotmail.com', '+12065550301', '5512',
  '1988-07-04', N'99 Union St', N'Seattle', 'WA', '98101', N'Vellum Retail Group',
  'web', '2024-10-10T10:00:00', '2026-03-15T10:00:00', 'P-GRETA'),
 (2733, N'Owen', N'Barlowe', N'protest.organizer@riseup.net', '+13035550302', '6603',
  '1996-01-28', N'20 Wynkoop St', N'Denver', 'CO', '80202', N'Summit Facilities',
  'web', '2025-02-02T10:00:00', '2026-04-01T10:00:00', 'P-OWEN'),
 (3560, N'Farida', N'Aziz', N'contest.winner1994@yahoo.com', '+13125550303', '7714',
  '1994-05-16', N'77 Wacker Dr', N'Chicago', 'IL', '60601', N'Ardent Care Partners',
  'ios', '2025-06-21T10:00:00', '2026-05-22T10:00:00', 'P-FARIDA'),
 (4318, N'Neel', N'Varma', N'latest.news@gmail.com', '+16175550304', '8825',
  '1991-11-09', N'5 Newbury St', N'Boston', 'MA', '02108', N'Northgate Health',
  'android', '2026-03-30T10:00:00', '2026-06-30T10:00:00', 'P-NEEL'),
 (1902, N'Dana', N'Tester', N'dana.tester@gmail.com', '+19195550305', '3341',
  '1985-09-02', N'31 Hillsborough St', N'Raleigh', 'NC', '27601', N'Blue Harbor Hospitality',
  'web', '2024-12-01T10:00:00', '2026-02-28T10:00:00', 'P-DANA'),
 (4501, N'Gio', N'Testani', N'gio.testani@icloud.com', '+13055550306', '9902',
  '1997-03-21', N'1 Ocean Dr', N'Miami', 'FL', '33130', N'Halcyon Foods',
  'ios', '2026-04-18T10:00:00', '2026-07-08T10:00:00', 'P-GIO');

-- ─────────────────────────────────────────────────────────────────────────────────────
-- SECTION 2 -- THE 16 TEST ARTIFACTS.
--
-- Chosen so that NO SINGLE SIGNAL covers them all, which is the point: the naive
-- `%test%` pattern catches 7 of them and 7 real people, while the shipped three-rule
-- detector catches all 16 and no real people.
--   Rule A: internal domain AND local part ~ ^(test|qa|demo|dev|staging|automation)...
--   Rule B: a +test / +qa subaddress tag
--   Rule C: canonical artifacts -- ssn_last4 '0000' AND dob 1900-01-01, or a principal
--           of exactly 0.00 or 999999.99
-- ─────────────────────────────────────────────────────────────────────────────────────
INSERT INTO dbo.customers
 (customer_id, first_name, last_name, email, phone, ssn_last4, date_of_birth,
  address_line1, city, state_code, postal_code, employer_name, signup_channel,
  created_at, updated_at, _seed_person_id)
VALUES
 -- 1-5: caught by A, and also by the naive pattern
 (4960, N'Test', N'One',   N'test@fundo.com',     '+15125559001', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:00:00', '2026-01-02T09:00:00', 'SYNTH-TEST-01'),
 (4961, N'Test', N'Two',   N'test1@fundo.com',    '+15125559002', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:01:00', '2026-01-02T09:01:00', 'SYNTH-TEST-02'),
 (4962, N'Test', N'Three', N'test.2@fundo.com',   '+15125559003', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:02:00', '2026-01-02T09:02:00', 'SYNTH-TEST-03'),
 (4963, N'Test', N'Four',  N'test_03@fundo.com',  '+15125559004', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:03:00', '2026-01-02T09:03:00', 'SYNTH-TEST-04'),
 (4964, N'Test', N'Five',  N'test-04@fundo.com',  '+15125559005', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:04:00', '2026-01-02T09:04:00', 'SYNTH-TEST-05'),
 -- 6-11: caught by A, INVISIBLE to the naive %test% pattern
 (4965, N'QA', N'Account',       N'qa@fundo.com',         '+15125559006', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:05:00', '2026-01-02T09:05:00', 'SYNTH-TEST-06'),
 (4966, N'QA', N'Seventeen',     N'qa.17@fundo.com',      '+15125559007', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:06:00', '2026-01-02T09:06:00', 'SYNTH-TEST-07'),
 (4967, N'Demo', N'Account',     N'demo@fundo.com',       '+15125559008', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:07:00', '2026-01-02T09:07:00', 'SYNTH-TEST-08'),
 (4968, N'Dev', N'One',          N'dev1@fundo.com',       '+15125559009', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:08:00', '2026-01-02T09:08:00', 'SYNTH-TEST-09'),
 (4969, N'Staging', N'Account',  N'staging@fundo.com',    '+15125559010', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:09:00', '2026-01-02T09:09:00', 'SYNTH-TEST-10'),
 (4970, N'Automation', N'Rig',   N'automation@fundo.com', '+15125559011', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:10:00', '2026-01-02T09:10:00', 'SYNTH-TEST-11'),
 -- 12-13: caught ONLY by B. The local part is a real employee name, so rule A fails.
 (4971, N'Rebecca', N'Chan', N'rebecca.chan+test@fundo.com', '+15125559012', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:11:00', '2026-01-02T09:11:00', 'SYNTH-TEST-12'),
 (4972, N'Rebecca', N'Chan', N'rebecca.chan+qa2@fundo.com',  '+15125559013', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:12:00', '2026-01-02T09:12:00', 'SYNTH-TEST-13'),
 -- 14-15: caught ONLY by C -- canonical artifact values. Note 4974 is on example.com,
 -- NOT the internal domain, so rule A cannot see it at all.
 (4973, N'Nina', N'Rowe',  N'nina.rowe@fundo.com',  '+15125559014', '0000', '1900-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:13:00', '2026-01-02T09:13:00', 'SYNTH-TEST-14'),
 (4974, N'Carl', N'Smith', N'carl.smith@example.com', '+15125559015', '0000', '1900-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:14:00', '2026-01-02T09:14:00', 'SYNTH-TEST-15'),
 -- 16: uppercase. The naive rule only catches this because SQL Server's default
 -- collation is case-INSENSITIVE -- i.e. its behaviour depends on the collation, which
 -- is not a property anyone checked. The shipped rule normalizes first, deliberately.
 (4975, N'TEST', N'UPPER', N'TEST@FUNDO.COM', '+15125559016', '1234', '1990-01-01',
  N'1 Test St', N'Austin', 'TX', '78701', NULL, 'web', '2026-01-02T09:15:00', '2026-01-02T09:15:00', 'SYNTH-TEST-16');
GO

SET IDENTITY_INSERT dbo.customers OFF;
GO
