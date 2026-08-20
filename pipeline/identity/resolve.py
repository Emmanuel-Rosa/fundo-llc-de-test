"""The resolver: re-derive the WHOLE customer map from current state, on every run.

TWO FAILURES THIS MODULE EXISTS TO PREVENT.

THE FIRST IS AN ORDERING FAILURE, and it is the one that raises nothing at all.
Test-account exclusion must run BEFORE grouping. Group first and the 16 synthetic
accounts in `sql/source/02a_seed_customers.sql` -- which carry placeholder ssn values,
one shared date of birth and colliding surnames ('Account' x3, 'One' x2, 'Chan' x2) --
form proof groups with each other and are available to join real ones. Nothing errors.
The only symptom is a precision figure that is quietly wrong, which is the worst kind:
`score.py` re-runs this logic with the two stages swapped and prints what that costs.
So the order is not expressed as a comment here. Stage 3 (`group_and_merge`) accepts only
a `Screening`, and a `Screening` is what stage 2 (`screen_test_accounts`) returns --
reordering the pipeline does not silently mis-measure, it has nothing to pass.

THE SECOND IS MUTATION. A merge here is INDIRECTION and never mutation: no source key
and no foreign key is ever rewritten, and the map is re-derived from scratch rather than
patched incrementally. That is what makes an unmerge free. G13's C5002 gains a funded
advance on churn day 2, the Marisol group acquires a second money-moved member, and three
aliases revert to canonical with zero source-data repair -- one map recompute instead of
a data-repair project.

THE ASYMMETRY, stated because it is a rule rather than an accident: the resolver MAY
automatically UN-merge, because that withdraws a claim it made itself. It may NEVER
automatically merge on the suggestive tier, because that asserts one. G03 -- a mother and
her son behind one household mailbox, four suggestive signals agreeing, the mother
holding a paid-off advance -- is what makes that a money question rather than a
philosophical one. Suggestive signals appear in this module in exactly one place: as
annotation on a review row, where a human reads them.
"""

from __future__ import annotations

import hashlib
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence, TypeVar

import duckdb

from ..warehouse import Warehouse
from .normalize import normalize_email, normalize_phone
from .rules import (
    MONEY_MOVED_MEANINGS,
    AdvanceFields,
    CardFields,
    CustomerFields,
    ProofKey,
    choose_survivor,
    classify_test_account,
    money_moved,
    money_moved_evidence,
    proof_key,
    status_meaning,
    suggestive_signals,
)

# ─────────────────────────────────────────────────────────────────────────────────────
# The resolution vocabulary.
#
# Constants rather than inline literals because these strings are not internal: they are
# VALUES IN THE WAREHOUSE. `meta.customer_map.resolution` is what every downstream
# consumer filters on, so a typo in one of nine literals produces a row that matches no
# filter anywhere and disappears from every mart without failing anything.
# ─────────────────────────────────────────────────────────────────────────────────────
RESOLUTION_SINGLETON = "singleton"
RESOLUTION_MERGED = "merged"
RESOLUTION_REVIEW = "review"
RESOLUTION_EXCLUDED_TEST = "excluded_test"

RULE_PROOF_TUPLE = "proof_tuple"
RULE_MANUAL_MERGE = "manual_merge"

REASON_TEST_PATTERN_WITH_MONEY = "test_pattern_with_money"
REASON_TWO_FUNDED_SAME_INSTRUMENT = "two_funded_same_instrument"
REASON_TWO_FUNDED_DIFFERENT_INSTRUMENT = "two_funded_different_instrument"

_META_TABLES = ("customer_map", "merge_review", "manual_merge", "identity_run")

# Column allow-lists, one per table the resolver reads. Named explicitly for the same
# reason tables.yml names them: `SELECT *` here would make this module's behaviour depend
# on column order in the warehouse DDL, which nothing asserts.
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


# ─────────────────────────────────────────────────────────────────────────────────────
# What the resolver produces
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MapRow:
    """One row of `meta.customer_map`: one customer, and what was decided about them.

    `run_id` and `resolved_at` are deliberately NOT here. They are stamped once, at write
    time, from a single clock read -- a map whose rows carry two timestamps reads as two
    runs, and this map is only ever one.
    """

    customer_id: int
    canonical_customer_id: int
    is_canonical: bool
    resolution: str
    rule: str | None = None
    group_key: str | None = None
    survivor_reason: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class ReviewRow:
    """One refusal, queued for a human.

    A refusal is a claim NOT made, so it needs an identity of its own or a reviewer cannot
    tell "this came back again" from "this is new". See `review_id`.
    """

    group_key: str | None
    customer_ids: tuple[int, ...]
    reason: str
    evidence: str

    @property
    def review_id(self) -> int:
        """A deterministic id for this refusal, from the sorted customer ids alone.

        NOT Python's `hash()`: PYTHONHASHSEED randomizes str/bytes hashing per process, so
        the same refusal would arrive with a new id on every run and the review queue could
        never show a recurring refusal as recurring. blake2b is stdlib and stable across
        processes, machines and versions.

        A group that GAINS a member gets a DIFFERENT id, which is the honest answer: the
        thing being reviewed changed. G13 is exactly that case -- three Marisol rows are
        refused as one group, and once C5002 arrives it is a four-member refusal.

        Truncated to 63 bits because the column is a signed BIGINT.
        """
        payload = ",".join(str(i) for i in sorted(self.customer_ids)).encode("ascii")
        return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big") >> 1

    @property
    def customer_ids_csv(self) -> str:
        """Comma-separated ids, ASCII, machine-readable -- no 'C' prefix in the table."""
        return ",".join(str(i) for i in sorted(self.customer_ids))


@dataclass(frozen=True)
class ManualEdge:
    """One human decision from `meta.manual_merge`, applied as a PROVES-tier edge.

    That table is the OUTPUT PATH of the review queue and the third proves-tier source. It
    starts empty and is read on every run, so a decision a human made once survives every
    re-derivation without anyone re-entering it.
    """

    customer_id_a: int
    customer_id_b: int
    decided_by: str
    decided_at: datetime | None = None
    note: str | None = None


