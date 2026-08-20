"""Scoring the resolver against ground truth, and pricing every rule that was removed.

THE FAILURE THIS MODULE PREVENTS: a precision figure nobody measured. Every number the
tool prints -- the composition of the fixture, the pairs proposed, the pairs missed, the
cost of each counterfactual -- is counted at run time from the data in front of it. An
earlier draft of the plan asserted 92 rows and 63 pairs, and those two totals did not
reconcile with the groups the fixture actually enumerates. A counted number cannot drift
away from the fixture; a written-down one always does, and it drifts silently.

GROUND TRUTH IS READ DIRECTLY FROM SQL SERVER, AND ONLY HERE. `_seed_person_id` was
never named in `pipeline/tables.yml`, so it reaches no warehouse table at all: THE
RESOLVER PHYSICALLY CANNOT READ THE COLUMN IT IS SCORED AGAINST. That absence is the
only reason the number means anything -- a resolver with access to its own answer key
scores 1.000 by construction and tells you nothing. This module reaches past the
warehouse to the source with `Source.query` to fetch it, which is exactly why that read
lives in the scorer and nowhere else in the pipeline. It is not excluded by a rule that
could have a bug; it is absent because it was never asked for.

And even with that separation it is a SELF-CONSISTENCY CHECK rather than a measurement,
for the reason the tool prints above its own table -- see `HONESTY_PARAGRAPH`. That
paragraph is emitted at runtime rather than filed in a README, because a caveat that
lives next to the number is a caveat that gets applied to it.

SCORED OVER PAIRS, NEVER GROUPS. A group-level score gives partial credit for a
half-merged household and full credit for a three-way group where only two members
belonged together, so it cannot express the failure that actually costs money: ONE wrong
edge attaching a funded advance to a different human being. Pairs count every edge once,
in both directions, which is why precision and recall here are comparable across the
counterfactuals at all.

WHAT THE COUNTERFACTUALS ARE FOR. Each one removes a single shipped rule, re-runs the
resolution, and prints what the removal costs in pairs. Every figure is produced by the
same engine as the baseline, so a difference between two rows is attributable to the
rule and to nothing else. Where a removal turns out to cost NOTHING on this fixture,
that is printed as plainly as a cost -- an unpriced rule is a finding about the fixture,
and hiding it would make the rule look better evidenced than it is.
"""

from __future__ import annotations

import itertools
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..config import Settings
from ..source import Source
from ..warehouse import Warehouse
from . import rules
from .normalize import (
    Normalized,
    Status,
    normalize_dob,
    normalize_email,
    normalize_name,
    normalize_ssn_last4,
)

Pair = tuple[int, int]

# ─────────────────────────────────────────────────────────────────────────────────────
# The paragraph the tool prints above its own table. Contract text, ASCII, verbatim.
#
# It is a constant rather than a docstring because it has to reach the TRANSCRIPT. A
# precision of 1.000 printed on its own is a claim about a lending book; printed under
# this paragraph it is a claim about a fixture, which is the only claim it can support.
# ─────────────────────────────────────────────────────────────────────────────────────
HONESTY_PARAGRAPH = (
    "This is a SELF-CONSISTENCY CHECK, not a measurement. The same author wrote the "
    "fixture and the rules that read it, so a precision of 1.000 says the rules do "
    "what the fixture was built to require -- nothing more. Measuring precision on "
    "Fundo's real book needs a labelled sample of real duplicates, adjudicated by "
    "someone who did not write the rules."
)

# ─────────────────────────────────────────────────────────────────────────────────────
# Why there is no _EXPECTED_MISSES constant
#
# There was one, set to 6, and it was PRINTED next to the derived count as a difference.
# It was wrong three ways at once, and the third is the one that matters:
#
#   1. It omitted G02a and G02b. Those are truth pairs whose group the resolver refuses,
#      so they sit in the map as resolution='review' and land in `shipped.missed`. The
#      derived list on a fresh load is 8, not 6 -- see the comment at `_resolve`.
#   2. It was compared against THREE different populations. `cmd_demo` scores once, at
#      the end, after churn day 2, where the Marisol group is a four-member refusal
#      contributing 6 more missed pairs. The honest count there is 14. One literal
#      cannot be right for a fresh load, day 1 and day 2.
#   3. A constant cannot tell a fixture change from a rule regression. Both move the
#      count, and the difference line reads identically either way -- so the one thing
#      it was supposed to buy, it never bought.
#
# What replaces it is a PARTITION BY CAUSE, derived from the same run. `_miss_reason`
# already classifies every miss into one of five operationally distinct shapes, so the
# report prints the breakdown and asserts only that the parts sum to the whole. That
# check is arithmetic on this run's own output: it cannot go stale, and a reviewer
# reading '6 no-proof-tuple, 2 refused' can see the design in it without being told a
# number to expect. An `unexplained` miss -- proof tuples that match with no refusal
# recorded -- is the actual regression signal, and it is counted separately and loudly.
# ─────────────────────────────────────────────────────────────────────────────────────

# The five shapes of miss, in the order they are tested. `UNEXPLAINED` is the only one
# that indicates a bug rather than a data condition.
_CAUSE_MIRROR_GAP = "in ground truth, absent from the mirror"
_CAUSE_NO_PROOF_TUPLE = "no proof tuple"
_CAUSE_TUPLES_DISAGREE = "proof tuples disagree"
_CAUSE_REFUSED = "refused by the resolver"
_CAUSE_UNEXPLAINED = "UNEXPLAINED -- tuples match, no refusal recorded"

# Print order for the partition line. Ordering it here rather than by count keeps the
# breakdown comparable between runs.
_CAUSE_ORDER = (
    _CAUSE_NO_PROOF_TUPLE,
    _CAUSE_TUPLES_DISAGREE,
    _CAUSE_REFUSED,
    _CAUSE_MIRROR_GAP,
    _CAUSE_UNEXPLAINED,
)

# How many customer ids to name inside one printed cost or note line before truncating.
# The counts are always exact; only the illustrative id list is capped.
_ID_SAMPLE = 8


