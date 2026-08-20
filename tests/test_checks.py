"""Reconciliation-check tests -- plain asserts, no test framework, no database.

Run:  python -m tests.test_checks

The two check modules import nothing but the stdlib and `..config`, so the comparison
logic is testable on a bare interpreter. The two ENGINES are faked below: a snapshot whose
`query` returns canned rows, and a connection whose `execute` returns canned tuples. That
is the whole point of separating compute from render -- the arithmetic that decides a
verdict never touches SQL Server or DuckDB, so it can be tested without either.

WHAT IS DELIBERATELY NOT TESTED HERE: whether the SQL is portable between the two engines.
No fake can answer that, and it is the one part of value_parity that genuinely needs both
databases -- a clean live run is the only evidence that counts. See the design doc.
"""

from __future__ import annotations

from pipeline.checks import key_parity, value_parity
from pipeline.checks.scorecard import (
    FAIL,
    PASS,
    SOURCE_DIRTY,
    CheckOutcome,
    Scorecard,
)
from pipeline.config import TableSpec

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


class FakeSnapshot:
    """Returns canned rows in the order they are asked for."""

    def __init__(self, responses: list[list[dict]]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    def query(self, sql: str, params: object = None) -> list[dict]:
        self.queries.append(sql)
        return self._responses.pop(0) if self._responses else []


class FakeCursor:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def fetchone(self):
        return self._payload

    def fetchall(self):
        return self._payload


class FakeConn:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.queries: list[str] = []

    def execute(self, sql: str) -> FakeCursor:
        self.queries.append(sql)
        return FakeCursor(self._responses.pop(0) if self._responses else None)


TXN = TableSpec(
    name="transactions",
    strategy="high_water",
    primary_key=("transaction_id",),
    columns=("transaction_id", "customer_id", "amount_cents", "posted_at", "direction"),
    watermark_column="transaction_id",
    append_only_asserted=True,
)

ADV = TableSpec(
    name="advances",
    strategy="change_tracking",
    primary_key=("advance_id",),
    columns=("advance_id", "customer_id", "status", "principal_amount"),
)


# ─────────────────────────────────────────────────────────────────────────────────────
# KEY PARITY -- the netting case is the reason this check reports three numbers
# ─────────────────────────────────────────────────────────────────────────────────────

# Source holds keys {1,2,3}, warehouse holds {1,2,4}. Row counts are BOTH 3, so a
# count-only check reports health while two rows are wrong: 3 was lost, 4 is stale.
# SUM differs (6 vs 7), which is what makes the cheap pass notice at all.
snap = FakeSnapshot([
    [{"n": 3, "s": 6, "lo": 1, "hi": 3}],            # source scalars
    [{"transaction_id": 1}, {"transaction_id": 2}, {"transaction_id": 3}],
])
conn = FakeConn([
    (3, 7, 1, 4),                                     # warehouse scalars
    [(1,), (2,), (4,)],
])
netting = key_parity.check_key_parity(snap, conn, TXN)

check("netting: row counts are equal on both sides",
      (netting.source_rows, netting.warehouse_rows), (3, 3))
check("netting: the lost row is reported as missing", netting.missing, (3,))
check("netting: the stale row is reported as extra", netting.extra, (4,))
check("netting: it is NOT ok despite equal counts", netting.ok, False)
check("netting: the case is flagged explicitly",
      netting.counts_agree_but_keys_do_not, True)
check("netting: the key sets were actually read", netting.drilled, True)
check("netting: the describe line says the counts net out",
      "net out" in key_parity.describe(netting), True)

# The cheap path: scalars agree, so the key sets are never read.
snap = FakeSnapshot([[{"n": 250000, "s": 12345678, "lo": 1, "hi": 250000}]])
conn = FakeConn([(250000, 12345678, 1, 250000)])
clean = key_parity.check_key_parity(snap, conn, TXN)
check("clean: verdict is ok", clean.ok, True)
check("clean: matched is the source row count", clean.matched, 250000)
check("clean: the key sets were NOT read -- one query only", len(snap.queries), 1)
check("clean: no drill-down happened", clean.drilled, False)

# Decimal from pymssql must not read as a difference against DuckDB's int.
snap = FakeSnapshot([[{"n": 2, "s": __import__("decimal").Decimal("3"), "lo": 1, "hi": 2}]])
conn = FakeConn([(2, 3, 1, 2)])
check("Decimal SUM from pymssql equals int SUM from DuckDB",
      key_parity.check_key_parity(snap, conn, TXN).ok, True)

# A composite key is refused rather than half-checked.
composite = TableSpec(name="t", strategy="change_tracking",
                      primary_key=("a", "b"), columns=("a", "b"))
try:
    key_parity.check_key_parity(FakeSnapshot([]), FakeConn([]), composite)
    check("composite key raises", False, True)
except ValueError as e:
    check("composite key raises rather than checking only the first column",
          "single-column primary keys" in str(e), True)


# ─────────────────────────────────────────────────────────────────────────────────────
# VALUE PARITY -- the FLOAT omission, and naming the rows behind a failure
# ─────────────────────────────────────────────────────────────────────────────────────

# advances.principal_amount is a FLOAT. It must be omitted, and the omission must carry
# the reason -- an omission a reviewer cannot see is indistinguishable from an oversight.
snap = FakeSnapshot([
    [   # INFORMATION_SCHEMA
        {"COLUMN_NAME": "advance_id", "DATA_TYPE": "int"},
        {"COLUMN_NAME": "customer_id", "DATA_TYPE": "int"},
        {"COLUMN_NAME": "status", "DATA_TYPE": "varchar"},
        {"COLUMN_NAME": "principal_amount", "DATA_TYPE": "float"},
    ],
    [{f"c{i}": v for i, v in enumerate([8000, 100, 1, 8000, 8000, 200, 1, 5000, 8000, 40000])}],
])
conn = FakeConn([(8000, 100, 1, 8000, 8000, 200, 1, 5000, 8000, 40000)])
adv = value_parity.check_value_parity(snap, conn, ADV)

check("FLOAT column is omitted from the comparison",
      [c.column for c in adv.comparisons].count("principal_amount"), 0)
check("the omission names the column", [o[0] for o in adv.omitted], ["principal_amount"])
check("the omission carries the reason",
      "not summable reproducibly" in adv.omitted[0][2], True)
check("the omission reaches the report",
      any("OMITTED" in line and "principal_amount" in line
          for line in value_parity.describe(adv)), True)
check("everything else agreed, so the table is ok", adv.ok, True)

# The break-demo shape: amount_cents SUM is short, and the rows get named.
MONEY = TableSpec(name="transactions", strategy="high_water",
                  primary_key=("transaction_id",),
                  columns=("transaction_id", "amount_cents"),
                  watermark_column="transaction_id")
snap = FakeSnapshot([
    [{"COLUMN_NAME": "transaction_id", "DATA_TYPE": "bigint"},
     {"COLUMN_NAME": "amount_cents", "DATA_TYPE": "bigint"}],
    [{"c0": 3, "c1": 6, "c2": 1, "c3": 3, "c4": 3, "c5": -19500, "c6": -6500, "c7": -6500}],
    [{"transaction_id": 1, "amount_cents": -6500},
     {"transaction_id": 2, "amount_cents": -6500},
     {"transaction_id": 3, "amount_cents": -6500}],
])
conn = FakeConn([
    (3, 6, 1, 3, 3, -55500, -28000, -6500),
    [(1, -6500), (2, -28000), (3, -21000)],
])
broken = value_parity.check_value_parity(snap, conn, MONEY)

check("a money drift is caught", broken.ok, False)
check("the failing column is named",
      sorted({c.column for c in broken.disagreeing}), ["amount_cents"])
check("the drill-down ran because an aggregate disagreed", broken.drilled, True)
check("the differing rows are named", [d.key for d in broken.row_differences], [2, 3])
check("the signed delta is reported",
      [c.delta for c in broken.disagreeing if c.aggregate == "SUM"], [-36000])
check("the report names a row",
      any("transaction 2" in line for line in value_parity.describe(broken)), True)

# The drill-down list is capped while the count stays exact -- same contract as score.py.
many_src = [{"transaction_id": i, "amount_cents": -100} for i in range(1, 41)]
snap = FakeSnapshot([
    [{"COLUMN_NAME": "transaction_id", "DATA_TYPE": "bigint"},
     {"COLUMN_NAME": "amount_cents", "DATA_TYPE": "bigint"}],
    [{"c0": 40, "c1": 820, "c2": 1, "c3": 40, "c4": 40, "c5": -4000, "c6": -100, "c7": -100}],
    many_src,
])
conn = FakeConn([
    (40, 820, 1, 40, 40, -8000, -200, -200),
    [(i, -200) for i in range(1, 41)],
])
capped = value_parity.check_value_parity(snap, conn, MONEY)
lines = value_parity.describe(capped)
check("all 40 differing rows are counted", len(capped.row_differences), 40)
check("the count is stated exactly in the report",
      any("40 row(s) differ" in line for line in lines), True)
check("the printed row list is truncated",
      any("and 32 more" in line for line in lines), True)


# ─────────────────────────────────────────────────────────────────────────────────────
# THE AGGREGATE SHAPES -- regression guards for engine rules a fake cannot enforce
#
# These assert the SQL TEXT rather than a result, which is unusual and deliberate: the
# first live run died on `MIN([is_default])` because SQL Server refuses min/max on `bit`,
# and no fake engine could have caught that -- a fake answers whatever it is handed. The
# tests below encode the engine rules that bit the real thing.
# ─────────────────────────────────────────────────────────────────────────────────────

bool_aggs = value_parity._aggregates("is_default", "bool")
check("bit columns get NO MIN/MAX -- SQL Server refuses both on bit",
      sorted(label for label, _s, _d in bool_aggs), ["COUNT", "SUM"])
check("bit SUM is CAST to BIGINT -- SUM(bit) is refused too",
      all("CAST" in s for label, s, _d in bool_aggs if label == "SUM"), True)

number_aggs = value_parity._aggregates("amount_cents", "number")
check("integer columns keep all four aggregates",
      sorted(label for label, _s, _d in number_aggs), ["COUNT", "MAX", "MIN", "SUM"])

text_aggs = value_parity._aggregates("last_name", "text")
check("text columns get no MIN/MAX -- collation is not comparable across engines",
      sorted(label for label, _s, _d in text_aggs), ["CHARS", "COUNT"])
check("text length counts trailing spaces on the SQL Server side",
      all("+ N'x'" in s for label, s, _d in text_aggs if label == "CHARS"), True)
check("text length counts trailing spaces on the DuckDB side",
      all("|| 'x'" in d for label, _s, d in text_aggs if label == "CHARS"), True)
check("no aggregate anywhere uses COUNT(DISTINCT -- case-insensitive collation",
      any("DISTINCT" in s or "DISTINCT" in d
          for _l, s, d in bool_aggs + number_aggs + text_aggs), False)

check("date columns get COUNT/MIN/MAX and no SUM",
      sorted(label for label, _s, _d in value_parity._aggregates("posted_at", "date")),
      ["COUNT", "MAX", "MIN"])


# ─────────────────────────────────────────────────────────────────────────────────────
# VERDICT PRECEDENCE AND EXIT CODES
# ─────────────────────────────────────────────────────────────────────────────────────

def card(*verdicts: str) -> Scorecard:
    return Scorecard(outcomes=[CheckOutcome(f"c{i}", v, []) for i, v in enumerate(verdicts)])


check("all PASS -> PASS", card(PASS, PASS, PASS).verdict, PASS)
check("PASS + SOURCE-DIRTY -> SOURCE-DIRTY", card(PASS, SOURCE_DIRTY).verdict, SOURCE_DIRTY)
check("SOURCE-DIRTY + FAIL -> FAIL", card(SOURCE_DIRTY, FAIL).verdict, FAIL)
check("FAIL wins from any position", card(FAIL, PASS, SOURCE_DIRTY).verdict, FAIL)
check("an empty scorecard is PASS, not an error", card().verdict, PASS)

check("PASS exits 0", card(PASS).exit_code, 0)
check("SOURCE-DIRTY exits 0 -- the two sides AGREE", card(PASS, SOURCE_DIRTY).exit_code, 0)
check("FAIL exits non-zero", card(SOURCE_DIRTY, FAIL).exit_code, 1)

# The whole reason SOURCE-DIRTY exists: it must never be able to fail the build, because
# the alternative is a hardcoded expected-orphan count -- the stale-constant trap that
# printed "expects 6" against a real 14 in score.py.
check("no combination of PASS and SOURCE-DIRTY can exit non-zero",
      {card(*combo).exit_code
       for combo in ((PASS,), (SOURCE_DIRTY,), (PASS, SOURCE_DIRTY),
                     (SOURCE_DIRTY, SOURCE_DIRTY, PASS))},
      {0})


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_checks: {checks} checks passed")