@dataclass(frozen=True)
class Screening:
    """Stage 2's output, and THE ORDERING INVARIANT EXPRESSED AS A TYPE.

    `group_and_merge` takes a `Screening` and nothing else, and `screen_test_accounts` is
    what returns one. The pipeline therefore cannot be reordered by accident: grouping
    first has nothing to be handed. That matters more than it looks, because the wrong
    order does not fail -- it produces a full map, a clean run and a wrong precision.

    `population` is who reached resolution. `excluded` are the test accounts, already
    resolved to themselves and taking no further part. `reviews` are the customers whose
    test pattern was BLOCKED BY MONEY -- on this fixture exactly C4973 and C4974, the two
    canonical-artifact rows carrying `funded` advances of 0.00 and 999999.99. Money-moved
    outranks test-account exclusion, so they stay in the population as normal rows and a
    human is told why.

    NOT C0402 Marcus Testerman, though an earlier version of this docstring said so and the
    seed comment invited it. No shipped rule fires on him at all: rule A needs the internal
    domain and he is on gmail.com, rule B needs a subaddress tag and his local part has no
    '+', rule C needs artifact values and his advance is 325.00. He is the exhibit for the
    NAIVE filter's mistake -- `%test%` against a real surname deletes a funded advance --
    and he is saved by rule A's domain requirement rather than by the money veto. Two
    different lessons that were being told as one.

    The constructor asserts the one property the type is for: nobody excluded is also in
    the population.
    """

    population: tuple[CustomerFields, ...]
    excluded: tuple[MapRow, ...] = ()
    reviews: tuple[ReviewRow, ...] = ()

    def __post_init__(self) -> None:
        overlap = {row.customer_id for row in self.excluded} & {
            c.customer_id for c in self.population
        }
        if overlap:
            raise ValueError(
                f"customer(s) {sorted(overlap)} are both excluded as test data and in the "
                f"resolvable population. Test-account exclusion runs BEFORE grouping; a "
                f"Screening that contains both is the reordered pipeline."
            )


@dataclass(frozen=True)
class Resolution:
    """Stages 3-8's output over one `Screening`: map rows for the population."""

    rows: tuple[MapRow, ...]
    reviews: tuple[ReviewRow, ...]
    groups_formed: int
    manual_decisions_applied: int
    bad_contact_canonical_ids: tuple[int, ...]
    # (field, status, count) over the resolved population, sorted. The brief asks for
    # malformed contacts to be found AND COUNTED; `bad_contact_canonical_ids` answers a
    # different question -- the cost of refusing to coalesce fields -- and conflates
    # email with phone and malformed with missing. This is the breakdown.
    contact_quality: tuple[tuple[str, str, int], ...] = ()
    # Canonical customers holding more than one is_default card AFTER resolution, and the
    # subset that already held two BEFORE any merge. The difference is what resolution
    # caused, which is the only part anyone can act on.
    multi_default_card_ids: tuple[int, ...] = ()
    multi_default_premerge_ids: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class ResolutionSummary:
    """One resolver run. Every count in `meta.identity_run` comes from here.

    The counts are DERIVED from the rows rather than accumulated alongside them, so they
    cannot disagree with the map they describe. `_assert_reconciles` then checks that the
    derived numbers add up, and raises if they do not -- an arithmetic mismatch here is a
    bug in this module, not a data condition to report.

    Deriving every count from one list has a limit worth naming: the counts agree with the
    rows unconditionally, INCLUDING when there are no rows, so none of them can detect a
    map that is missing customers. That is why `_assert_reconciles` also takes the mirror's
    row count -- the only input to the check that this dataclass did not produce.
    """

    run_id: int
    resolved_at: datetime
    rows: list[MapRow] = field(default_factory=list)
    reviews: list[ReviewRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    groups_formed: int = 0
    manual_decisions_applied: int = 0
    bad_contact_canonical_ids: list[int] = field(default_factory=list)
    contact_quality: list[tuple[str, str, int]] = field(default_factory=list)
    multi_default_card_ids: list[int] = field(default_factory=list)
    multi_default_premerge_ids: list[int] = field(default_factory=list)
    wall_ms: int = 0

    @property
    def customers_in(self) -> int:
        return len(self.rows)

    @property
    def excluded_test(self) -> int:
        return sum(1 for r in self.rows if r.resolution == RESOLUTION_EXCLUDED_TEST)

    @property
    def rows_merged(self) -> int:
        """Rows folded away into another customer -- the aliases, not the survivors."""
        return sum(
            1 for r in self.rows if r.resolution == RESOLUTION_MERGED and not r.is_canonical
        )

    @property
    def canonical_out(self) -> int:
        """Canonical customers the marts should see. Test accounts are not among them."""
        return sum(
            1 for r in self.rows
            if r.is_canonical and r.resolution != RESOLUTION_EXCLUDED_TEST
        )

    @property
    def review_row_count(self) -> int:
        return len(self.reviews)

    @property
    def canonical_with_bad_contact(self) -> int:
        return len(self.bad_contact_canonical_ids)

    @property
    def multi_default_cards(self) -> int:
        return len(self.multi_default_card_ids)

    @property
    def multi_default_caused_by_merge(self) -> int:
        return len(self.multi_default_card_ids) - len(self.multi_default_premerge_ids)


# ─────────────────────────────────────────────────────────────────────────────────────
# Union-find
# ─────────────────────────────────────────────────────────────────────────────────────

class _UnionFind:
    """Disjoint sets over customer ids, each rooted at its LOWEST id.

    A plain key -> members dict cannot express this problem, and that is why this is here
    rather than a `defaultdict(list)`. A `manual_merge` edge is not key-based: it can join
    two customers with different proof keys, or with no proof key at all, and after that
    edge two former groups are ONE group with ONE survivor. Bucketing by key and then
    reconciling the buckets by hand is this algorithm, reimplemented, with a chance to get
    it wrong -- and the case where it goes wrong is a group that then holds two money-moved
    members and must be refused.

    Rooted at the lowest id rather than by rank. That gives up the rank heuristic's
    amortized guarantee and buys a root that is the same on every machine and every run,
    which is what lets a reviewer diff two runs at all. Path compression is kept, and it
    is iterative: a resolver that recurses has a depth limit on a real book.
    """

    def __init__(self) -> None:
        self._parent: dict[int, int] = {}

    def add(self, item: int) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: int) -> int:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        low, high = (root_a, root_b) if root_a < root_b else (root_b, root_a)
        self._parent[high] = low

    def groups(self) -> dict[int, list[int]]:
        """Root -> members, both the roots and the members in ascending id order."""
        out: dict[int, list[int]] = {}
        for item in sorted(self._parent):
            out.setdefault(self.find(item), []).append(item)
        return out