# ─────────────────────────────────────────────────────────────────────────────────────
# The contract surface
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PairScore:
    """One rule set's score, in pairs.

    `wrong` is the number that matters operationally. `missed` is a review-queue volume;
    `wrong` is a merge that already happened, and in a lending book a wrong merge is not
    recoverable by editing a row -- it has already moved somebody's borrowing history.
    """

    proposed: int
    correct: int
    missed: int
    wrong: int
    precision: float
    recall: float


@dataclass
class ScoreReport:
    truth_groups: int
    truth_rows: int
    truth_pairs: int
    shipped: PairScore
    counterfactuals: list[tuple[str, PairScore, str]]   # (label, score, what it costs)
    misses: list[tuple[str, str, str]]                  # (customer ids, cause, why refused)
    # Findings that are neither a score nor a miss: reconciliations between this module
    # and the resolver, and between the derived miss list and the expectation above. They
    # are carried here rather than printed from inside the computation so that the caller
    # gets the whole report as one value.
    notes: list[str] = field(default_factory=list)
    # Composition, counted rather than asserted. Printed above the table.
    mirror_rows: int = 0
    excluded_test: int = 0
    population: int = 0


# ─────────────────────────────────────────────────────────────────────────────────────
# Rule sets. The shipped one, and one per counterfactual.
#
# A counterfactual is a FLAG on this dataclass rather than a separate hand-written
# scorer, because two implementations of "the same resolution minus one rule" diverge
# for reasons nobody can attribute afterwards. One engine, six configurations: any
# difference between two printed rows comes from the flag that differs and from nothing
# else.
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuleVariant:
    label: str
    # normalize.py's gate: anything not `valid` is never evidence. Removing it takes the
    # placeholders at face value -- ssn '0000'/'1234', dob 1900-01-01, phone
    # '0000000000'. Phone is a SUGGESTS field and never proves, so its placeholders
    # change no merge under this tiering; they only enlarge the suggestive annotations.
    placeholder_gate: bool = True
    include_last_name: bool = True    # G10 Ferraro/Okonkwo is what this component blocks
    include_first_name: bool = False  # G11 Robert/Bobby is what excluding it tolerates
    email_proves: bool = False        # G03 mother/son, G04 coworkers, if promoted
    exclude_test_first: bool = True   # THE ordering invariant
    naive_test_detector: bool = False # '%test%' instead of the three shipped rules


SHIPPED = RuleVariant(label="shipped")


# ─────────────────────────────────────────────────────────────────────────────────────
# Union-find, not a key -> members dict.
#
# Same structure the resolver uses, for the same reason: a manual_merge edge is not
# key-based, so it has to be able to join two proof groups that share no key at all. A
# dict keyed by the proof tuple cannot express that edge, and the failure is silent --
# the human decision is simply never applied.
# ─────────────────────────────────────────────────────────────────────────────────────

class _UnionFind:
    def __init__(self, ids: Iterable[int]) -> None:
        self._parent: dict[int, int] = {i: i for i in ids}

    def __contains__(self, x: int) -> bool:
        return x in self._parent

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while x != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Lowest id wins the root, so a group's identity is stable across runs and a
            # printed group can be compared between two reports.
            self._parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for i in self._parent:
            out.setdefault(self.find(i), []).append(i)
        return {root: sorted(members) for root, members in out.items()}


def _pairs_of(members: Sequence[int]) -> set[Pair]:
    return set(itertools.combinations(sorted(members), 2))


def _cid(customer_id: int) -> str:
    """Render an id the way the fixture and SOLUTION.md cite them: C0402, C1041."""
    return f"C{customer_id:04d}"


def _ids(pairs: Iterable[Pair], limit: int = _ID_SAMPLE) -> str:
    ordered = sorted(pairs)
    shown = ", ".join(f"{_cid(a)}+{_cid(b)}" for a, b in ordered[:limit])
    if len(ordered) > limit:
        shown += f", and {len(ordered) - limit} more"
    return shown or "none"


# ─────────────────────────────────────────────────────────────────────────────────────
# The proof tuple, with each component switchable.
#
# This cannot call `rules.proof_key` for the variants -- a variant IS a different tuple.
# So it builds the tuple from the same normalizer calls, and then PROVES it did not
# drift: for the shipped configuration the result is compared against `rules.proof_key`
# on every row in the mirror, and a single disagreement is reported as a finding. A
# scorer that quietly re-implements the rule it is scoring measures itself.
# ─────────────────────────────────────────────────────────────────────────────────────

def _component(value: Normalized, placeholder_gate: bool) -> str | None:
    if value.is_evidence:
        return value.normalized
    if not placeholder_gate and value.status is Status.PLACEHOLDER and value.raw is not None:
        # The counterfactual takes the placeholder at face value. Only PLACEHOLDER is
        # promoted: `malformed` and `missing` are not values under either rule set, so
        # ' 12' and NULL stay out and the delta is attributable to the gate alone.
        return value.raw.strip()
    return None


def _variant_key(customer: rules.CustomerFields, variant: RuleVariant) -> tuple[str, ...] | None:
    """The proof tuple under `variant`, or None if any required component fails.

    A partial key is not a weaker key, it is a different question -- so any missing
    component refuses the whole tuple, exactly as `rules.proof_key` does.
    """
    parts: list[str] = []

    ssn = _component(normalize_ssn_last4(customer.ssn_last4), variant.placeholder_gate)
    dob = _component(normalize_dob(customer.date_of_birth), variant.placeholder_gate)
    if ssn is None or dob is None:
        return None
    parts.extend((ssn, dob))

    if variant.include_last_name:
        last = _component(normalize_name(customer.last_name), variant.placeholder_gate)
        if last is None:
            return None
        parts.append(last)

    if variant.include_first_name:
        first = _component(normalize_name(customer.first_name), variant.placeholder_gate)
        if first is None:
            return None
        parts.append(first)

    return tuple(parts)


# ─────────────────────────────────────────────────────────────────────────────────────
# The resolution engine
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Proposal:
    """What one rule set proposes, and the population it proposed over."""

    pairs: set[Pair]
    population: frozenset[int]
    flagged_test: frozenset[int]


