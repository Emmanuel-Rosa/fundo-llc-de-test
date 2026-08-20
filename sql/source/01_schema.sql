-- ═══════════════════════════════════════════════════════════════════════════════════
-- Fundo source system -- schema.
--
-- This is a deliberately imperfect schema. Every smell in it is here because it
-- produces a MEASURED consequence somewhere in the run output; none of them are
-- decoration. They are annotated inline and summarised in SOLUTION.md.
--
-- Executed by the pipeline over pymssql, which has no notion of `GO`. The batch
-- separators below are honoured by a splitter in pipeline/source.py so these files
-- stay copy-pasteable into SSMS, Azure Data Studio or sqlcmd unchanged.
-- ═══════════════════════════════════════════════════════════════════════════════════

IF DB_ID('fundo_src') IS NULL
    CREATE DATABASE fundo_src;
GO

-- Required by the loader: the change-tracking read takes its version stamp and its
-- base-table rows inside one SNAPSHOT transaction, so anything committing mid-read is
-- invisible to the read AND above the stamp -- picked up next run rather than lost.
ALTER DATABASE fundo_src SET ALLOW_SNAPSHOT_ISOLATION ON;
GO

USE fundo_src;
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- customers -- small, mutable, HARD-deleted. Capture strategy: Change Tracking.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.customers', 'U') IS NULL
CREATE TABLE dbo.customers (
    customer_id      INT            IDENTITY(1,1) NOT NULL
                                    CONSTRAINT pk_customers PRIMARY KEY CLUSTERED,
    first_name       NVARCHAR(100)  NULL,
    last_name        NVARCHAR(100)  NULL,
    email            NVARCHAR(320)  NULL,   -- no format check, no uniqueness, no index
    phone            VARCHAR(40)    NULL,   -- free text: punctuation, extensions, vanity
    ssn_last4        VARCHAR(10)    NULL,   -- SMELL: should be CHAR(4). Holds '0000', '', ' 12'.
                                            -- The field that looks like the strongest
                                            -- evidence and is contaminated by placeholders.
    date_of_birth    DATE           NULL,
    address_line1    NVARCHAR(200)  NULL,
    city             NVARCHAR(100)  NULL,
    state_code       CHAR(2)        NULL,
    postal_code      VARCHAR(12)    NULL,
    employer_name    NVARCHAR(200)  NULL,
    signup_channel   VARCHAR(20)    NULL,   -- web | ios | android | call_centre | partner
    created_at       DATETIME2(3)   NOT NULL,
    updated_at       DATETIME2(3)   NULL,   -- TRAP: NULL on 60 pre-2019 rows, and not every
                                            -- write path maintains it. A watermark on this
                                            -- column silently never sees those rows again.
    is_deleted       BIT            NOT NULL
                       CONSTRAINT df_customers_is_deleted DEFAULT (0),
                                            -- TRAP: the column exists; the application
                                            -- HARD-deletes. No row is ever set to 1.
                                            -- Trusting it is how you ship a pipeline that
                                            -- has never once seen a delete.
    _seed_person_id  VARCHAR(20)    NULL    -- GROUND TRUTH. Excluded from tables.yml, so it
                                            -- reaches no warehouse table and no resolver can
                                            -- read it. It exists only to score the matching
                                            -- rules after the fact.
);
GO

-- Exists ONLY so the naive-watermark comparison is a fair fight rather than a straw man:
-- the shadow extractor that reads "updated_at > last_run" gets a real index.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_customers_updated_at')
    CREATE INDEX ix_customers_updated_at ON dbo.customers(updated_at);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- advances -- same profile as customers. Capture strategy: Change Tracking.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.advances', 'U') IS NULL