# ─────────────────────────────────────────────────────────────────────────────────────
# STAGE 2 -- test-account exclusion. FIRST. Always.
# ─────────────────────────────────────────────────────────────────────────────────────

def screen_test_accounts(
    customers: Sequence[CustomerFields],
    advances_by_customer: Mapping[int, Sequence[AdvanceFields]],
) -> Screening:
    """Partition the customers before anything looks at a proof key.

    Three outcomes, and the third is the one the brief leaves unstated:

      is_test                -> `excluded_test`, canonical to itself, out of resolution.
      blocked_by_money       -> STAYS in the population as a normal row, plus a review row.
                                Money-moved outranks test-account exclusion. The exhibits
                                are C4973 and C4974: artifact ssn/dob rows whose advances
                                are nonetheless `funded`, so excluding them would delete a
                                funded advance. Measured, 2 rows every run.
      neither                -> population.

    ONE COMBINATION IS UNEXERCISED and is written down rather than implied: no fixture row
    fires rule A or rule B *and* has moved money. Only rule C reaches the veto here. The
    veto is not gated on which rule fired -- it reads `if rules and money_moved(...)` -- so
    the other two paths are defensive rather than measured, the same way rule A's local-part
    anchoring is.

    Pure: no database, no clock, so it can be re-run over any population. `score.py`
    deliberately does NOT call it: score.py imports only `rules` and `normalize` and
    re-implements both stages inline, which is what lets it run the two orders against each
    other without this module's ordering guarantee getting in the way.
    """
    population: list[CustomerFields] = []
    excluded: list[MapRow] = []
    reviews: list[ReviewRow] = []

    for customer in sorted(customers, key=lambda c: c.customer_id):
        advances = tuple(advances_by_customer.get(customer.customer_id, ()))
        verdict = classify_test_account(customer, advances)

        if verdict.is_test:
            excluded.append(
                MapRow(
                    customer_id=customer.customer_id,
                    canonical_customer_id=customer.customer_id,
                    is_canonical=True,
                    resolution=RESOLUTION_EXCLUDED_TEST,
                    evidence=f"rule {'+'.join(verdict.rules)}: {verdict.detail}",
                )
            )
            continue

        if verdict.blocked_by_money:
            # The money evidence is appended only where the classifier's own detail does
            # not already quote it. A review row that prints the same 64-character
            # instrument hash twice is a review row a human learns to skim, and a skimmed
            # queue is the same outcome as no queue.
            extra = [
                line for line in money_moved_evidence(advances)
                if line not in verdict.detail
            ]
            reviews.append(
                ReviewRow(
                    group_key=None,
                    customer_ids=(customer.customer_id,),
                    reason=REASON_TEST_PATTERN_WITH_MONEY,
                    evidence=(
                        f"test pattern rule {'+'.join(verdict.rules)} fired, but money has "
                        f"moved and money OUTRANKS test-account exclusion: this customer "
                        f"stays in the population and a human decides. Excluding them "
                        f"removes real money from the book, silently. "
                        f"Detail: {verdict.detail}"
                        + (" " + "; ".join(extra) if extra else "")
                    ),
                )
            )

        population.append(customer)

    return Screening(
        population=tuple(population), excluded=tuple(excluded), reviews=tuple(reviews)
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# STAGES 3-8 -- group, apply human decisions, refuse, merge, count the cost
# ─────────────────────────────────────────────────────────────────────────────────────

def group_and_merge(
    screening: Screening,
    advances_by_customer: Mapping[int, Sequence[AdvanceFields]],
    cards_by_customer: Mapping[int, Sequence[CardFields]],
    manual_edges: Sequence[ManualEdge] = (),
) -> Resolution:
    """Group the screened population, refuse what cannot be proved, merge the rest.

    Takes a `Screening` and nothing else, which is the whole point -- see that class.

    The stage order inside is load-bearing too. Manual edges are unioned BEFORE the
    two-money-moved refusal, because a human edge can join two groups that each hold one
    money-moved member; checking first and unioning after would merge exactly the group
    the refusal exists for.

    Pure: no database, no clock.
    """
    population = tuple(sorted(screening.population, key=lambda c: c.customer_id))
    by_id = {c.customer_id: c for c in population}
    keys: dict[int, ProofKey | None] = {c.customer_id: proof_key(c) for c in population}

    rows: list[MapRow] = []
    reviews: list[ReviewRow] = []
    notes: list[str] = []

    # ── STAGE 3 -- group by the proof tuple ─────────────────────────────────────────
    uf = _UnionFind()
    buckets: dict[ProofKey, list[int]] = {}
    for customer in population:
        uf.add(customer.customer_id)
        key = keys[customer.customer_id]
        if key is None:
            # A partial key is not a weaker key, it is a different question -- so a
            # customer with no key is a group of one, never a bucket of NULLs. G07 and
            # G11 (migration lost the ssn) land here, on purpose, and score.py reports
            # them as refusals rather than as recall this design pretends to have.
            continue
        buckets.setdefault(key, []).append(customer.customer_id)
    for members in buckets.values():
        for other in members[1:]:
            uf.union(members[0], other)

    # ── STAGE 4 -- apply meta.manual_merge as a PROVES-tier edge ─────────────────────
    excluded_ids = {row.customer_id for row in screening.excluded}
    manual_members: set[int] = set()
    manual_applied = 0
    for edge in sorted(manual_edges, key=lambda e: (e.customer_id_a, e.customer_id_b)):
        pair = (edge.customer_id_a, edge.customer_id_b)
        absent = [cid for cid in pair if cid not in by_id]
        if absent:
            for cid in absent:
                where = (
                    "was excluded as a test account" if cid in excluded_ids
                    else "is not in raw.customers"
                )
                notes.append(
                    f"manual_merge({pair[0]}, {pair[1]}) decided by {edge.decided_by} was "
                    f"NOT applied: C{cid} {where}. A human decision the resolver cannot "
                    f"honour is reported, never dropped."
                )
            continue
        uf.union(*pair)
        manual_members.update(pair)
        manual_applied += 1

    # ── STAGES 5-7 -- refuse, merge, or leave alone ──────────────────────────────────
    groups_formed = 0
    for members_ids in uf.groups().values():
        members = [by_id[cid] for cid in members_ids]

        if len(members) == 1:
            only = members[0]
            rows.append(
                MapRow(
                    customer_id=only.customer_id,
                    canonical_customer_id=only.customer_id,
                    is_canonical=True,
                    resolution=RESOLUTION_SINGLETON,
                    evidence=(
                        None if keys[only.customer_id] is not None
                        else "no proof key: a component of (ssn_last4, dob, last_name) "
                             "is not evidence"
                    ),
                )
            )
            continue

        groups_formed += 1
        group_key = _group_key_text(members_ids, keys)
        rule = RULE_MANUAL_MERGE if manual_members & set(members_ids) else RULE_PROOF_TUPLE
        money_ids = frozenset(
            cid for cid in members_ids
            if money_moved(tuple(advances_by_customer.get(cid, ())))
        )

        # ── STAGE 5 -- more than one member has moved money: REFUSE ──────────────────
        if len(money_ids) > 1:
            review = _refusal(
                members_ids, money_ids, group_key, advances_by_customer, cards_by_customer,
                by_id,
            )
            reviews.append(review)
            for cid in members_ids:
                rows.append(
                    MapRow(
                        customer_id=cid,
                        canonical_customer_id=cid,
                        is_canonical=True,
                        resolution=RESOLUTION_REVIEW,
                        rule=None,
                        group_key=group_key,
                        evidence=(
                            f"refused ({review.reason}); see meta.merge_review review_id "
                            f"{review.review_id}"
                        ),
                    )
                )
            continue

        # ── STAGE 6 -- merge ────────────────────────────────────────────────────────
        survivor_id, survivor_reason = choose_survivor(members, money_ids)
        alias_ids = [cid for cid in members_ids if cid != survivor_id]
        rows.append(
            MapRow(
                customer_id=survivor_id,
                canonical_customer_id=survivor_id,
                is_canonical=True,
                resolution=RESOLUTION_MERGED,
                rule=rule,
                group_key=group_key,
                survivor_reason=survivor_reason,
                evidence=(
                    f"survivor by {survivor_reason}; absorbs "
                    + ",".join(f"C{cid}" for cid in alias_ids)
                    + " by indirection -- no source row rewritten"
                ),
            )
        )
        for cid in alias_ids:
            rows.append(
                MapRow(
                    customer_id=cid,
                    canonical_customer_id=survivor_id,
                    is_canonical=False,
                    resolution=RESOLUTION_MERGED,
                    rule=rule,
                    group_key=group_key,
                    survivor_reason=survivor_reason,
                    evidence=f"alias of C{survivor_id} ({survivor_reason})",
                )
            )

    # ── STAGE 8 -- the stated cost of refusing field-level coalescing ────────────────
    #
    # The survivor's row is taken WHOLE. G12's survivor C2411 carries a malformed 8-digit
    # phone -- '(555) 012-33', defect_class 'digit_count_8' -- while the loser C0338 had
    # a valid one, and this count is where that shows up instead of being quietly
    # repaired. Stitching a 'best' record out of several rows produces a customer who
    # never existed, and ops then cannot contact anybody by it.
    #
    # Test accounts are canonical-to-themselves but are NOT counted: they are out of the
    # population on both sides of every figure, or the number is dishonest.
    bad_contact: list[int] = []
    for row in rows:
        if not row.is_canonical:
            continue
        customer = by_id[row.customer_id]
        if not (
            normalize_email(customer.email).is_evidence
            and normalize_phone(customer.phone).is_evidence
        ):
            bad_contact.append(row.customer_id)

    # ── STAGE 8b -- CONTACT QUALITY, counted rather than described ──────────────────
    #
    # The brief asks for malformed phones and emails to be found, COUNTED, and for a
    # decision about them. The decision is the third of the three it offers: they are
    # neither repaired nor dropped, they are REFUSED AS EVIDENCE and counted. Repairing
    # a phone number invents a way to contact somebody; dropping the row loses a real
    # customer; refusing the field costs only the matches that field would have made,
    # and those are exactly the matches that should not be made on a broken value.
    #
    # FOUR STATES, NOT TWO, and the split is the useful part: `missing` is an absent
    # value, `malformed` is a value that failed its shape, and `placeholder` is a value
    # that PASSES its shape and is still not evidence -- '5555555555' is a valid phone
    # number and identifies nobody. Collapsing those three into 'bad' loses the only
    # one of them that is dangerous.
    #
    # Counted over the POPULATION, so test accounts are out of it. A contact-quality
    # figure that includes synthetic rows describes the fixture, not the book.
    quality: dict[tuple[str, str], int] = {}
    for customer in by_id.values():
        for field_name, normalized in (
            ("email", normalize_email(customer.email)),
            ("phone", normalize_phone(customer.phone)),
        ):
            key = (field_name, str(normalized.status))
            quality[key] = quality.get(key, 0) + 1

    # ── STAGE 8c -- WHERE THE CARDS LANDED ──────────────────────────────────────────
    #
    # Cards are never re-pointed. A card keeps the `customer_id` it was issued against
    # and reaches its person THROUGH the map, which is the same indirection the merge
    # itself is: rewrite nothing, resolve at read time. An unmerge is then a one-row map
    # edit and the cards follow it, where re-pointing them would need a restore.
    #
    # WHAT BREAKS IF YOU GET IT WRONG, as a number rather than a warning: two customers
    # who each held their own default card become ONE resolved person holding TWO
    # defaults, and nothing can say which card a charge should hit. That is a live
    # ambiguity in a lender's billing path, not a tidiness problem, so it is counted
    # every run and named. It is NOT repaired here -- picking a winner between two
    # customer-chosen default cards is a decision for whoever owns the billing
    # relationship, and this stage has no basis for it.
    # SPLIT BY CAUSE, because the split is the whole finding. A single count of 27 reads
    # like source dirt. Holding the ORIGINAL customer id alongside each default card
    # separates a customer who already had two from a canonical customer who has two only
    # because two people were merged into one -- and on this fixture that is 0 and all of
    # them respectively. The source's one-default-per-customer invariant is intact; the
    # merge is what breaks it.
    defaults_by_canonical: dict[int, list[tuple[int, int]]] = {}
    canonical_of = {r.customer_id: r.canonical_customer_id for r in rows}
    for customer_id, cards in cards_by_customer.items():
        canonical = canonical_of.get(customer_id)
        if canonical is None:
            continue                     # a test account, out of the population
        for card in cards:
            if card.is_default:
                defaults_by_canonical.setdefault(canonical, []).append(
                    (customer_id, card.card_id)
                )
    multi_default: list[int] = []
    multi_default_premerge: list[int] = []
    for cid, held in defaults_by_canonical.items():
        if len(held) <= 1:
            continue
        multi_default.append(cid)
        if len({owner for owner, _card in held}) == 1:
            multi_default_premerge.append(cid)
    multi_default.sort()
    multi_default_premerge.sort()

    return Resolution(
        rows=tuple(sorted(rows, key=lambda r: r.customer_id)),
        reviews=tuple(reviews),
        groups_formed=groups_formed,
        manual_decisions_applied=manual_applied,
        bad_contact_canonical_ids=tuple(sorted(bad_contact)),
        contact_quality=tuple(sorted((f, s, n) for (f, s), n in quality.items())),
        multi_default_card_ids=tuple(multi_default),
        multi_default_premerge_ids=tuple(multi_default_premerge),
        notes=tuple(notes),
    )


def _group_key_text(
    members_ids: Sequence[int], keys: Mapping[int, ProofKey | None]
) -> str | None:
    """`ssn|dob|surname` for the group, or None when no member has a proof key.

    Normally every keyed member shares one key and this is that key. A `manual_merge` edge
    can join members whose keys DIFFER (or who have none), and then this reports every key
    present, joined by ' + ', rather than picking one and implying agreement that a human
    -- not the tuple -- supplied.
    """
    present = sorted(
        {
            f"{key.ssn_last4}|{key.date_of_birth}|{key.last_name}"
            for cid in members_ids
            if (key := keys.get(cid)) is not None
        }
    )
    return " + ".join(present) if present else None


def _funding_instruments(advances: Sequence[AdvanceFields]) -> frozenset[str]:
    """The distinct repayment accounts behind this customer's money-moved advances.

    STRIPPED BEFORE COMPARING, and that is not tidiness. `repayment_account_hash` is
    CHAR(64), so SQL Server space-pads anything shorter and the padding arrives in the
    warehouse; DuckDB then compares it, unlike SQL Server, without padding. The same
    engine disagreement that makes `external_advance_id` count 7,997 rows one side and
    8,000 the other would here turn one account into two -- and "two distinct
    instruments" is the first-party fraud verdict. A whitespace difference must never be
    able to accuse a customer.
    """
    return frozenset(
        advance.repayment_account_hash.strip()
        for advance in advances
        if advance.repayment_account_hash
        and advance.repayment_account_hash.strip()
        and status_meaning(advance.status) in MONEY_MOVED_MEANINGS
    )


def _refusal(
    members_ids: Sequence[int],
    money_ids: frozenset[int],
    group_key: str | None,
    advances_by_customer: Mapping[int, Sequence[AdvanceFields]],
    cards_by_customer: Mapping[int, Sequence[CardFields]],
    by_id: Mapping[int, CustomerFields],
) -> ReviewRow:
    """Build the review row for a group with two or more money-moved members.

    The instrument comparison is the whole reason there are two reasons rather than one.
    Same repayment account on both sides (G02a, both on the '9' hash) is one person with
    two records and a human confirms it. DIFFERENT accounts with overlapping funded
    windows (G02b, on the '1' and '2' hashes; G13's C5002 on 'c' against A4408 on 'd') is
    a first-party fraud signal for a cash-advance lender -- merging it destroys the trace
    of a possible loss event, so the evidence string has to say that outright or the
    reviewer reads a data-quality ticket.
    """
    instruments: set[str] = set()
    lines: list[str] = []
    for cid in sorted(money_ids):
        advances = tuple(advances_by_customer.get(cid, ()))
        instruments.update(_funding_instruments(advances))
        for line in money_moved_evidence(advances):
            lines.append(f"C{cid}: {line}")

    # The hashes themselves are NOT repeated here: money_moved_evidence already prints each
    # one whole, on purpose, because a truncated hash cannot answer the reviewer's actual
    # question. This sentence carries the verdict and the count; the lines below carry the
    # evidence for it.
    if len(instruments) > 1:
        reason = REASON_TWO_FUNDED_DIFFERENT_INSTRUMENT
        verdict = (
            f"DIFFERENT funding instruments across the money-moved members "
            f"({len(instruments)} distinct repayment accounts, listed in full below). For "
            f"a cash-advance lender that is a first-party fraud signal, not a "
            f"data-quality ticket: merging these records destroys the evidence of a "
            f"possible loss event. REFUSED -- a human decides."
        )
    else:
        reason = REASON_TWO_FUNDED_SAME_INSTRUMENT
        shown = (
            "one repayment account, listed in full below" if instruments
            else "no repayment account recorded on either side"
        )
        verdict = (
            f"SAME funding instrument ({shown}) -- most likely one person with two "
            f"records, but two money-moved members means an auto-merge would move "
            f"somebody's borrowing history on the resolver's own authority. REFUSED -- a "
            f"human confirms, and meta.manual_merge is where that decision goes."
        )

    # The suggestive tier appears here and NOWHERE else: as annotation a human reads.
    # It never causes a merge -- G03 (household mailbox, four signals agreeing, mother and
    # son), G04 (shared office inbox), G05 (roommates) and G06 (Sr/Jr) are all in the
    # fixture to prove that two agreeing weak fields must not be enough.
    signals: set[str] = set()
    ordered = sorted(members_ids)
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            signals.update(
                suggestive_signals(
                    by_id[left],
                    by_id[right],
                    tuple(cards_by_customer.get(left, ())),
                    tuple(cards_by_customer.get(right, ())),
                )
            )
    if signals:
        lines.append(
            "suggestive signals agreeing (never a merge basis, context only): "
            + ", ".join(sorted(signals))
        )

    return ReviewRow(
        group_key=group_key,
        customer_ids=tuple(ordered),
        reason=reason,
        evidence=verdict + " " + "; ".join(lines),
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# The effectful shell -- read raw.*, publish meta.*
# ─────────────────────────────────────────────────────────────────────────────────────

_Indexable = TypeVar("_Indexable", AdvanceFields, CardFields)


def _index_by_customer(
    items: Sequence[_Indexable],
) -> dict[int, tuple[_Indexable, ...]]:
    grouped: dict[int, list[_Indexable]] = {}
    for item in items:
        grouped.setdefault(item.customer_id, []).append(item)
    return {cid: tuple(rows) for cid, rows in grouped.items()}


def _fetch(
    conn: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Read one raw table as dicts, ordered by its primary key.

    Ordered deliberately: DuckDB makes no ordering promise, and an unordered read makes
    the report's row order differ between runs over identical data -- which is exactly the
    kind of diff noise that trains a reviewer to stop reading diffs.
    """
    projection = ", ".join(f'"{col}"' for col in columns)
    rows = conn.execute(
        f'SELECT {projection} FROM raw."{table}" ORDER BY "{columns[0]}"'
    ).fetchall()
    return [dict(zip(columns, row)) for row in rows]


def _read_manual_merges(conn: duckdb.DuckDBPyConnection) -> list[ManualEdge]:
    rows = conn.execute(
        "SELECT customer_id_a, customer_id_b, decided_by, decided_at, note "
        "FROM meta.manual_merge ORDER BY customer_id_a, customer_id_b"
    ).fetchall()
    return [
        ManualEdge(
            customer_id_a=int(a), customer_id_b=int(b), decided_by=decided_by,
            decided_at=decided_at, note=note,
        )
        for a, b, decided_by, decided_at, note in rows
    ]


def _require_meta_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Fail with the fix, not with a catalogue error three statements later."""
    present = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'meta'"
        ).fetchall()
    }
    missing = [name for name in _META_TABLES if name not in present]
    if missing:
        raise RuntimeError(
            "The resolver does not create its own tables, and these are missing: "
            + ", ".join(f"meta.{name}" for name in missing)
            + ". Call Warehouse.create_schema(manifest) after Warehouse.connect() -- "
              "every schema in this warehouse is declared there, in one place."
        )


def _next_run_id(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(run_id), 0) + 1 FROM meta.identity_run"
    ).fetchone()
    return int(row[0]) if row else 1


