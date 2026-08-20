"""Probe: is the bounded high-water window actually safe? (It is not.)

THIS IS THE PROBE THAT RETRACTED A CLAIM, kept so the retraction is reproducible rather
than asserted. `pipeline/source.py::high_water_ceiling` used to say:

    "The ceiling is MAX(id) *as this snapshot sees it*. Because the snapshot cannot see
     uncommitted work at all, id 899 is not in this read AND is not below the ceiling --
     it is picked up next run."

That holds only if no HIGHER id is already visible while the lower one is still
uncommitted. This constructs exactly that state and measures it. The sentence above is no
longer in the code; what replaced it is the finding this script produces, and the
scorecard's contiguity check is what ships because of it.

    A: INSERT (gets id N), transaction LEFT OPEN
    B: INSERT (gets id N+1), COMMITTED
    C: opens SNAPSHOT, reads MAX(id)  -> if this is N+1, the ceiling is above an
                                         uncommitted row and the claim is false
    A: COMMITS                        -> id N is now visible, but <= ceiling

A loader that advanced its watermark to C's ceiling reads `id > N+1` next run, so id N is
never read again. Silently.

Runs against the live container. Uses its own throwaway table, never a replicated one --
dbo.wm_probe is absent from tables.yml, which is a default-deny allow-list.
"""
import os
import sys

import pymssql

CONN = dict(
    server=os.environ["FUNDO_MSSQL_HOST"],
    port=int(os.environ.get("FUNDO_MSSQL_PORT", "1433")),
    user=os.environ["FUNDO_MSSQL_USER"],
    password=os.environ["FUNDO_MSSQL_PASSWORD"],
    database=os.environ["FUNDO_MSSQL_DB"],
)


def connect():
    # autocommit=True, mirroring Source.connect. With autocommit=False pymssql opens an
    # implicit transaction on the first statement, and SET TRANSACTION ISOLATION LEVEL
    # SNAPSHOT then arrives after the transaction has already started under READ
    # COMMITTED -- SQL Server error 3951. That is what killed the first run of this probe,
    # and it is also why the pipeline's own snapshot() can work at all.
    return pymssql.connect(**CONN, autocommit=True)


def say(msg=""):
    print(msg, flush=True)


setup = connect()
cur = setup.cursor()
cur.execute("IF OBJECT_ID('dbo.wm_probe','U') IS NOT NULL DROP TABLE dbo.wm_probe")
cur.execute(
    "CREATE TABLE dbo.wm_probe ("
    "  id INT IDENTITY(1,1) PRIMARY KEY,"
    "  note VARCHAR(40) NOT NULL,"
    "  amount_cents BIGINT NOT NULL DEFAULT 0)"
)

# Two rows already committed, so the table looks like an ordinary append-only history.
cur.execute("INSERT INTO dbo.wm_probe(note, amount_cents) VALUES ('history-1', 100)")
cur.execute("INSERT INTO dbo.wm_probe(note, amount_cents) VALUES ('history-2', 100)")
cur.execute("SELECT MAX(id) FROM dbo.wm_probe")
baseline = cur.fetchone()[0]
say(f"baseline: table holds ids 1..{baseline}, all committed")
say()

# ── construct the hazard ────────────────────────────────────────────────────────────
conn_a = connect()
ca = conn_a.cursor()
ca.execute("BEGIN TRANSACTION")
ca.execute("INSERT INTO dbo.wm_probe(note, amount_cents) VALUES ('A-still-open', 50000)")
ca.execute("SELECT CAST(SCOPE_IDENTITY() AS INT)")
id_a = ca.fetchone()[0]
say(f"A: inserted id {id_a}  -- transaction LEFT OPEN, not committed")

conn_b = connect()
cb = conn_b.cursor()
cb.execute("BEGIN TRANSACTION")
cb.execute("INSERT INTO dbo.wm_probe(note, amount_cents) VALUES ('B-committed', 1)")
cb.execute("SELECT CAST(SCOPE_IDENTITY() AS INT)")
id_b = cb.fetchone()[0]
cb.execute("COMMIT TRANSACTION")
say(f"B: inserted id {id_b} and COMMITTED")
say()