def _resolve(
    variant: RuleVariant,
    customers: Sequence[rules.CustomerFields],
    advances: Mapping[int, list[rules.AdvanceFields]],
    manual: Sequence[Pair],
) -> _Proposal:
    """Run the real resolution stages under `variant`.

    The stage ORDER is a parameter of this function and not of the caller, which is the
    whole point of CF-5: the swapped order has to be executed, not estimated. Everything
    else -- the classifier, the proof tuple, the money-moved refusal, the manual-merge
    edge -- is the same code in both orders.

    Survivorship is deliberately NOT run here. `choose_survivor` decides WHICH row
    becomes canonical and cannot change which rows are in a group, so it cannot move a
    single pair; running it would only risk raising on a group the refusal step has
    already declined.
    """
    all_ids = frozenset(c.customer_id for c in customers)

    # ── STAGE: test-account classification ──────────────────────────────────────────
    flagged: set[int] = set()
    for c in customers:
        adv = advances.get(c.customer_id, ())
        if variant.naive_test_detector:
            # The counterfactual detector has no money precedence, and that is the
            # exhibit: G15's C0402 Marcus Testerman is funded, and this filter deletes
            # him and his funded advance for having "test" inside a real surname.
            if rules.naive_test_filter(c):
                flagged.add(c.customer_id)
        else:
            verdict = rules.classify_test_account(c, adv)
            if verdict.is_test:
                flagged.add(c.customer_id)

    # Exclusion BEFORE resolution is the shipped order. With it moved after, resolution
    # runs over everybody and the map it produces contains whatever those rows caused --
    # a later exclusion cannot un-merge an edge that has already been written, which is
    # the failure the ordering invariant exists to prevent.
    population = all_ids - frozenset(flagged) if variant.exclude_test_first else all_ids

    # ── STAGE: group by proof key ───────────────────────────────────────────────────
    uf = _UnionFind(population)
    by_key: dict[tuple[str, ...], list[int]] = {}
    for c in customers:
        if c.customer_id not in population:
            continue
        key = _variant_key(c, variant)
        if key is not None:
            by_key.setdefault(key, []).append(c.customer_id)
    for members in by_key.values():
        for other in members[1:]:
            uf.union(members[0], other)

    # ── STAGE: email promoted to PROVES (counterfactual only) ───────────────────────
    if variant.email_proves:
        by_email: dict[str, list[int]] = {}
        for c in customers:
            if c.customer_id not in population:
                continue
            email = normalize_email(c.email)
            if email.is_evidence and email.normalized is not None:
                by_email.setdefault(email.normalized, []).append(c.customer_id)
        for members in by_email.values():
            for other in members[1:]:
                uf.union(members[0], other)

    # ── STAGE: the human decisions, as PROVES-tier edges ────────────────────────────
    for a, b in manual:
        if a in uf and b in uf:
            uf.union(a, b)

    # ── STAGE: refuse where more than one member has moved money ────────────────────
    pairs: set[Pair] = set()
    for members in uf.groups().values():
        if len(members) < 2:
            continue
        moved = [m for m in members if rules.money_moved(advances.get(m, ()))]
        if len(moved) > 1:
            # Two money-moved members is a question for a human, not a merge. Proposing
            # nothing here is what makes G02a/G02b show up as recall misses rather than
            # as precision wins -- the cost of the refusal is visible instead of hidden.
            continue
        pairs |= _pairs_of(members)

    return _Proposal(
        pairs=pairs,
        population=population,
        flagged_test=frozenset(flagged),
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# Ground truth and scoring
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass
class _Outcome:
    score: PairScore
    truth: set[Pair]
    proposed: set[Pair]
    correct: set[Pair]
    wrong: set[Pair]
    missed: set[Pair]
    groups: int
    rows: int
    # The population this outcome was scored OVER, kept because the denominator is part
    # of the result. Two outcomes with identical pair arithmetic are not comparable if
    # one of them was graded over fewer customers, and without this field _delta_text
    # could not tell the difference -- which is how CF-6 came to print that it changed
    # nothing immediately before the cost of what it changed.
    population: frozenset[int]


def _truth_pairs(population: Iterable[int],
                 person_by_customer: Mapping[int, str]) -> tuple[set[Pair], int, int]:
    """All within-`_seed_person_id` pairs among customers that reached resolution.

    The population bound is not a detail. Test-excluded rows are out on BOTH sides: they
    are not in the proposed set and they are not in this denominator either. Leave them
    in the denominator and recall is diluted by rows the pipeline was told to ignore;
    leave them in the numerator and precision is scored against pairs nobody proposed.

    Returns (pairs, duplicate_groups, rows_in_those_groups) so the composition printed
    above the table reconciles with the pair count by construction -- groups, rows and
    pairs all come out of this one grouping instead of three separate counts.
    """
    members: dict[str, list[int]] = {}
    for cid in sorted(population):
        person = person_by_customer.get(cid)
        if person is None:
            continue
        members.setdefault(person, []).append(cid)

    duplicates = [ms for ms in members.values() if len(ms) > 1]
    pairs: set[Pair] = set()
    for ms in duplicates:
        pairs |= _pairs_of(ms)
    return pairs, len(duplicates), sum(len(ms) for ms in duplicates)


def _score(pairs: set[Pair], population: frozenset[int],
           person_by_customer: Mapping[int, str]) -> _Outcome:
    truth, groups, rows = _truth_pairs(population, person_by_customer)
    proposed = {p for p in pairs if p[0] in population and p[1] in population}
    correct = proposed & truth
    wrong = proposed - truth
    missed = truth - proposed
    return _Outcome(
        score=PairScore(
            proposed=len(proposed),
            correct=len(correct),
            missed=len(missed),
            wrong=len(wrong),
            # An empty proposal set has no precision. It is printed as 0.000 rather than
            # 1.000 so that "proposed nothing" can never read as a perfect score.
            precision=(len(correct) / len(proposed)) if proposed else 0.0,
            recall=(len(correct) / len(truth)) if truth else 0.0,
        ),
        truth=truth, proposed=proposed, correct=correct, wrong=wrong, missed=missed,
        groups=groups, rows=rows, population=frozenset(population),
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# Reading the two sides
# ─────────────────────────────────────────────────────────────────────────────────────

_CUSTOMER_COLUMNS = (
    "customer_id", "first_name", "last_name", "email", "phone", "ssn_last4",
    "date_of_birth", "address_line1", "city", "state_code", "postal_code",
    "employer_name", "signup_channel", "created_at", "updated_at",
)
_ADVANCE_COLUMNS = (
    "advance_id", "customer_id", "status", "principal_amount", "funded_at",
    "paid_off_at", "repayment_account_hash",
)
_CARD_COLUMNS = ("card_id", "customer_id", "card_fingerprint", "is_default")


def _table_exists(warehouse: Warehouse, schema: str, name: str) -> bool:
    row = warehouse.connection.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?",
        [schema, name],
    ).fetchone()
    return bool(row and row[0])


def _rows(warehouse: Warehouse, table: str,
          columns: Sequence[str]) -> list[dict[str, Any]]:
    """Read a warehouse table by an EXPLICIT column list.

    Never SELECT * -- for the same reason tables.yml names its columns: a `SELECT *`
    here would start silently reading whatever a later migration adds, including a
    column that is not supposed to be readable.
    """
    select = ", ".join(f'"{c}"' for c in columns)
    out = warehouse.connection.execute(f"SELECT {select} FROM {table}").fetchall()
    return [dict(zip(columns, row)) for row in out]


def _load_truth(source: Source) -> dict[int, str]:
    """Read `_seed_person_id` from SQL Server. The ONLY read of it in the pipeline.

    It is fetched here, from the source, because it is not in the warehouse to fetch --
    `tables.yml` never named it. That is the separation the whole score depends on, so
    this query is deliberately the one place in the codebase that mentions the column.
    """
    rows = source.query(
        "SELECT customer_id, [_seed_person_id] AS person_id FROM dbo.customers"
    )
    return {int(r["customer_id"]): str(r["person_id"]) for r in rows}


def _map_pairs(map_rows: Sequence[Mapping[str, Any]]) -> tuple[set[Pair], frozenset[int],
                                                               frozenset[int]]:
    """The pairs the SHIPPED resolver actually wrote, read back out of meta.customer_map.

    The shipped column of the report is scored from the resolver's own output, not from
    this module's re-run, because the map IS the deliverable -- a second opinion about
    what the map should have said scores the opinion. The re-run exists only to make the
    counterfactual rows comparable, and it is reconciled against this, loudly.
    """
    groups: dict[int, list[int]] = {}
    excluded: set[int] = set()
    population: set[int] = set()
    for row in map_rows:
        cid = int(row["customer_id"])
        resolution = str(row["resolution"])
        if resolution == "excluded_test":
            excluded.add(cid)
            continue
        population.add(cid)
        if resolution == "merged":
            groups.setdefault(int(row["canonical_customer_id"]), []).append(cid)

    pairs: set[Pair] = set()
    for members in groups.values():
        if len(members) > 1:
            pairs |= _pairs_of(members)
    return pairs, frozenset(population), frozenset(excluded)


# ─────────────────────────────────────────────────────────────────────────────────────
# Why a truth pair was not proposed -- derived from the rows, never from a list
# ─────────────────────────────────────────────────────────────────────────────────────

def _key_failures(customer: rules.CustomerFields) -> list[str]:
    """Which components of the proof tuple refused, and why, in the normalizer's words."""
    out: list[str] = []
    for name, value in (
        ("ssn_last4", normalize_ssn_last4(customer.ssn_last4)),
        ("date_of_birth", normalize_dob(customer.date_of_birth)),
        ("last_name", normalize_name(customer.last_name)),
    ):
        if not value.is_evidence:
            detail = f"{name} {value.status.value}"
            if value.defect_class:
                detail += f"/{value.defect_class}"
            out.append(detail)
    return out


def _miss_reason(
    a: int,
    b: int,
    by_id: Mapping[int, rules.CustomerFields],
    cards: Mapping[int, list[rules.CardFields]],
    advances: Mapping[int, list[rules.AdvanceFields]],
    review_by_pair: Mapping[Pair, str],
) -> tuple[str, str]:
    """Why the shipped rules did not propose this true pair. Derived by inspection.

    Returns `(cause, reason)`. The CAUSE is one of the five `_CAUSE_*` constants and is
    what the report partitions on; the REASON is the sentence a reviewer reads, and it
    names the actual values so the finding is actionable without a query.

    The shapes are operationally different, which is the whole reason they are not
    collapsed into "not merged": no proof tuple at all (chase the customer for the
    missing field), two tuples that disagree (a real-world change like a marriage, or a
    keying error), a tuple that matched inside a group the rules refused to merge (a
    human decision is owed), a truth row the mirror never received (a load problem, not
    an identity one), and tuples that match with no refusal recorded -- which is not a
    data condition at all but a bug in the resolver or in this scorer.
    """
    ca, cb = by_id.get(a), by_id.get(b)
    if ca is None or cb is None:
        missing = _cid(a) if ca is None else _cid(b)
        return (_CAUSE_MIRROR_GAP,
                f"{missing} is in ground truth but not in the warehouse mirror")

    ka, kb = rules.proof_key(ca), rules.proof_key(cb)

    if ka is None or kb is None:
        blocked = []
        if ka is None:
            blocked.append(f"{_cid(a)} [{'; '.join(_key_failures(ca)) or 'no component'}]")
        if kb is None:
            blocked.append(f"{_cid(b)} [{'; '.join(_key_failures(cb)) or 'no component'}]")
        cause = _CAUSE_NO_PROOF_TUPLE
        reason = "no proof tuple: " + ", ".join(blocked)
    elif ka != kb:
        differing = [
            f"{name} {getattr(ka, name)!r} vs {getattr(kb, name)!r}"
            for name in ("ssn_last4", "date_of_birth", "last_name")
            if getattr(ka, name) != getattr(kb, name)
        ]
        cause = _CAUSE_TUPLES_DISAGREE
        reason = "proof tuples disagree: " + "; ".join(differing)
    else:
        recorded = review_by_pair.get((a, b))
        if recorded:
            cause = _CAUSE_REFUSED
            reason = f"refused by the resolver: {recorded}"
        else:
            # Fall back to deriving it, so a miss is never reported as unexplained just
            # because meta.merge_review was written under a different grouping.
            moved = [m for m in (a, b) if rules.money_moved(advances.get(m, ()))]
            if len(moved) > 1:
                hashes = {
                    adv.repayment_account_hash
                    for m in moved for adv in advances.get(m, ())
                    if adv.repayment_account_hash
                }
                same = "same" if len(hashes) <= 1 else "different"
                cause = _CAUSE_REFUSED
                reason = (f"refused: proof tuples match but both members have moved money "
                          f"({same} funding instrument)")
            else:
                cause = _CAUSE_UNEXPLAINED
                reason = ("proof tuples match and the group was not refused -- the map "
                          "does not contain this pair; this is a finding, not a rule")

    signals = rules.suggestive_signals(ca, cb, cards.get(a, ()), cards.get(b, ()))
    if signals:
        reason += f" | weak signals agreeing: {', '.join(signals)}"
    return (cause, reason)


# ─────────────────────────────────────────────────────────────────────────────────────
# What each removed rule costs -- the consequence, printed next to the measured delta
#
# The CONSEQUENCE clauses below are statements of principle and carry no numbers. Every
# number in a printed cost line is computed from the run, so the two halves cannot drift
# apart: the clause says what kind of damage the rule prevents, the measured delta says
# how much of it this fixture actually contains.
# ─────────────────────────────────────────────────────────────────────────────────────

_CONSEQUENCE = {
    "email promoted to PROVES": (
        "email identifies a MAILBOX, not a person: a household shares one and an "
        "accounts-receivable inbox is shared by coworkers, so a wrong edge here can "
        "attach one person's paid-off advance to a different human being."
    ),
    "placeholder gate removed": (
        "a placeholder is a value that many unrelated people share, so promoting it to "
        "evidence merges strangers with exactly the confidence used for real matches."
    ),
    "first_name added to the proof tuple": (
        "a person's given name is the field most likely to be a nickname, so requiring "
        "it trades recall for nothing a stronger component does not already provide."
    ),
    "last_name dropped from the proof tuple": (
        "(ssn_last4, date_of_birth) alone is not identity -- surname is the only "
        "component separating a chance collision from a duplicate, and dropping it buys "
        "the marriage-rename case by paying with unrelated people."
    ),
    "test-account exclusion moved to AFTER resolution": (
        "synthetic accounts share the artifact values a form emits, so resolving before "
        "removing them lets those artifacts act as proof; and a later exclusion cannot "
        "un-write a merge the map already contains."
    ),
    "naive '%test%' filter": (
        "'%test%' is a substring of real surnames and real mailbox names, so it deletes "
        "real customers -- including funded ones, which removes real money from the "
        "book with no error anywhere."
    ),
}


def _delta_text(label: str, base: _Outcome, variant: _Outcome) -> str:
    """Measured cost of one removal, relative to the baseline run of the same engine.

    THE POPULATION IS PART OF THE COST, and leaving it out produced a printed
    contradiction. Every variant is scored against a truth denominator bounded by its
    OWN population, so a variant that deletes customers removes its own errors from the
    denominator as well. CF-6's naive '%test%' filter deletes 14 rows, 7 of them real
    people, and all 7 happen to be singletons in truth -- so no PAIR moved, the four
    clauses below all found nothing, and the report said 'changes NOTHING on this
    fixture ... does not price the rule'. The next sentence, from _detector_cost_text,
    then priced it at precision 0.500. The paragraph refuted itself in print.

    A population change is therefore reported as a change, and the 'prices nothing'
    sentence is reachable only when the variant scored the SAME population as the
    baseline. It is an honest sentence when it is true and a contradiction when it is
    not, and the difference is one set comparison.
    """
    introduced = variant.wrong - base.wrong
    fixed = base.wrong - variant.wrong
    recovered = base.missed - variant.missed
    lost = variant.missed - base.missed
    dropped = base.population - variant.population
    added = variant.population - base.population

    parts: list[str] = []
    if dropped:
        parts.append(
            f"scores {len(dropped)} FEWER customer(s) than the baseline "
            f"({', '.join(_cid(i) for i in sorted(dropped)[:_ID_SAMPLE])}"
            f"{f', and {len(dropped) - _ID_SAMPLE} more' if len(dropped) > _ID_SAMPLE else ''})"
            f" -- the denominator moved with them, so its pair figures are not "
            f"comparable to the baseline's"
        )
    if added:
        parts.append(
            f"scores {len(added)} MORE customer(s) than the baseline "
            f"({', '.join(_cid(i) for i in sorted(added)[:_ID_SAMPLE])})"
        )
    if introduced:
        parts.append(f"introduces {len(introduced)} wrong pair(s) ({_ids(introduced)})")
    if lost:
        parts.append(f"loses {len(lost)} true pair(s) ({_ids(lost)})")
    if recovered:
        parts.append(f"recovers {len(recovered)} true pair(s) ({_ids(recovered)})")
    if fixed:
        parts.append(f"removes {len(fixed)} wrong pair(s) ({_ids(fixed)})")
    if not parts:
        parts.append(
            "changes NOTHING on this fixture -- no pair moves in either direction and the "
            "scored population is identical, so this fixture does not price the rule and "
            "the rule's value here rests on argument rather than on measurement"
        )
    return f"{'; '.join(parts)}. {_CONSEQUENCE[label]}"


# ─────────────────────────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────────────────────────

def score_resolution(source: Source, warehouse: Warehouse) -> ScoreReport:
    """Score the resolver's map against ground truth, and price every removed rule."""
    notes: list[str] = []

    for schema, name in (("raw", "customers"), ("meta", "customer_map")):
        if not _table_exists(warehouse, schema, name):
            raise SystemExit(
                f"{schema}.{name} does not exist in the warehouse, so there is nothing "
                f"to score.\n"
                f"  The loader must have run (`python -m pipeline load`) and the identity "
                f"resolution after it.\n"
                f"  This module never creates either table: it would then score a map it "
                f"had just invented."
            )

    # ── the warehouse side: everything the resolver was allowed to see ──────────────
    customers = [rules.CustomerFields.from_row(r)
                 for r in _rows(warehouse, 'raw."customers"', _CUSTOMER_COLUMNS)]
    by_id = {c.customer_id: c for c in customers}

    advances: dict[int, list[rules.AdvanceFields]] = {}
    for row in _rows(warehouse, 'raw."advances"', _ADVANCE_COLUMNS):
        advance = rules.AdvanceFields.from_row(row)
        advances.setdefault(advance.customer_id, []).append(advance)

    cards: dict[int, list[rules.CardFields]] = {}
    for row in _rows(warehouse, 'raw."cards"', _CARD_COLUMNS):
        card = rules.CardFields.from_row(row)
        cards.setdefault(card.customer_id, []).append(card)

    manual: list[Pair] = []
    if _table_exists(warehouse, "meta", "manual_merge"):
        manual = [
            (int(a), int(b)) for a, b in warehouse.connection.execute(
                "SELECT customer_id_a, customer_id_b FROM meta.manual_merge"
            ).fetchall()
        ]
    else:
        notes.append(
            "meta.manual_merge does not exist, so the PROVES-tier human decisions could "
            "not be applied by this scorer. The shipped column is unaffected (it is read "
            "from the map), but every counterfactual is missing those edges."
        )

    map_rows = [
        {"customer_id": r[0], "canonical_customer_id": r[1], "resolution": r[2],
         "run_id": r[3]}
        for r in warehouse.connection.execute(
            "SELECT customer_id, canonical_customer_id, resolution, run_id "
            "FROM meta.customer_map"
        ).fetchall()
    ]
    if not map_rows:
        raise SystemExit(
            "meta.customer_map is empty, so the resolver has not produced a map to "
            "score.\n  Run the resolution step first, then re-run this scorer."
        )
    run_ids = {int(r["run_id"]) for r in map_rows}
    if len(run_ids) > 1:
        notes.append(
            f"meta.customer_map holds {len(run_ids)} run_ids ({sorted(run_ids)}). The map "
            f"is re-derived whole on every run and the previous one is deleted in the same "
            f"transaction, so more than one run_id means a half-old map is being scored."
        )

    # ── the source side: ground truth, and nothing else ────────────────────────────
    person_by_customer = _load_truth(source)

    shipped_pairs, shipped_population, shipped_excluded = _map_pairs(map_rows)

    mirror_ids = frozenset(by_id)
    mapped_ids = shipped_population | shipped_excluded
    if mirror_ids != mapped_ids:
        notes.append(
            f"the map covers {len(mapped_ids):,} customers and the mirror holds "
            f"{len(mirror_ids):,}: {len(mirror_ids - mapped_ids):,} mirror row(s) are "
            f"absent from the map and {len(mapped_ids - mirror_ids):,} mapped row(s) are "
            f"absent from the mirror. The likeliest cause is a load between the "
            f"resolution and this score, which makes the map one run stale."
        )
    truth_only = frozenset(person_by_customer) - mirror_ids
    mirror_only = mirror_ids - frozenset(person_by_customer)
    if mirror_only:
        notes.append(
            f"{len(mirror_only):,} customer(s) in the warehouse mirror have no row in the "
            f"source, so they have no ground truth and are scored as unmatchable: "
            f"{', '.join(_cid(i) for i in sorted(mirror_only)[:_ID_SAMPLE])}."
        )
    if truth_only:
        notes.append(
            f"{len(truth_only):,} customer(s) exist in the source but not in the mirror "
            f"(a load has not run since they were inserted). They are outside the scored "
            f"population on both sides."
        )

    # ── the shipped score: the map the resolver actually wrote ─────────────────────
    shipped = _score(shipped_pairs, shipped_population, person_by_customer)

    # ── the same engine, re-run at the shipped settings, reconciled against the map ─
    # This is what makes the counterfactual rows comparable to the shipped row. If the
    # re-run and the map disagree, every delta below is measured against a baseline the
    # resolver did not produce -- so the disagreement is reported rather than absorbed.
    base = _resolve(SHIPPED, customers, advances, manual)
    base_outcome = _score(base.pairs, base.population, person_by_customer)
    if base.pairs != shipped.proposed or base.population != shipped_population:
        notes.append(
            f"RE-RUN DISAGREES WITH THE MAP. Scoring meta.customer_map gives "
            f"{len(shipped.proposed):,} pair(s) over {len(shipped_population):,} customers; "
            f"re-running the same rules here gives {len(base.pairs):,} pair(s) over "
            f"{len(base.population):,}. {len(base.pairs - shipped.proposed):,} pair(s) are "
            f"only in the re-run and {len(shipped.proposed - base.pairs):,} only in the "
            f"map. Every counterfactual delta below is relative to the re-run, so treat "
            f"the comparison as approximate until this reconciles."
        )

    drift = [c.customer_id for c in customers
             if _variant_key(c, SHIPPED) != _shipped_key_from_rules(c)]
    if drift:
        notes.append(
            f"KEY DRIFT: this module's proof tuple disagrees with rules.proof_key on "
            f"{len(drift):,} row(s) ({', '.join(_cid(i) for i in drift[:_ID_SAMPLE])}). "
            f"The counterfactuals are therefore not variations of the shipped rule and "
            f"their deltas cannot be attributed to the flag that changed."
        )

    # ── the six counterfactuals ────────────────────────────────────────────────────
    counterfactuals: list[tuple[str, PairScore, str]] = []

    for label, variant in (
        # CF-1. G03 (mother/son) and G04 (shared AR inbox) are the exhibits that make
        # email a SUGGESTS field; G07 and G08 are the recall it would buy back.
        ("email promoted to PROVES",
         RuleVariant(label="cf1", email_proves=True)),
        # CF-2. The normalizer's gate, removed. G12's C4855 carries ssn '0000'.
        ("placeholder gate removed",
         RuleVariant(label="cf2", placeholder_gate=False)),
        # CF-3. G11 is Robert/Bobby Ellison.
        ("first_name added to the proof tuple",
         RuleVariant(label="cf3", include_first_name=True)),
        # CF-4. G10 is Ferraro/Okonkwo -- unrelated, same ssn_last4 AND dob. G09 is the
        # Nowak -> Nowak-Brennan marriage this would recover.
        ("last_name dropped from the proof tuple",
         RuleVariant(label="cf4", include_last_name=False)),
        # CF-5. THE ordering invariant, violated. Same stages, swapped.
        ("test-account exclusion moved to AFTER resolution",
         RuleVariant(label="cf5", exclude_test_first=False)),
        # CF-6. The blunt filter, in place of the three shipped rules.
        ("naive '%test%' filter",
         RuleVariant(label="cf6", naive_test_detector=True)),
    ):
        proposal = _resolve(variant, customers, advances, manual)
        outcome = _score(proposal.pairs, proposal.population, person_by_customer)
        cost = _delta_text(label, base_outcome, outcome)

        if label == "test-account exclusion moved to AFTER resolution":
            cost += " " + _ordering_cost_text(customers, advances, manual,
                                              base_outcome, outcome, person_by_customer)
        if label == "naive '%test%' filter":
            cost += " " + _detector_cost_text(proposal, base, person_by_customer)

        counterfactuals.append((label, outcome.score, cost))

    # ── the misses, derived, and partitioned by cause ──────────────────────────────
    review_by_pair = _review_reasons(warehouse)
    misses = []
    for a, b in sorted(shipped.missed):
        cause, reason = _miss_reason(a, b, by_id, cards, advances, review_by_pair)
        misses.append((f"{_cid(a)},{_cid(b)}", cause, reason))

    by_cause = Counter(cause for _, cause, _ in misses)

    # The only assertion made about the miss count, and it is about THIS run's own
    # arithmetic rather than about a number someone expected in advance. A cause outside
    # `_CAUSE_ORDER` would be silently dropped from the printed breakdown, so the
    # breakdown is checked to sum to the list it describes.
    partitioned = sum(by_cause[cause] for cause in _CAUSE_ORDER)
    if partitioned != len(misses):
        raise ValueError(
            f"the miss breakdown covers {partitioned} of {len(misses)} pair(s) -- "
            f"_miss_reason returned a cause that _CAUSE_ORDER does not list: "
            f"{sorted(set(by_cause) - set(_CAUSE_ORDER))}"
        )

    if misses:
        notes.append(
            "the "
            + str(len(misses))
            + " missed pair(s) break down as "
            + "; ".join(
                f"{by_cause[cause]} {cause}"
                for cause in _CAUSE_ORDER if by_cause[cause]
            )
            + ". This breakdown is derived from this run, not compared against an "
              "expected count -- the honest total moves with the population, and after "
              "a churn day it moves a lot: a refused group of n money-moved members "
              "contributes n*(n-1)/2 missed pairs on its own."
        )

    # A miss the scorer cannot explain is the one shape that is not a data condition.
    # It is reported separately from the breakdown so it cannot be read past.
    if by_cause[_CAUSE_UNEXPLAINED]:
        notes.append(
            f"{by_cause[_CAUSE_UNEXPLAINED]} missed pair(s) have MATCHING proof tuples "
            f"and no recorded refusal, so the shipped rules should have merged them and "
            f"did not. Unlike every other line here that is not a fixture property -- it "
            f"is a defect in the resolver or in this scorer, and the pair ids are in the "
            f"list above."
        )

    return ScoreReport(
        truth_groups=shipped.groups,
        truth_rows=shipped.rows,
        truth_pairs=len(shipped.truth),
        shipped=shipped.score,
        counterfactuals=counterfactuals,
        misses=misses,
        notes=notes,
        mirror_rows=len(customers),
        excluded_test=len(shipped_excluded),
        population=len(shipped_population),
    )


def _shipped_key_from_rules(customer: rules.CustomerFields) -> tuple[str, ...] | None:
    """`rules.proof_key` as a plain tuple, for the drift check above."""
    key = rules.proof_key(customer)
    if key is None:
        return None
    return (key.ssn_last4, key.date_of_birth, key.last_name)


def _review_reasons(warehouse: Warehouse) -> dict[Pair, str]:
    """Map every pair inside a merge_review row to that row's recorded reason.

    Read from the resolver's own queue rather than re-derived, so a miss is explained in
    the words a reviewer will actually see in `meta.merge_review`.
    """
    if not _table_exists(warehouse, "meta", "merge_review"):
        return {}
    out: dict[Pair, str] = {}
    for customer_ids, reason in warehouse.connection.execute(
        "SELECT customer_ids, reason FROM meta.merge_review"
    ).fetchall():
        ids: list[int] = []
        for token in str(customer_ids).split(","):
            token = token.strip()
            if token.lstrip("-").isdigit():
                ids.append(int(token))
        for pair in _pairs_of(ids):
            out[pair] = str(reason)
    return out


def _ordering_cost_text(
    customers: Sequence[rules.CustomerFields],
    advances: Mapping[int, list[rules.AdvanceFields]],
    manual: Sequence[Pair],
    base: _Outcome,
    swapped: _Outcome,
    person_by_customer: Mapping[int, str],
) -> str:
    """The extra sentence CF-5 needs, because the ordering is defended TWICE.

    The invariant is real, and on this fixture the stage order alone may cost nothing --
    normalize.py's placeholder gate independently refuses the artifact values the
    synthetic accounts carry, so the merges the wrong order would enable are unreachable
    for a second, unrelated reason. That redundancy is itself the finding: a rule that is
    "correct only because another stage ran first" stops being correct the day someone
    reorders the pipeline, and nothing fails loudly when it does.

    So the swapped order is ALSO run with the gate removed, and the joint cost is
    measured here rather than asserted. Both figures are printed; neither is softened.
    """
    joint = _resolve(
        RuleVariant(label="cf5+cf2", placeholder_gate=False, exclude_test_first=False),
        customers, advances, manual,
    )
    joint_outcome = _score(joint.pairs, joint.population, person_by_customer)
    introduced = joint_outcome.wrong - base.wrong

    if swapped.score.wrong == base.score.wrong:
        lead = (
            "The stage order alone moves nothing here because the placeholder gate in "
            "normalize.py refuses the artifact values independently -- the ordering is "
            "currently protected TWICE, and neither defence is visible from the other."
        )
    else:
        lead = (
            "The stage order alone already costs the pairs above, on top of whatever the "
            "placeholder gate is separately preventing."
        )
    return (
        f"{lead} Removing BOTH defences and running the same swapped order proposes "
        f"{len(introduced)} wrong pair(s) ({_ids(introduced)}) -- precision "
        f"{joint_outcome.score.precision:.3f} against {base.score.precision:.3f} "
        f"shipped. That is the priced version of this invariant on this fixture: it is "
        f"paid jointly, not singly."
    )


def _detector_cost_text(naive: _Proposal, shipped: _Proposal,
                        person_by_customer: Mapping[int, str]) -> str:
    """Flagged, real people caught, and the detector's own precision -- all counted.

    This is a CLASSIFIER score, not a pair score, and it is reported as prose beside the
    pair row rather than squeezed into the PairScore columns, because a "precision" whose
    unit is customers cannot be compared against one whose unit is pairs. Mixing the two
    in one column is how a table starts lying.
    """
    def real_people(flagged: Iterable[int]) -> list[int]:
        # Ground truth marks every synthetic account with a SYNTH- person id, so a
        # flagged customer with any other id is a real person being deleted.
        return sorted(
            cid for cid in flagged
            if not str(person_by_customer.get(cid, "")).startswith("SYNTH-")
        )

    naive_flagged = sorted(naive.flagged_test)
    naive_real = real_people(naive_flagged)
    shipped_flagged = sorted(shipped.flagged_test)
    shipped_real = real_people(shipped_flagged)

    # Counted over the MIRROR, not over the source: a synthetic account the loader has not
    # replicated yet cannot be caught by a detector that reads the warehouse, and putting
    # it in the denominator would report a miss nobody could have made.
    mirror_ids = naive.population | naive.flagged_test
    synthetic_total = sum(
        1 for cid in mirror_ids
        if str(person_by_customer.get(cid, "")).startswith("SYNTH-")
    )
    naive_caught = len(naive_flagged) - len(naive_real)
    shipped_caught = len(shipped_flagged) - len(shipped_real)
    naive_precision = (naive_caught / len(naive_flagged)) if naive_flagged else 0.0
    shipped_precision = (shipped_caught / len(shipped_flagged)) if shipped_flagged else 0.0

    return (
        f"AS A DETECTOR: naive flags {len(naive_flagged)} customer(s), {len(naive_real)} "
        f"of them real people ({', '.join(_cid(i) for i in naive_real[:_ID_SAMPLE])}) -- "
        f"precision {naive_precision:.3f}, catching {naive_caught} of "
        f"{synthetic_total} synthetic accounts. The shipped three rules flag "
        f"{len(shipped_flagged)}, {len(shipped_real)} of them real people -- precision "
        f"{shipped_precision:.3f}, catching {shipped_caught} of {synthetic_total}."
    )


def format_score(report: ScoreReport) -> str:
    """Render the score the way `load.format_run` renders a load: ASCII, fixed columns.

    ASCII only, deliberately. Box-drawing characters crash a default Windows console
    (cp1252), and a reviewer piping `docker compose logs` through one is a real path.
    """
    # 98 leaves the label column wide enough for the longest counterfactual label at its
    # full length. A truncated rule name in a table of rule costs is the one column that
    # must not be abbreviated.
    rule = 54
    width = 98
    lines: list[str] = []

    # The paragraph goes ABOVE the table, always. A number this easy to quote must not be
    # readable without the sentence that bounds what it can support.
    lines.append("  READ THIS FIRST")
    lines.append(textwrap.fill(HONESTY_PARAGRAPH, width=width,
                               initial_indent="  ", subsequent_indent="  "))
    lines.append("")

    lines.append("  POPULATION AND GROUND TRUTH -- counted from this run, none asserted")
    lines.append("  " + "-" * width)
    for label, value in (
        ("customers in the warehouse mirror", report.mirror_rows),
        ("excluded as test accounts before resolution", report.excluded_test),
        ("population that reached resolution", report.population),
        ("duplicate groups in that population (2+ rows, one person)", report.truth_groups),
        ("rows inside those groups", report.truth_rows),
        ("truth pairs (every within-person pair)", report.truth_pairs),
    ):
        lines.append(f"  {label:<62} {value:>8,}")
    lines.append("")

    lines.append("  RESOLUTION SCORE -- PAIRS, not groups")
    lines.append("  " + "-" * width)
    lines.append(f"  {'rule set':<{rule}} {'prop':>6} {'corr':>6} {'wrong':>6} "
                 f"{'miss':>6} {'prec':>7} {'rec':>7}")
    lines.append("  " + "-" * width)

    def row(label: str, s: PairScore) -> str:
        return (f"  {label:<{rule}} {s.proposed:>6,} {s.correct:>6,} {s.wrong:>6,} "
                f"{s.missed:>6,} {s.precision:>7.3f} {s.recall:>7.3f}")

    lines.append(row("SHIPPED (meta.customer_map)", report.shipped))
    for index, (label, score, _) in enumerate(report.counterfactuals, start=1):
        lines.append(row(f"CF-{index} {label}", score))
    lines.append("  " + "-" * width)
    lines.append("")

    lines.append("  WHAT EACH REMOVED RULE COSTS -- one rule removed, re-resolved, re-scored")
    lines.append("  " + "-" * width)
    for index, (label, _, cost) in enumerate(report.counterfactuals, start=1):
        lines.append(f"  CF-{index} {label}")
        lines.append(textwrap.fill(cost, width=width,
                                   initial_indent="       ", subsequent_indent="       "))
        lines.append("")

    lines.append(f"  TRUTH PAIRS THE SHIPPED RULES DID NOT PROPOSE -- {len(report.misses)}, "
                 f"each reason derived from the rows")
    lines.append("  " + "-" * width)
    if not report.misses:
        lines.append("  none")
    for ids, _cause, reason in report.misses:
        # 15 = two leading spaces + the 'Cnnnn,Cnnnn' id column + two more, so a wrapped
        # reason lines up under the first word of the reason rather than under the ids.
        lines.append(textwrap.fill(f"{ids}  {reason}", width=width,
                                   initial_indent="  ", subsequent_indent=" " * 15))

    if report.notes:
        lines.append("")
        lines.append("  FINDINGS -- reported, not suppressed")
        lines.append("  " + "-" * width)
        for note in report.notes:
            lines.append(textwrap.fill(f"* {note}", width=width,
                                       initial_indent="  ", subsequent_indent="    "))

    return "\n".join(lines)


def main() -> int:
    """`python -m pipeline.identity.score`

    Opens both sides because that is the point of the module: the map from the warehouse,
    the answer key from the source, and no path by which the first could have read the
    second.
    """
    settings = Settings.from_env()
    source = Source(settings.source)
    warehouse = Warehouse(settings.warehouse)
    try:
        source.connect()
        warehouse.connect()
        print(format_score(score_resolution(source, warehouse)))
    finally:
        warehouse.close()
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