def _assert_reconciles(
    summary: ResolutionSummary, *, mirror_customers: int
) -> None:
    """Refuse to publish a map that does not add up.

    Every check here is a bug in this module rather than a condition in the data, so the
    response is a raise inside the transaction -- which rolls the whole write back and
    leaves the previous map in place. Publishing a map that fails its own arithmetic is
    worse than publishing nothing, because every row in it still looks valid.

    `mirror_customers` is `len()` of the customer list this run actually read, inside the
    same transaction and therefore the same snapshot. Passing the list's own length rather
    than issuing a second `COUNT(*)` is deliberate: a second read could disagree with the
    first, and then the check would be testing the warehouse's concurrency behaviour
    instead of this module's completeness.

    EVERY OTHER CHECK BELOW IS SATISFIED BY AN EMPTY MAP. That is what the first two
    guards are for, and it was a real hole rather than a hypothetical one: the arithmetic
    reconcile is `customers_in - excluded_test == canonical_out + rows_merged`, and every
    one of those four numbers is derived from `summary.rows`, so at zero rows it reads
    `0 == 0 + 0` and passes. Combined with `_write` deleting the previous map
    unconditionally, a run over an empty mirror replaced a correct map with an empty one,
    wrote an all-zeros `meta.identity_run` row, printed "REVIEW QUEUE -- empty", and
    exited 0. Downstream marts joining `meta.customer_map` would then resolve nothing,
    with the run log asserting success. Self-consistency is not completeness, and only
    the second one can be checked against something outside this module's own output.
    """
    # An empty map is never a legitimate result. Resolving before loading is the caller's
    # mistake, not an outcome, and it is reported as a precondition rather than published.
    if not summary.rows:
        raise ValueError(
            "refusing to publish an empty customer map: the resolver read "
            f"{mirror_customers} row(s) from raw.customers. "
            + ("Load before resolving -- `python -m pipeline load`, or `python -m "
               "pipeline demo` for the whole sequence."
               if mirror_customers == 0 else
               "The mirror is not empty, so this is a bug in this module: every customer "
               "read must appear in the map exactly once.")
        )

    # COMPLETENESS, against the one number that does not come from `summary.rows`. Without
    # this, a resolution that silently dropped customers -- a stage that returns a subset,
    # a grouping that loses a member -- publishes a map that is internally perfect and
    # missing people, and every check above it still passes.
    if len(summary.rows) != mirror_customers:
        raise ValueError(
            f"meta.customer_map would hold {len(summary.rows)} row(s) for the "
            f"{mirror_customers} customer(s) read from raw.customers "
            f"({len(summary.rows) - mirror_customers:+d}) -- the map must carry exactly "
            f"one row per mirror customer, including the excluded test accounts"
        )

    ids = [row.customer_id for row in summary.rows]
    if len(set(ids)) != len(ids):
        raise ValueError("meta.customer_map would hold a customer twice")

    for row in summary.rows:
        if row.is_canonical != (row.customer_id == row.canonical_customer_id):
            raise ValueError(
                f"C{row.customer_id}: is_canonical disagrees with canonical_customer_id"
            )

    # No alias may point at another alias. A two-hop chain does not fail any join -- it
    # silently resolves half a group to the wrong customer.
    canonical_ids = {row.customer_id for row in summary.rows if row.is_canonical}
    for row in summary.rows:
        if row.canonical_customer_id not in canonical_ids:
            raise ValueError(
                f"C{row.customer_id} points at C{row.canonical_customer_id}, which is not "
                f"canonical -- an alias of an alias"
            )

    resolved = summary.customers_in - summary.excluded_test
    if resolved != summary.canonical_out + summary.rows_merged:
        raise ValueError(
            f"population does not reconcile: {summary.customers_in} in - "
            f"{summary.excluded_test} excluded != {summary.canonical_out} canonical + "
            f"{summary.rows_merged} merged away"
        )