CREATE TABLE dbo.advances (
    advance_id             INT          IDENTITY(1,1) NOT NULL
                                        CONSTRAINT pk_advances PRIMARY KEY CLUSTERED,
    customer_id            INT          NOT NULL
                                        CONSTRAINT fk_advances_customer
                                        REFERENCES dbo.customers(customer_id),

    -- ***** DELIBERATE BAD SCHEMA CHOICE #1: an identifier in unbounded text. *****
    -- Three downstream reports treat this as a natural key. Consequences, all of them
    -- printed by the run rather than asserted here:
    --   (a) VARCHAR(MAX) cannot be indexed -- the 1,700-byte key limit -- so the
    --       "natural key" is structurally unenforceable. There is no UNIQUE to add.
    --   (b) it forces LOB handling on every scan that touches it.
    --   (c) 3 rows carry trailing spaces. SQL Server pads on comparison, DuckDB does
    --       not, so COUNT(DISTINCT) is 8,039 at the source and 8,042 in the warehouse
    --       for byte-identical data. Reported as a finding; value parity reconciles on
    --       advance_id instead, and says why.
    external_advance_id    VARCHAR(MAX) NULL,

    status                 VARCHAR(50)  NULL,   -- free text: 15 raw spellings, 7 meanings.
                                                -- 'funded', 'FUNDED ' and 'Funded' all occur,
                                                -- which is how a case-sensitive funded-veto
                                                -- check loses the veto in 9 of 15 groups.

    -- ***** DELIBERATE BAD SCHEMA CHOICE #2: money in a float. *****
    -- This one DISABLES A CHECK rather than merely being ugly: no exact sum
    -- reconciliation is possible on it, so chk_value_parity omits this column and
    -- prints the reason. Omitting it honestly is a stronger demonstration than
    -- reconciling it with a tolerance and calling that a pass. Should be DECIMAL(12,2).
    principal_amount       FLOAT        NULL,
    fee_amount             FLOAT        NULL,

    funded_at              DATETIME2(3) NULL,
    paid_off_at            DATETIME2(3) NULL,
    repayment_account_hash CHAR(64)     NULL,   -- the funding instrument. Evidence attached to
                                                -- a two-funded merge_review row: the same
                                                -- account on both sides is one person with two
                                                -- accounts; different accounts with
                                                -- overlapping funded windows is a first-party
                                                -- fraud signal, and merging it destroys the
                                                -- evidence of a loss event.
    created_at             DATETIME2(3) NOT NULL,
    updated_at             DATETIME2(3) NULL
);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- transactions -- the large table, 86.8% of all replicated rows and effectively none
-- of the churn. That asymmetry is the whole argument for per-table strategies.
-- Capture strategy: BOUNDED identity high-water mark. No Change Tracking.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.transactions', 'U') IS NULL
CREATE TABLE dbo.transactions (
    transaction_id BIGINT       IDENTITY(1,1) NOT NULL
                                CONSTRAINT pk_transactions PRIMARY KEY CLUSTERED,
    customer_id    INT          NOT NULL,   -- NO FK (legacy). Keeps 250k inserts cheap and
                                            -- permits the orphans the scorecard reports.
    advance_id     INT          NULL,
    direction      VARCHAR(20)  NOT NULL,
    amount_cents   BIGINT       NOT NULL,   -- the ONE money column that reconciles exactly.
                                            -- Contrast advances.principal_amount above.
    currency       CHAR(3)      NOT NULL CONSTRAINT df_txn_ccy DEFAULT ('USD'),
    posted_at      DATETIME2(3) NOT NULL,   -- TRAP: not monotonic with transaction_id. 150 of
                                            -- each day's 2,800 inserts are backdated (5.4%).
                                            -- A posted_at watermark loses the subset landing
                                            -- below the PREVIOUS mark: measured, 129 of the
                                            -- 3,700 rows that arrive after the first load.
    created_at     DATETIME2(3) NOT NULL CONSTRAINT df_txn_created DEFAULT (SYSUTCDATETIME()),
    -- Deliberately NO updated_at. The append-only contract is a schema fact here, not a
    -- comment -- and the break demo violates it in place to prove the strategy is blind
    -- to exactly that, which is the failure a nightly full copy papers over.
    CONSTRAINT ck_txn_direction CHECK
        (direction IN ('disbursement','repayment','fee','refund','adjustment'))
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_transactions_posted_at')
    CREATE INDEX ix_transactions_posted_at ON dbo.transactions(posted_at);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- customer_history -- append-only version table. 18,000 rows growing +1,200/day =
-- 6.67%/day, the fastest-growing table in the source. Same strategy as transactions.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.customer_history', 'U') IS NULL
CREATE TABLE dbo.customer_history (
    history_id     BIGINT        IDENTITY(1,1) NOT NULL
                                 CONSTRAINT pk_customer_history PRIMARY KEY CLUSTERED,
    customer_id    INT           NOT NULL,   -- no FK: 3 rows point at customers purged in 2019
    changed_column VARCHAR(64)   NOT NULL,
    old_value      NVARCHAR(400) NULL,       -- keeps PII copies forever -- email and ssn_last4
                                             -- history. A second reason retention here is a
                                             -- compliance decision, not an engineering one.
    new_value      NVARCHAR(400) NULL,
    changed_at     DATETIME2(3)  NOT NULL,
    changed_by     VARCHAR(128)  NOT NULL    -- 'app' | 'ops_batch' | 'dba_manual'
    -- no update path, no delete path, no updated_at
);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- cards -- mutable, small. Capture strategy: Change Tracking, same as customers and
-- advances. An earlier draft gave this table a ROWVERSION column to demonstrate a
-- third mechanism; that was cut. Three strategies for five tables is the
-- over-engineering the brief warns about, and the argument for rowversion survives as
-- one paragraph in SOLUTION.md at zero storage cost and zero extra code path.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.cards', 'U') IS NULL
CREATE TABLE dbo.cards (
    card_id          INT          IDENTITY(1,1) NOT NULL
                                  CONSTRAINT pk_cards PRIMARY KEY CLUSTERED,
    customer_id      INT          NOT NULL,   -- NO FK, deliberately asymmetric with advances.
                                              -- This asymmetry is WHY a customer can be
                                              -- hard-deleted out from under their cards.
    card_token       VARCHAR(64)  NOT NULL,   -- one token per stored card INSTANCE
    card_fingerprint VARCHAR(64)  NOT NULL,   -- the INSTRUMENT: shared across card_ids and
                                              -- across customers. Suggests, never proves --
                                              -- 2 of the 3 shared fingerprints in the seed
                                              -- belong to people who are NOT the same person.
    brand            VARCHAR(20)  NULL,
    last4            CHAR(4)      NULL,
    exp_month        TINYINT      NULL,
    exp_year         SMALLINT     NULL,
    is_default       BIT          NOT NULL CONSTRAINT df_cards_default DEFAULT (0),
    billing_postal   VARCHAR(12)  NULL,
    created_at       DATETIME2(3) NOT NULL,
    updated_at       DATETIME2(3) NULL
);
GO

-- ─────────────────────────────────────────────────────────────────────────────────────
-- THE ABANDONED SCRATCH TABLE. Not replicated -- and the point is the recorded
-- decision, not the 0.4% of daily bytes it saves.
--
-- No PK, no index, no FK, no constraints, no owner. Its deadness is PROVEN by the
-- evidence the scorecard prints, not inferred from the tmp_ prefix: inferring death
-- from a name is how you drop the thing month-end depends on.
-- ─────────────────────────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.tmp_dedupe_backup_2019', 'U') IS NULL
CREATE TABLE dbo.tmp_dedupe_backup_2019 (
    cust_id     INT          NULL,   -- populated
    email       VARCHAR(200) NULL,   -- populated
    note        VARCHAR(MAX) NULL,   -- populated
    dupe_of     INT          NULL,   -- populated
    reviewed_by VARCHAR(100) NULL,   -- 100% NULL
    reviewed_at VARCHAR(50)  NULL,   -- 100% NULL, and a date typed as text
    batch_id    VARCHAR(50)  NULL,   -- 100% NULL
    score       FLOAT        NULL    -- 100% NULL
);
GO
