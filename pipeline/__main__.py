"""Entry point. `python -m pipeline <command>`

Commands, in the order the demo uses them:

    test      run every pure test suite -- no database, no network
    build     apply the source schema, seed it, enable change capture
    load      run one incremental load
    resolve   re-derive the identity map from the warehouse's current state
    score     grade the identity map against the seeded ground truth
    check     reconcile the warehouse against the source and set the exit code
    churn N   simulate one day of source activity (N = 1 or 2)
    break     rewrite history below the watermark, prove the checks catch it, restore
    demo      the whole sequence, which is what `docker compose up` runs
    runs      print the ops.load_run history

`test`, `check` and `break` carry a verdict in their EXIT CODE, and they answer three
different questions. `test` asks whether the pure logic is sound. `check` asks whether the
warehouse says what the source says -- non-zero only on FAIL, a replication defect, and
zero on SOURCE-DIRTY, where the two sides agree and what they agree on is bad data the
source really holds. That distinction is what makes `docker compose up
--abort-on-container-failure` abort on defects and not on known dirt. `break` asks a third
thing: not "is the warehouse wrong" but "do the checks behave the way this repo says they
do", and it exits non-zero when one of its own predictions did not hold.

Deliberately a plain argv switch rather than argparse subparsers with shared options: the
surface is a flat list of commands with one positional argument between them, and a
reviewer reading this file should be able to see all of it at once.

The command COUNT is deliberately not stated above. It was wrong twice in one day -- once
when `break` was added and again when `test` was -- which is a small instance of the thing
this repo keeps finding: a number written into prose has no test and drifts silently. The
list is the list.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import SQL_DIR, Settings
from .selftest import run_selftest
from .transcript import transcript

# EVERY DATABASE-BACKED IMPORT IS FUNCTION-LOCAL BELOW, and that is not a style choice.
# `pipeline/source.py` does `import pymssql` at module scope, so importing it here meant
# that on an interpreter without the driver `python -m pipeline` died with
# ModuleNotFoundError instead of printing the help text above -- and `pipeline test` could
# not run suites whose defining property is that they need no database. The two modules
# imported at the top are safe: `config` needs only PyYAML, and `selftest` needs only the
# standard library. What is left is a file whose help and self test run anywhere, and
# whose remaining commands import a driver exactly when they are about to use one.

SEED_SCRIPTS = [
    "01_schema.sql",
    "02a_seed_customers.sql",
    "02b_seed_customers_generated.sql",
    "02c_seed_advances.sql",
    "02d_seed_cards.sql",
    "02e_seed_append_only.sql",
    "03_enable_capture.sql",   # AFTER the seed, always -- see the file's own header
]


def _banner(text: str) -> None:
    print()
    print("=" * 100)
    print(f"  {text}")
    print("=" * 100)


def _emit(results: list[list[dict]]) -> None:
    """Print result sets a SQL script chose to emit, so they land in the transcript."""
    for rs in results:
        for row in rs:
            fields = "  ".join(f"{k}={v}" for k, v in row.items())
            print(f"     {fields}")


def cmd_test() -> int:
    """Every pure suite. Takes no Settings: it opens no connection and reads no env."""
    _banner("SELF TEST -- the deterministic half, before anything expensive")
    return run_selftest()


def cmd_build(settings: Settings) -> None:
    from .source import Source

    _banner("BUILD SOURCE -- schema, seed, then enable change capture")
    source = Source(settings.source)
    # Connect to master: fundo_src does not exist yet on a cold start, and 01_schema.sql
    # is what creates it.
    source.connect(database="master")
    for name in SEED_SCRIPTS:
        print(f"   {name}")
        _emit(source.execute_script(SQL_DIR / "source" / name))
    source.close()


def cmd_load(settings: Settings) -> None:
    from .load import format_run, run_load
    from .source import Source
    from .warehouse import Warehouse

    source = Source(settings.source)
    source.connect()
    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    summary = run_load(settings, source, warehouse)
    print(format_run(summary))
    warehouse.close()
    source.close()


def cmd_resolve(settings: Settings) -> None:
    """Re-derive the identity map. NO SOURCE CONNECTION, deliberately.

    The resolver reads `raw.*` and writes `meta.*` and never touches SQL Server, so the
    map is a function of the mirror alone. That is what makes re-running it free: there is
    no live query whose answer could have moved underneath the previous run, and nothing
    to reconcile between two databases when the map changes.
    """
    from .identity.resolve import format_resolution, resolve_identities
    from .warehouse import Warehouse

    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    summary = resolve_identities(warehouse)
    print(format_resolution(summary))
    warehouse.close()


def cmd_score(settings: Settings) -> None:
    """Grade the map. THE ONLY COMMAND THAT OPENS BOTH CONNECTIONS.

    `_seed_person_id` was never named in tables.yml, so it is not in the warehouse at all
    and the ground truth has to be read straight from SQL Server. That asymmetry is the
    only reason the number means anything: the resolver physically cannot read the column
    it is graded against.
    """
    from .identity.score import format_score, score_resolution
    from .source import Source
    from .warehouse import Warehouse

    source = Source(settings.source)
    source.connect()
    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    report = score_resolution(source, warehouse)
    print(format_score(report))
    warehouse.close()
    source.close()


def cmd_check(settings: Settings) -> int:
    """Reconcile both sides. Returns the process exit code.

    Opens BOTH connections, like cmd_score -- and for the same structural reason. A check
    that reads only the warehouse can confirm the loader is self-consistent and nothing
    more; the question here is whether the warehouse says what the SOURCE says, and that
    cannot be answered from one side.
    """
    from .checks import format_scorecard, run_scorecard
    from .source import Source
    from .warehouse import Warehouse

    source = Source(settings.source)
    source.connect()
    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    card = run_scorecard(source, warehouse, settings.manifest)
    print(format_scorecard(card))
    warehouse.close()
    source.close()
    return card.exit_code


def cmd_churn(settings: Settings, day: str) -> None:
    if day not in ("1", "2"):
        raise SystemExit(f"churn takes day 1 or 2, got {day!r}")
    from .source import Source

    _banner(f"CHURN DAY {day} -- simulating one day of source activity")
    source = Source(settings.source)
    source.connect()
    _emit(source.execute_script(SQL_DIR / "demo" / f"04_churn_day{day}.sql"))
    source.close()


def cmd_break(settings: Settings) -> int:
    """The break-and-restore demo. Returns the process exit code.

    Opens both connections and WRITES to the source, which no other command here does
    outside `build` and `churn`. It restores what it changed and verifies the restore by
    re-reading, so it is safe to run against a warehouse you want to keep -- that property
    is what lets compose advertise it as a one-liner next to the happy path.
    """
    from .break_demo import run_break_demo
    from .source import Source
    from .warehouse import Warehouse

    source = Source(settings.source)
    source.connect()
    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    _banner("BREAK AND RESTORE -- an in-place rewrite of history below the watermark")
    code = run_break_demo(settings, source, warehouse)
    warehouse.close()
    source.close()
    return code


def cmd_runs(settings: Settings) -> None:
    from .warehouse import Warehouse

    warehouse = Warehouse(settings.warehouse)
    warehouse.connect()
    warehouse.create_schema(settings.manifest)
    rows = warehouse.connection.execute("""
        SELECT run_id, table_name, read_mode, rows_read, rows_inserted, rows_updated,
               rows_deleted, payload_bytes, wall_ms
        FROM ops.load_run ORDER BY run_id, table_name
    """).fetchall()
    print(f"  {'run':>3} {'table':<18} {'mode':<15} {'read':>8} {'ins':>8} "
          f"{'upd':>6} {'del':>5} {'KiB':>9} {'ms':>6}")
    print("  " + "-" * 92)
    for r in rows:
        print(f"  {r[0]:>3} {r[1]:<18} {r[2]:<15} {r[3]:>8,} {r[4]:>8,} "
              f"{r[5]:>6,} {r[6]:>5,} {r[7]/1024:>9.1f} {r[8]:>6,}")
    warehouse.close()


def cmd_demo(settings: Settings) -> int:
    """The full demonstration, as ONE sequenced process.

    Sequenced in code rather than as a graph of compose services because compose
    `depends_on` orders STARTUP, not COMPLETION -- an initial-load-then-incremental
    sequence cannot be expressed as a service DAG without a well-disguised race.
    """
    # FIRST, and this ordering is the only reason it is worth running here at all. None of
    # it needs a database and the build that follows takes minutes; failing the cheap
    # deterministic half before spending them is the point of having a cheap half. A demo
    # that built a source for six minutes and then reported that normalize() is broken
    # would be answering the questions in the wrong order.
    if cmd_test() != 0:
        print()
        print("  Stopping before the source build. The pure logic is broken, so every")
        print("  number the rest of this demo would print is unsafe to read.")
        return 1

    cmd_build(settings)

    _banner("RUN 1 -- initial load (snapshot read on the mutable tables)")
    cmd_load(settings)

    # The resolver runs after EVERY load, not once at the end, and that repetition is the
    # demonstration rather than padding. The map is re-derived from current state each
    # time and never patched forward, so the withdrawal only becomes visible by resolving
    # more than once: churn day 1 gives G13 a fourth member, churn day 2 gives that member
    # a funded advance, and the next map un-merges what the previous one merged -- with no
    # source-data repair anywhere. Resolved once at the end, that is invisible.
    _banner("IDENTITY 1 -- resolve the initial population")
    cmd_resolve(settings)

    cmd_churn(settings, "1")
    _banner("RUN 2 -- incremental (change feed + closed high-water window)")
    cmd_load(settings)

    _banner("IDENTITY 2 -- RE-DERIVED from current state, never patched forward")
    cmd_resolve(settings)

    cmd_churn(settings, "2")
    _banner("RUN 3 -- incremental")
    cmd_load(settings)

    _banner("IDENTITY 3 -- G13 now has two money-moved members, so its merge is WITHDRAWN")
    cmd_resolve(settings)

    _banner("RUN 4 -- replay with ZERO churn (idempotency: everything should read 0)")
    cmd_load(settings)

    _banner("IDENTITY 4 -- same input as IDENTITY 3, so the map must come out identical")
    cmd_resolve(settings)

    _banner("LOAD HISTORY -- every number below is a side effect of running, not a claim")
    cmd_runs(settings)

    # Checks run BEFORE the score, deliberately. Every identity number below depends on the
    # mirror being a faithful copy, so "the replication is sound" is established first --
    # a precision figure computed over a warehouse that lost rows is precise about nothing.
    _banner("RECONCILIATION -- does the warehouse say what the source says?")
    check_exit = cmd_check(settings)

    # Scored ONCE, and last. The scorer reads the map that is on disk, so scoring it after
    # every run would grade four different populations and invite the reader to compare
    # figures whose denominators differ.
    _banner("IDENTITY SCORE -- graded against a column the warehouse never replicated")
    cmd_score(settings)

    # The demo's exit code is the scorecard's. It runs mid-sequence but the score still
    # prints, because a reviewer looking at a failure wants both halves in the transcript,
    # not a run that stopped before the interesting part.
    return check_exit


def main(argv: list[str]) -> int:
    """Dispatch, wrapped in a transcript.

    The transcript wraps EVERYTHING including the help text and the unknown-command exit,
    because a reviewer whose run failed on a typo wants that in the file too -- a transcript
    that only records successful runs is a transcript of the runs nobody needed.
    """
    command = argv[1] if len(argv) > 1 else "demo"
    with transcript(command if command.isalnum() else "usage"):
        return _dispatch(command)


def _dispatch(command: str) -> int:

    # BEFORE Settings, deliberately. `Settings.from_env()` requires the source password to
    # be present and raises without it, so constructing it up front made both the self test
    # and the help text refuse to run on a machine with no .env -- neither of which needs a
    # single environment variable.
    if command == "test":
        return cmd_test()
    if command not in ("build", "load", "resolve", "score", "check", "churn",
                       "break", "runs", "demo"):
        print(__doc__)
        return 2

    settings = Settings.from_env()

    if command == "build":
        cmd_build(settings)
    elif command == "load":
        cmd_load(settings)
    elif command == "resolve":
        cmd_resolve(settings)
    elif command == "score":
        cmd_score(settings)
    elif command == "check":
        return cmd_check(settings)
    elif command == "churn":
        cmd_churn(settings, argv[2] if len(argv) > 2 else "1")
    elif command == "break":
        return cmd_break(settings)
    elif command == "runs":
        cmd_runs(settings)
    elif command == "demo":
        return cmd_demo(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