def _write(conn: duckdb.DuckDBPyConnection, summary: ResolutionSummary) -> None:
    """Publish the map, the review queue and the run row. Caller holds the transaction.

    Caller must also have run `_assert_reconciles` first: the map insert below is
    unconditional, and it is the completeness check there that makes that safe.
    """
    # The map is a SNAPSHOT of one run, not an append log, so the whole previous map goes.
    # Inside the same transaction as the insert, always: a map that is half old and half
    # new is worse than either, and it is worse silently -- every row in it still parses,
    # still joins and still looks resolved.
    conn.execute("DELETE FROM meta.customer_map")
    # The review queue and the run log are HISTORY and keep every other run's rows, so a
    # reviewer can see a refusal recur. Only this run_id is cleared, which is what makes a
    # crashed-then-retried run re-runnable instead of a primary key collision.
    conn.execute("DELETE FROM meta.merge_review WHERE run_id = ?", [summary.run_id])
    conn.execute("DELETE FROM meta.identity_run WHERE run_id = ?", [summary.run_id])

    # UNCONDITIONAL, and paired with the DELETE above on purpose. A `if summary.rows:`
    # guard here is the asymmetry that turns an empty result into silent destruction: the
    # delete always runs, so the guard's False branch publishes emptiness. `rows` is
    # guaranteed non-empty by `_assert_reconciles`, which the caller runs before this --
    # so if this ever raises on an empty list, the guard that belongs upstream is missing,
    # and that is the bug worth seeing.
    conn.executemany(
        "INSERT INTO meta.customer_map (customer_id, canonical_customer_id, "
        "is_canonical, resolution, rule, group_key, survivor_reason, evidence, "
        "run_id, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [row.customer_id, row.canonical_customer_id, row.is_canonical,
             row.resolution, row.rule, row.group_key, row.survivor_reason,
             row.evidence, summary.run_id, summary.resolved_at]
            for row in summary.rows
        ],
    )

    if summary.reviews:
        conn.executemany(
            "INSERT INTO meta.merge_review (review_id, run_id, group_key, customer_ids, "
            "reason, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                [review.review_id, summary.run_id, review.group_key,
                 review.customer_ids_csv, review.reason, review.evidence,
                 summary.resolved_at]
                for review in summary.reviews
            ],
        )

    conn.execute(
        "INSERT INTO meta.identity_run (run_id, customers_in, excluded_test, "
        "groups_formed, rows_merged, canonical_out, review_rows, "
        "manual_decisions_applied, canonical_with_bad_contact, wall_ms, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [summary.run_id, summary.customers_in, summary.excluded_test,
         summary.groups_formed, summary.rows_merged, summary.canonical_out,
         summary.review_row_count, summary.manual_decisions_applied,
         summary.canonical_with_bad_contact, summary.wall_ms, summary.resolved_at],
    )