# ── what the loader's snapshot sees ─────────────────────────────────────────────────
conn_c = connect()
cc = conn_c.cursor()
cc.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
cc.execute("BEGIN TRANSACTION")
cc.execute("SELECT MAX(id) FROM dbo.wm_probe")
ceiling = cc.fetchone()[0]
cc.execute("SELECT COUNT(*) FROM dbo.wm_probe")
visible = cc.fetchone()[0]
cc.execute("SELECT id FROM dbo.wm_probe ORDER BY id")
visible_ids = [r[0] for r in cc.fetchall()]
cc.execute("COMMIT TRANSACTION")

say("C: opened SNAPSHOT -- this is exactly what high_water_ceiling does")
say(f"   ceiling MAX(id)   = {ceiling}")
say(f"   rows visible      = {visible}   ids {visible_ids}")
say(f"   id {id_a} visible?    = {id_a in visible_ids}")
say()

# ── A commits, after the ceiling was taken ──────────────────────────────────────────
ca.execute("COMMIT TRANSACTION")
say(f"A: COMMITTED. id {id_a} is now visible to everyone.")
say()

verdict_cur = setup.cursor()
verdict_cur.execute("SELECT id, note FROM dbo.wm_probe ORDER BY id")
final = verdict_cur.fetchall()
say(f"final table: {[(r[0], r[1]) for r in final]}")
say()

# ── the verdict ─────────────────────────────────────────────────────────────────────
hole = (id_a not in visible_ids) and (ceiling is not None) and (id_a <= ceiling)

say("=" * 76)
if hole:
    say("RESULT: THE HOLE IS REAL.")
    say(f"  id {id_a} was NOT in the snapshot's read, and {id_a} <= ceiling {ceiling}.")
    say(f"  A loader that advanced its watermark to {ceiling} reads 'id > {ceiling}' next")
    say(f"  run. Row {id_a} is never read again. No error is raised anywhere.")
    verdict_cur.execute(
        f"SELECT amount_cents FROM dbo.wm_probe WHERE id = {id_a}"
    )
    say(f"  Money in the lost row: {verdict_cur.fetchone()[0]} cents.")
    say()
    say("  source.py::high_water_ceiling's docstring is therefore WRONG where it says")
    say("  the uncommitted id 'is not below the ceiling'. It can be, whenever a higher")
    say("  id committed first -- which is the normal case under any concurrency.")
else:
    say("RESULT: NO HOLE OBSERVED in this construction.")
    say(f"  id {id_a} vs ceiling {ceiling}; visible ids {visible_ids}")
    say("  Either SQL Server withheld the higher committed id from the snapshot, or")
    say("  IDENTITY did not allocate in the order assumed. Read the numbers above.")
say("=" * 76)
say()

# ── would a CONTIGUITY check have caught it? ────────────────────────────────────────
# This is the cheap detector: an append-only IDENTITY history should have no gaps below
# the ceiling. count == ceiling only if 1..ceiling are all present.
say("Would a contiguity check have caught it, without any extra bookkeeping?")
say(f"  the snapshot saw {visible} row(s) with a ceiling of {ceiling}")
if ceiling is not None and visible != ceiling:
    say(f"  {ceiling} - {visible} = {ceiling - visible} gap(s) below the ceiling -- so YES:")
    say("  'COUNT(*) != MAX(id)' is enough to know the window is incomplete, at the cost")
    say("  of one extra scalar per table and the assumption that the identity has no")
    say("  legitimate gaps (a rolled-back insert burns an id permanently, so on a real")
    say("  system this is a heuristic that needs a tolerance, not a proof).")
else:
    say("  no gap detectable this way in this construction.")
say()

# ── cleanup ─────────────────────────────────────────────────────────────────────────
for c in (conn_a, conn_b, conn_c):
    try:
        c.close()
    except Exception:
        pass
cur.execute("DROP TABLE dbo.wm_probe")
setup.close()
say("probe table dropped. dbo.wm_probe was never in tables.yml, so nothing replicated.")
sys.exit(0)
