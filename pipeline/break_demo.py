"""The break-and-restore demo: prove the capture strategy is blind, and the check is not.

`transactions` is declared append-only in tables.yml and carries no `updated_at` -- that
is a schema fact in 01_schema.sql, not a comment. A high-water mark reads `id > last_id`,
so an UPDATE to a row already below the watermark changes no id, produces no change-feed
entry, and moves no bytes. The loader will report a perfectly clean run over a warehouse
that is now wrong. This is the failure a nightly full copy papers over by accident, and it
is the exact bet the high-water strategy makes on these two tables.

TWO BREAKS, and the second one is the more useful:

  1. THE REWRITE. Two historical amounts altered by two different deltas that do not
     cancel. Caught: value parity names the rows, append-only names the diagnosis
     ("history was rewritten below the watermark").

  2. THE SWAP. The same two rows EXCHANGE their amounts. COUNT, SUM, MIN and MAX over the
     column are identical by construction -- not approximately, provably -- so every
     aggregate-based check passes and the corruption is invisible. Two rows are attributed
     to the wrong transactions and the scorecard says the two sides agree.

The second break is here because append-only and key_parity both DOCUMENT this blind spot
in prose ("two edits that cancel exactly pass this check"), and a documented limitation
that has never been executed is a claim, not a finding. This runs it.

THE ORIGINAL VALUES ARE READ, NEVER HARDCODED. A restore that writes back two constants
typed into a file is a restore that silently corrupts the fixture the day the seed changes
-- and it would corrupt it in the direction that still passes every check, because the
constants came from a passing state. So the values are read first, kept in memory, and the
restore is VERIFIED by re-reading rather than assumed from a rowcount.

ONE SIDE EFFECT IT DOES NOT UNDO: the load between break and check is a real load, so
it appends one row per table to `ops.load_run` -- all zeros, which is the evidence. The
SOURCE is restored byte-for-byte and verified; the run HISTORY grows by one run, and
`pipeline runs` will show it. Rolling that back would mean lying about a load that
happened.

THIS DEMO ASSERTS ITS OWN PREDICTIONS and exits non-zero on a surprise in EITHER
direction: the rewrite going undetected is a hole in the checks, and the swap being
detected means the blind spot documented in two modules is described wrongly. Both are
findings; neither is allowed to pass quietly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checks import format_scorecard, run_scorecard
from .checks.scorecard import FAIL
from .config import Settings
from .load import format_run, run_load
from .source import Source
from .warehouse import Warehouse

# The two deltas, in cents. Chosen to be different, non-cancelling, and of opposite sign:
# same-sign edits would also be caught by MIN or MAX moving, and a check that only ever
# meets inflation is a check whose SUM arm is untested.
_DELTAS = (50_000, -12_345)

# How far into the table to look for two rows with DIFFERENT amounts, which the swap needs
# to be a swap at all. Two distinct values inside twenty consecutive rows is not in doubt
# for this seed; the bound exists so that a fixture where it IS in doubt reports that fact
# instead of "swapping" two identical values and calling the no-op a demonstration.
_SEARCH_ROWS = 20


@dataclass(frozen=True)
class Row:
    transaction_id: int
    amount_cents: int


def _rule(text: str) -> None:
    print()
    print("  " + "-" * 96)
    print(f"  {text}")
    print("  " + "-" * 96)


def _amounts(source: Source, ids: tuple[int, ...]) -> dict[int, int]:
    """Read the live amounts for these ids. Used to set up AND to verify the restore."""
    placeholders = ", ".join(["%s"] * len(ids))
    rows = source.query(
        f"SELECT transaction_id, amount_cents FROM dbo.transactions "
        f"WHERE transaction_id IN ({placeholders})",
        ids,
    )
    return {int(r["transaction_id"]): int(r["amount_cents"]) for r in rows}


def _set_amount(source: Source, transaction_id: int, amount_cents: int) -> int:
    return source.execute(
        "UPDATE dbo.transactions SET amount_cents = %s WHERE transaction_id = %s",
        (amount_cents, transaction_id),
    )


def _pick_rows(source: Source, mark: int) -> tuple[Row, Row] | None:
    """The two oldest rows below the watermark that hold different amounts.

    OLDEST, deliberately. They sit as far below the watermark as it is possible to be,
    which is the point: distance from the watermark buys nothing. A high-water read is
    blind to an in-place edit at id 1 and at id 253,699 equally.
    """
    rows = source.query(
        f"SELECT TOP {_SEARCH_ROWS} transaction_id, amount_cents FROM dbo.transactions "
        f"WHERE transaction_id <= %s ORDER BY transaction_id",
        (mark,),
    )
    if len(rows) < 2:
        return None
    first = Row(int(rows[0]["transaction_id"]), int(rows[0]["amount_cents"]))
    for raw in rows[1:]:
        candidate = Row(int(raw["transaction_id"]), int(raw["amount_cents"]))
        if candidate.amount_cents != first.amount_cents:
            return first, candidate
    return None


def _scorecard(source: Source, warehouse: Warehouse, settings: Settings) -> str:
    card = run_scorecard(source, warehouse, settings.manifest)
    print(format_scorecard(card))
    return card.verdict


def run_break_demo(settings: Settings, source: Source, warehouse: Warehouse) -> int:
    """Break, observe, restore, verify -- twice. Returns the process exit code."""
    mark = warehouse.watermark("transactions")
    if mark is None:
        print("  transactions has no watermark yet, so there is no loaded history to")
        print("  rewrite. Run `python -m pipeline demo` (or at least one load) first.")
        return 2

    picked = _pick_rows(source, mark)
    if picked is None:
        print(f"  could not find two rows with different amounts inside the first")
        print(f"  {_SEARCH_ROWS} ids below the watermark. Refusing to 'swap' identical")
        print(f"  values and call it a demonstration.")
        return 2

    a, b = picked
    ids = (a.transaction_id, b.transaction_id)
    original = {a.transaction_id: a.amount_cents, b.transaction_id: b.amount_cents}
    findings: list[str] = []

    _rule("THE TARGET -- two of the oldest rows in the book, far below the watermark")
    print(f"  transactions watermark            {mark:,}")
    print(f"  transaction {a.transaction_id:<6}  amount_cents  {a.amount_cents:>14,}")
    print(f"  transaction {b.transaction_id:<6}  amount_cents  {b.amount_cents:>14,}")
    print()
    print("  Neither row's id changes in anything below, which is the whole mechanism:")
    print("  the high-water read is `id > last_id`, and these ids are not moving.")

    # ── BREAK 1: the rewrite ────────────────────────────────────────────────────────
    _rule("BREAK 1 of 2 -- REWRITE two historical amounts in place")
    for row, delta in zip((a, b), _DELTAS):
        altered = row.amount_cents + delta
        _set_amount(source, row.transaction_id, altered)
        print(f"  transaction {row.transaction_id:<6} {row.amount_cents:>14,} "
              f"-> {altered:>14,}   ({delta:+,})")
    print(f"\n  net movement in SUM(amount_cents): {sum(_DELTAS):+,} cents")
    print("  The two deltas differ and do not cancel, so the SUM has to move.")

    _rule("THE LOADER RUNS -- and reports a clean run over a warehouse that is now wrong")
    summary = run_load(settings, source, warehouse)
    print(format_run(summary))
    txn = next((t for t in summary.tables if t.table == "transactions"), None)
    read = txn.rows_read if txn else -1
    print(f"\n  transactions rows_read this run: {read:,}")
    if read == 0:
        print("  ZERO. Two rows of money just changed and the loader saw nothing to do,")
        print("  because no id moved. Nothing in the load report can ever show this.")
    else:
        findings.append(
            f"the loader read {read:,} transactions row(s) after an in-place UPDATE; the "
            f"high-water strategy should have been blind to it"
        )
        print(f"  UNEXPECTED: the loader read {read:,} row(s). See the findings below.")

    _rule("THE CHECK RUNS -- this is what the loader cannot tell you")
    verdict_broken = _scorecard(source, warehouse, settings)
    if verdict_broken != FAIL:
        findings.append(
            f"the rewrite was NOT caught: the scorecard returned {verdict_broken} where "
            f"FAIL was required. A hole in the checks, not a quirk of the demo"
        )

    # ── RESTORE 1 ───────────────────────────────────────────────────────────────────
    _rule("RESTORE 1 -- write back the values that were READ, then re-read to prove it")
    for tid, amount in original.items():
        _set_amount(source, tid, amount)
    live = _amounts(source, ids)
    if live == original:
        print(f"  verified by re-reading: {live}")
    else:
        findings.append(f"restore 1 did not verify -- read back {live}, expected {original}")
        print(f"  NOT VERIFIED: read back {live}, expected {original}")

    verdict_restored = _scorecard(source, warehouse, settings)
    if verdict_restored == FAIL:
        findings.append(
            "the scorecard still FAILs after restore 1, so the break was not fully undone"
        )

    # ── BREAK 2: the swap ───────────────────────────────────────────────────────────
    _rule("BREAK 2 of 2 -- SWAP the two amounts. Every aggregate is preserved EXACTLY")
    _set_amount(source, a.transaction_id, b.amount_cents)
    _set_amount(source, b.transaction_id, a.amount_cents)
    print(f"  transaction {a.transaction_id:<6} {a.amount_cents:>14,} -> {b.amount_cents:>14,}")
    print(f"  transaction {b.transaction_id:<6} {b.amount_cents:>14,} -> {a.amount_cents:>14,}")
    print()
    print("  COUNT, SUM, MIN and MAX over amount_cents are unchanged -- by construction,")
    print("  not by luck: the multiset of values is identical. Two rows are now attributed")
    print("  to the wrong transactions and no aggregate can see it.")

    _rule("THE CHECK RUNS AGAIN -- and this time it should NOT catch it")
    verdict_swapped = _scorecard(source, warehouse, settings)
    if verdict_swapped == FAIL:
        findings.append(
            "the SWAP was caught. That contradicts the blind spot documented in "
            "checks/scorecard.py and checks/key_parity.py, so one of those write-ups is "
            "wrong and needs correcting"
        )
        print("\n  The swap was CAUGHT. Better than documented -- and therefore a")
        print("  documentation defect. See the findings below.")
    else:
        print(f"\n  OVERALL {verdict_swapped}, and the corruption is still there. This is the")
        print("  limit of aggregate reconciliation, executed rather than asserted. Catching")
        print("  it needs a row-level comparison, which key_parity and value_parity")
        print("  deliberately run only for a table whose aggregates already disagree --")
        print("  the cost of doing it always is the full copy this pipeline replaced.")

    # ── RESTORE 2 ───────────────────────────────────────────────────────────────────
    _rule("RESTORE 2 -- and verified the same way")
    for tid, amount in original.items():
        _set_amount(source, tid, amount)
    live = _amounts(source, ids)
    if live == original:
        print(f"  verified by re-reading: {live}")
    else:
        findings.append(f"restore 2 did not verify -- read back {live}, expected {original}")
        print(f"  NOT VERIFIED: read back {live}, expected {original}")

    verdict_final = _scorecard(source, warehouse, settings)
    if verdict_final == FAIL:
        findings.append(
            "the scorecard still FAILs after restore 2, so the fixture is left broken"
        )

    # ── the demo grades itself ──────────────────────────────────────────────────────
    _rule("WHAT THIS DEMO PREDICTED, AND WHAT HAPPENED")
    print(f"  loader blind to an in-place UPDATE     {read:,} rows read")
    print(f"  scorecard on the REWRITE               {verdict_broken:<14} (FAIL required)")
    print(f"  scorecard after restore 1              {verdict_restored:<14} (any non-FAIL)")
    print(f"  scorecard on the SWAP                  {verdict_swapped:<14} (non-FAIL expected)")
    print(f"  scorecard after restore 2              {verdict_final:<14} (any non-FAIL)")
    print()
    if findings:
        print(f"  {len(findings)} FINDING(S) -- this demo exits non-zero:")
        for f in findings:
            print(f"    * {f}")
        return 1
    print("  Every prediction held. The source is back to the values it started with,")
    print("  verified by re-reading them, so this command is safe to run repeatedly.")
    return 0