def resolve_identities(
    warehouse: Warehouse, *, run_id: int | None = None
) -> ResolutionSummary:
    """Re-derive the whole customer map from current warehouse state and publish it.

    Preconditions: `Warehouse.connect()` and `Warehouse.create_schema()` have run. The
    `meta.*` tables are declared there, with every other schema, and never here.

    `run_id` defaults to the next id in `meta.identity_run`. Pass the load's run_id to tie
    a map to the load that produced the rows it read.

    READS AND WRITES SHARE ONE TRANSACTION, deliberately, and this is not the same point
    as the atomic write. The map is a CLAIM ABOUT A STATE. Read that state outside the
    transaction that publishes the claim and a human INSERT into `meta.manual_merge` -- or
    another load -- can land in between, leaving a map that disagrees with the tables it
    says it was derived from, with nothing anywhere recording that it ever happened.
    """
    t0 = time.monotonic()
    conn = warehouse.connection
    _require_meta_tables(conn)

    # One clock read for the whole run. Every row carries the same resolved_at, because a
    # map stamped across two timestamps reads as two runs.
    resolved_at = datetime.now()

    with warehouse.transaction():
        customers = [
            CustomerFields.from_row(row)
            for row in _fetch(conn, "customers", _CUSTOMER_COLUMNS)
        ]
        advances = [
            AdvanceFields.from_row(row)
            for row in _fetch(conn, "advances", _ADVANCE_COLUMNS)
        ]
        cards = [CardFields.from_row(row) for row in _fetch(conn, "cards", _CARD_COLUMNS)]
        manual_edges = _read_manual_merges(conn)

        advances_by_customer = _index_by_customer(advances)
        cards_by_customer = _index_by_customer(cards)

        # ── STAGE 2 ── TEST ACCOUNTS OUT, BEFORE ANYTHING IS GROUPED ────────────────
        screening = screen_test_accounts(customers, advances_by_customer)

        # ── STAGES 3-8 ── the only input is stage 2's output. See `Screening`. ───────
        resolution = group_and_merge(
            screening, advances_by_customer, cards_by_customer, manual_edges
        )

        summary = ResolutionSummary(
            run_id=run_id if run_id is not None else _next_run_id(conn),
            resolved_at=resolved_at,
            rows=sorted(
                [*screening.excluded, *resolution.rows], key=lambda r: r.customer_id
            ),
            reviews=[*screening.reviews, *resolution.reviews],
            notes=list(resolution.notes),
            groups_formed=resolution.groups_formed,
            manual_decisions_applied=resolution.manual_decisions_applied,
            bad_contact_canonical_ids=list(resolution.bad_contact_canonical_ids),
            contact_quality=list(resolution.contact_quality),
            multi_default_card_ids=list(resolution.multi_default_card_ids),
            multi_default_premerge_ids=list(resolution.multi_default_premerge_ids),
        )

        # `len(customers)` is the mirror's row count as this transaction's snapshot sees
        # it -- the same list every stage above was derived from, so the completeness
        # check compares the map against its own input rather than against a re-read.
        _assert_reconciles(summary, mirror_customers=len(customers))
        # Measured to the last write and not past the commit, so the number is one a
        # reviewer can reproduce from this code rather than one only this process saw.
        summary.wall_ms = int((time.monotonic() - t0) * 1000)
        _write(conn, summary)

    return summary


# ─────────────────────────────────────────────────────────────────────────────────────
# The report
# ─────────────────────────────────────────────────────────────────────────────────────

def format_resolution(summary: ResolutionSummary) -> str:
    """Render one resolver run as a table a non-engineer can read.

    ASCII only, same reason as `load.format_run`: box-drawing characters crash a default
    Windows console (cp1252), and a reviewer piping `docker compose logs` through one is a
    real path rather than a hypothetical.
    """
    width = 96
    lines: list[str] = []
    stamp = summary.resolved_at.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(
        f"  IDENTITY RUN {summary.run_id}    resolved {stamp}    {summary.wall_ms:,} ms"
    )
    lines.append("  " + "-" * width)

    counts: list[tuple[str, int, str]] = [
        ("customers in", summary.customers_in, ""),
        ("excluded as test data", summary.excluded_test,
         "removed BEFORE grouping, never after"),
        ("groups formed", summary.groups_formed, "proof tuple, plus manual_merge edges"),
        ("rows merged away", summary.rows_merged,
         "aliases -- no source row was rewritten"),
        ("canonical customers out", summary.canonical_out, ""),
        ("review rows", summary.review_row_count, "refused: a human decides, not this"),
        ("manual decisions applied", summary.manual_decisions_applied,
         "from meta.manual_merge"),
        ("canonical with bad contact", summary.canonical_with_bad_contact,
         "stated cost of no field-level coalescing"),
        ("resolved with 2+ default cards", summary.multi_default_cards,
         f"{summary.multi_default_caused_by_merge:,} caused BY the merge, "
         f"{len(summary.multi_default_premerge_ids):,} already like that"),
    ]
    for label, value, note in counts:
        suffix = f"   {note}" if note else ""
        lines.append(f"  {label:<28}{value:>10,}{suffix}")

    lines.append("  " + "-" * width)

    if summary.contact_quality:
        lines.append("  CONTACT QUALITY -- counted over the population, test accounts out")
        lines.append("  refused as evidence, never repaired and never dropped: repairing a "
                     "phone invents")
        lines.append("  a way to reach somebody, and dropping the row loses a real customer")
        by_field: dict[str, list[tuple[str, int]]] = {}
        for field_name, status, count in summary.contact_quality:
            by_field.setdefault(field_name, []).append((status, count))
        for field_name in sorted(by_field):
            states = sorted(by_field[field_name])
            total = sum(n for _s, n in states)
            rendered = "  ".join(f"{s} {n:,}" for s, n in states)
            lines.append(f"    {field_name:<8}{total:>8,}   {rendered}")
        lines.append("  " + "-" * width)

    if not summary.reviews:
        lines.append("  REVIEW QUEUE -- empty")
    else:
        lines.append(
            "  REVIEW QUEUE -- nothing below was merged, and nothing below is merged "
            "without a human"
        )
        for review in sorted(summary.reviews, key=lambda r: (r.reason, r.customer_ids)):
            lines.append(f"    review_id {review.review_id}   {review.reason}")
            ids = ",".join(f"C{cid}" for cid in review.customer_ids)
            key = f"   key {review.group_key}" if review.group_key else ""
            lines.append(f"      customers {ids}{key}")
            for chunk in textwrap.wrap(review.evidence, width=width - 8) or [""]:
                lines.append(f"      {chunk}")

    for note in summary.notes:
        lines.append(f"\n  NOTE: {note}")
    return "\n".join(lines)
