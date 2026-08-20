"""The identity rules, as pure functions: what proves identity, what merely suggests it,
and what is a test artifact rather than a customer.

THE FAILURE THIS MODULE PREVENTS: a rule that reads a raw source column directly. Every
predicate here reads a normalized value behind `Normalized.is_evidence`, because the raw
columns are exactly where this source hides its traps -- 15 spellings of 7 advance
statuses, an internal-domain mailbox belonging to a real staff member, a reserved-domain
address the normalizer correctly refuses to normalize at all. `WHERE status = 'funded'`
and `email LIKE '%test%'` are not slightly wrong. They are wrong on specific named
customers who have moved money, and they are wrong without raising anything.

PURE, and deliberately so rather than incidentally: no I/O, no database handle, no
clock, no `datetime.now()`. Every function here is a function of its arguments, so the
entire rule surface is testable on a bare interpreter with no container running -- see
tests/test_rules.py. The effectful half lives in resolve.py.

TWO PRECEDENCES ARE SETTLED HERE, because the brief leaves them unstated:

  * MONEY OUTRANKS TEST-DATA PATTERNS. A customer who has moved money is never
    auto-excluded as a test artifact; the verdict says which rules would have fired and
    the caller routes it to a human. C4973 and C4974 are the exhibits -- canonical
    artifact rows carrying `funded` advances of 0.00 and 999999.99 -- and C0402 Marcus
    Testerman is the naive filter's version of the same mistake.

  * A STATUS OUTSIDE THE VOCABULARY IS `unrecognised`, NEVER "not funded". Folding an
    unknown spelling into the negative branch is how a funded veto silently stops
    vetoing on the day the source adds a sixteenth spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import AbstractSet, Any, Mapping, Sequence

from .normalize import (
    Normalized,
    normalize_dob,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_ssn_last4,
)


# ─────────────────────────────────────────────────────────────────────────────────────
# The row shapes.
#
# Dataclasses rather than the dicts the warehouse hands back, so that every rule below
# NAMES the field it reads and a mistyped field name raises at import-adjacent time
# instead of degrading. `row['ssn_last4']` typo'd to `row['ssn_last']` raises;
# `row.get('ssn_last')` returns None, forms no proof key for anybody, and reports a
# recall of zero as though that were a finding about the data.
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CustomerFields:
    """One customer row, as the identity rules see it."""

    customer_id: int
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    ssn_last4: str | None
    date_of_birth: date | None
    address_line1: str | None
    city: str | None
    state_code: str | None
    postal_code: str | None
    employer_name: str | None
    signup_channel: str | None
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CustomerFields":
        """Build from a warehouse row. A MISSING COLUMN RAISES rather than defaulting.

        The same trade `warehouse.duckdb_type` makes, for the same reason: every column
        named here is in tables.yml, so a KeyError means the manifest and this module
        have drifted. `row.get(...)` would turn that drift into a resolver that reads
        None for an identity field and confidently proves nothing.
        """
        return cls(
            customer_id=int(row["customer_id"]),
            first_name=row["first_name"],
            last_name=row["last_name"],
            email=row["email"],
            phone=row["phone"],
            ssn_last4=row["ssn_last4"],
            date_of_birth=row["date_of_birth"],
            address_line1=row["address_line1"],
            city=row["city"],
            state_code=row["state_code"],
            postal_code=row["postal_code"],
            employer_name=row["employer_name"],
            signup_channel=row["signup_channel"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def freshness(self) -> datetime:
        """COALESCE(updated_at, created_at). 60 pre-2019 rows have a NULL updated_at.

        Ordering on updated_at alone would sort every one of those rows as the oldest
        record in existence, and the fixture aims that trap at the pairs survivorship
        has to get right: G07's C0203 and G11's C1866 are each BOTH a duplicate AND a
        NULL-updated_at legacy row.

        dbo.customers.created_at is NOT NULL at the source (01_schema.sql), so the
        coalesce always lands on something. If it ever does not, that is a schema change
        and it raises -- substituting an epoch would silently make the row lose every
        comparison it takes part in.
        """
        stamp = self.updated_at or self.created_at
        if stamp is None:
            raise ValueError(
                f"customer {self.customer_id} has neither updated_at nor created_at, so "
                f"there is no defensible freshness for it. created_at is NOT NULL at the "
                f"source, so this is a schema change rather than a data defect."
            )
        return stamp


@dataclass(frozen=True)
class AdvanceFields:
    """One advance, reduced to the fields the identity rules are allowed to read."""

    advance_id: int
    customer_id: int
    status: str | None
    principal_amount: float | None
    funded_at: datetime | None
    paid_off_at: datetime | None
    repayment_account_hash: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AdvanceFields":
        return cls(
            advance_id=int(row["advance_id"]),
            customer_id=int(row["customer_id"]),
            status=row["status"],
            # float(), not Decimal(): principal_amount is a FLOAT at the source and is
            # stored as DOUBLE in the warehouse. Casting it to a decimal here would
            # produce a value that looks exact and is not -- the precision was lost
            # before this module ever saw it. See warehouse._DUCKDB_TYPES.
            principal_amount=(
                None if row["principal_amount"] is None else float(row["principal_amount"])
            ),
            funded_at=row["funded_at"],
            paid_off_at=row["paid_off_at"],
            repayment_account_hash=row["repayment_account_hash"],
        )


@dataclass(frozen=True)
class CardFields:
    """One stored card. The INSTRUMENT is the fingerprint, never the token or the id."""

    card_id: int
    customer_id: int
    card_fingerprint: str
    is_default: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CardFields":
        return cls(
            card_id=int(row["card_id"]),
            customer_id=int(row["customer_id"]),
            card_fingerprint=row["card_fingerprint"],
            # BIT arrives as 0/1 through pymssql, so bool() is a conversion rather than
            # decoration: `1 is True` is False, which makes an `is True` check on an
            # unconverted value quietly wrong for every default card in the table.
            is_default=bool(row["is_default"]),
        )


# ─────────────────────────────────────────────────────────────────────────────────────
# A1 -- the status vocabulary and the funded veto.
#
# 15 raw spellings, 7 meanings, taken from the #status table in 02c_seed_advances.sql.
# The map below has 8 keys rather than 15 because it is keyed on the NORMALIZED
# spelling: 'funded', 'FUNDED ' and 'Funded' all arrive here as 'funded'.
#
# WHY THIS CANNOT BE `status = 'funded'`. SQL Server's default collation is
# case-INSENSITIVE and pads trailing spaces, so that predicate accidentally works AT THE
# SOURCE -- it catches all three spellings, and the bug is therefore invisible where it
# is written. DuckDB compares case-sensitively and does not pad, so the same predicate
# re-implemented in the warehouse keeps the veto on exactly ONE of the six money-moved
# spellings. Counting the whole vocabulary: only 6 of the 15 raw spellings are already
# identical to the meaning they carry; the other 9 need casefolding, trimming, separator
# folding or a vocabulary alias before anything can recognise them. The defect is not
# the SQL, it is the unexamined dependency on a collation.
# ─────────────────────────────────────────────────────────────────────────────────────

MONEY_MOVED_MEANINGS = frozenset({"funded", "paid_off"})

_STATUS_MEANINGS: dict[str, str] = {
    "funded": "funded",         # 'funded', 'FUNDED ' (trailing space), 'Funded'
    "paid off": "paid_off",     # 'paid_off', 'Paid Off', 'PAID OFF'
    "pending": "pending",       # 'pending', 'Pending'
    "approved": "approved",     # 'approved', 'APPROVED'
    "declined": "declined",     # 'declined', 'Declined'
    "cancelled": "cancelled",   # two real spellings of one meaning, and both occur
    "canceled": "cancelled",
    "expired": "expired",
}


def status_meaning(raw: str | None) -> str:
    """Map a raw status to its meaning. 'missing' for NULL/blank, 'unrecognised' for
    anything outside the vocabulary.

    Normalizes by strip + casefold + collapsing internal whitespace + folding '_' to ' ',
    so 'FUNDED ' and 'funded' and 'Funded' are one meaning and 'paid_off' == 'Paid Off'.
    Only 6 of the 15 spellings are already identical to their meaning; the other 9 need
    casefolding, trimming, separator folding or an alias, so a predicate that compares
    raw text is wrong on 9 of the 15. A case-sensitive funded check keeps the veto on
    exactly one of the six spellings that moved money.

    'unrecognised' IS A FIRST-CLASS RETURN VALUE and is never folded into "not funded".
    A sixteenth spelling arriving from a source with no CHECK constraint on this column
    has to surface as an unknown somebody can report, not as a customer who quietly
    stopped having moved money.
    """
    if raw is None:
        return "missing"
    # '_' -> ' ' happens BEFORE the whitespace collapse so 'paid_off' and 'Paid  Off'
    # land on the same key.
    folded = re.sub(r"\s+", " ", raw.replace("_", " ")).strip().casefold()
    if not folded:
        return "missing"
    return _STATUS_MEANINGS.get(folded, "unrecognised")


def money_moved(advances: Sequence[AdvanceFields]) -> bool:
    """Whether any of these advances has moved money.

    THE SEAM, named and deliberately not crossed: this reads `status` and nothing else.
    Corroborating it against `funded_at` and `repayment_account_hash` -- a 'funded' row
    with neither is arguably a status that was never true -- is a separately scoped
    change, because it converts a veto into a judgement and every group it moved would
    have to be re-scored.
    """
    return any(status_meaning(a.status) in MONEY_MOVED_MEANINGS for a in advances)


def money_moved_evidence(advances: Sequence[AdvanceFields]) -> tuple[str, ...]:
    """Evidence lines for a review row: advance id, meaning, funding instrument.

    ASCII, one line per money-moved advance, ordered by advance id so a recurring
    refusal reads identically on every run and a reviewer can tell it from a new one.

    THE INSTRUMENT HASH IS PRINTED WHOLE. The reviewer's question on a two-funded group
    is whether the members repay from the SAME account -- G02a's two members both carry
    REPLICATE('9', 64), while G02b's carry REPLICATE('1', 64) and REPLICATE('2', 64)
    with overlapping funded windows, which for a cash-advance lender is a first-party
    fraud signal rather than a data-quality ticket. A truncated hash cannot answer that
    question, so it is not truncated.
    """
    lines: list[str] = []
    for advance in sorted(advances, key=lambda a: a.advance_id):
        meaning = status_meaning(advance.status)
        if meaning not in MONEY_MOVED_MEANINGS:
            continue
        instrument = (advance.repayment_account_hash or "").strip()
        lines.append(
            f"advance {advance.advance_id} {meaning} instrument "
            f"{instrument or 'none recorded'}"
        )
    return tuple(lines)


# ─────────────────────────────────────────────────────────────────────────────────────
# A2 -- test-account classification.
#
# THREE RULES, because no single signal covers the 16 synthetic accounts in
# 02a_seed_customers.sql and the fixture was built to make that true:
#   C4971/C4972 carry a real employee name in the local part, so rule A cannot see them.
#   C4973/C4974 look like ordinary people and C4974 is not even on the internal domain,
#     so neither A nor B can see them.
#
# AND NO RULE MAY FIRE ON A REAL PERSON. C0088 Priya Nadkarni is a staff member on
# priya.n@fundo.com holding a paid-off advance and SIX transactions: one disbursement of
# $250.00, one $12.50 fee, and FOUR repayments totalling $262.50. The six net to exactly
# $0.00, which is what "paid off" means here. `email LIKE '%@fundo.com'` removes her and
# all six rows from the book, silently. Rule A's local-part requirement is why she stays.
#
# The $250.00 is the DISBURSEMENT -- it equals advance 91's principal -- so it is the one
# figure in this exhibit that must never be quoted as a repayment total. C0088 is held out
# of the bulk transaction pass (02e:113, `WHERE n NOT BETWEEN 212401 AND 212406`) so both
# numbers stay hand-checkable at any fixture size -- which is why getting them the wrong
# way round would be worse here than anywhere else.
# ─────────────────────────────────────────────────────────────────────────────────────

_INTERNAL_DOMAIN = "fundo.com"

# 'test', 'test1', 'test.2', 'test_03', 'test-04', 'qa', 'qa.17', 'demo', 'dev1',
# 'staging', 'automation' -- every one of those local parts is present at C4960-C4970 and
# C4975. Anchored at BOTH ends: 'test' and 'dev' are prefixes of real words ('Testerman'
# is a real surname, at C0402), so an unanchored version would fire on a local part that
# merely begins with one of them. No fixture row exercises that -- rule A also requires
# the internal domain, and C0402 is on gmail.com -- so the anchoring is defensive rather
# than measured, and it is written down as such.
_AUTOMATION_LOCAL = re.compile(r"^(test|qa|demo|dev|staging|automation)[._-]?[0-9]*$")

# The subaddress tag, anchored the same way. '+test' and '+qa2' match; G01's C4788
# carries 'a.moreau+new@gmail.com', and a rule that fired on "has a plus tag" would
# excise a real customer holding a real card.
_SUBADDRESS_TAG = re.compile(r"^(test|qa)[._-]?[0-9]*$")

_ARTIFACT_SSN = "0000"
_ARTIFACT_DOB = date(1900, 1, 1)

# Exact equality, no tolerance. These are canonical artifact LITERALS -- somebody
# exercising a form -- not measurements. 0.00 is exactly representable as a double;
# 999999.99 is not, but the same decimal literal converts to the same double under IEEE
# 754 round-to-nearest at both ends, so equality survives the FLOAT column. A tolerance
# would be the beginning of a rule that flags a real advance of 999,999.98.
_ARTIFACT_PRINCIPALS = (0.00, 999999.99)


def _raw_email_parts(raw: str | None) -> tuple[str, str]:
    """Split an email into (local, domain) using a LOCAL casefold + strip of the RAW value.

    NOT `normalize_email().normalized`, and this is the trap worth spelling out.
    normalize_email correctly returns status PLACEHOLDER with `normalized=None` for a
    reserved domain, so C4974 `carl.smith@example.com` is INVISIBLE to any classifier
    that reads `normalized` -- and so is every malformed address, a population
    normalize.py puts at 96 defective email values of which only 40 are normalizable.

    In this fixture C4974 happens to be caught by rule C anyway, so a `normalized`-only
    classifier would still exclude it -- which is precisely why the defect would go
    unnoticed here and fire on the first reserved-domain or malformed address that
    carries a +test tag instead. Classification is a question about the STRING the source
    holds, so it reads the string.

    Casefolding here rather than relying on the source: `TEST@FUNDO.COM` (C4975) is
    caught by the rule itself, not by SQL Server's collation happening to be
    case-insensitive. That collation is not a property anybody checked.
    """
    if raw is None:
        return "", ""
    local, _, domain = raw.strip().casefold().partition("@")
    return local, domain


@dataclass(frozen=True)
class TestAccountVerdict:
    """Whether this customer is a test artifact, and why -- or why the answer is refused."""

    is_test: bool
    rules: tuple[str, ...]          # subset of ("A", "B", "C"), in order
    detail: str                     # ASCII, printable in a review row
    blocked_by_money: bool = False


def classify_test_account(customer: CustomerFields,
                          advances: Sequence[AdvanceFields]) -> TestAccountVerdict:
    """Classify one customer as a test artifact, or refuse to.

    Rule A -- internal domain AND an automation local part.
    Rule B -- a +test / +qa subaddress tag. C4971 and C4972 are caught ONLY by this.
    Rule C -- canonical artifact values. C4973 and C4974 are caught ONLY by this.

    MONEY OUTRANKS ALL THREE. When a rule fires on a customer who has moved money the
    verdict is `is_test=False` with `blocked_by_money=True`, and the rules that WOULD
    have fired stay listed so the caller can route it to a human instead of deleting a
    funded advance. C4973 and C4974 are that case: canonical artifact rows carrying
    `funded` advances of 0.00 and 999999.99.

    THE BLOCK IS GATED ON A RULE HAVING FIRED, deliberately. Roughly two thirds of the
    advances in this book have moved money, so blocking on money alone would file
    thousands of review rows saying nothing about test data -- and a review queue that
    size is a review queue nobody reads.
    """
    rules: list[str] = []
    details: list[str] = []

    local, domain = _raw_email_parts(customer.email)

    if domain == _INTERNAL_DOMAIN and _AUTOMATION_LOCAL.match(local):
        rules.append("A")
        details.append(f"A: internal domain with automation local part '{local}'")

    tag = local.partition("+")[2]
    if tag and _SUBADDRESS_TAG.match(tag):
        rules.append("B")
        details.append(f"B: subaddress tag '+{tag}'")

    # Rule C reads the RAW ssn string rather than normalize_ssn_last4, because the
    # question here is the opposite one: the normalizer refuses '0000' as evidence, while
    # this rule needs to know that the source literally holds '0000'. Both readings of
    # the same column are correct for their own question.
    artifacts: list[str] = []
    if (customer.ssn_last4 or "").strip() == _ARTIFACT_SSN \
            and customer.date_of_birth == _ARTIFACT_DOB:
        artifacts.append(
            f"ssn {_ARTIFACT_SSN} with dob {_ARTIFACT_DOB.isoformat()}"
        )
    for advance in sorted(advances, key=lambda a: a.advance_id):
        if advance.principal_amount is None:
            continue
        if advance.principal_amount in _ARTIFACT_PRINCIPALS:
            artifacts.append(
                f"advance {advance.advance_id} principal {advance.principal_amount:.2f}"
            )
    if artifacts:
        rules.append("C")
        details.append("C: " + "; ".join(artifacts))

    if not rules:
        return TestAccountVerdict(
            is_test=False, rules=(), detail="no test-account rule fired"
        )

    if money_moved(advances):
        evidence = " | ".join(money_moved_evidence(advances))
        return TestAccountVerdict(
            is_test=False,
            rules=tuple(rules),
            detail=(
                "; ".join(details)
                + " -- NOT EXCLUDED: this customer has moved money, which outranks every "
                + f"test-data pattern. {evidence}"
            ),
            blocked_by_money=True,
        )

    return TestAccountVerdict(is_test=True, rules=tuple(rules), detail="; ".join(details))


def naive_test_filter(customer: CustomerFields) -> bool:
    """The counterfactual, not the shipped rule: '%test%' against name and email.

    Measured on this fixture: 14 flagged, 7 of them real people -> precision 0.500.

    The 7 real people it flags are C0402 Testerman (funded -- this filter deletes a
    funded advance), C1902 Tester, C4501 Testani, and the four mailboxes whose local
    part merely CONTAINS the substring: greatest.deals, protest.organizer,
    contest.winner1994, latest.news. It reaches only 7 of the 16 synthetic accounts; the
    other 9 -- the qa/demo/dev/staging/automation mailboxes, the '+qa2' tag at C4972, and
    the two canonical-artifact rows C4973/C4974 -- hold no 'test' in a name or an email.

    address_line1 IS NOT MATCHED, and that is a deviation from the contract's wording
    which has to be stated rather than absorbed. All 16 synthetic accounts share
    address_line1 '1 Test St', so including that field flags all 16 on one signal --
    which RAISES this filter's precision, contradicts the 0.500 the contract measured,
    and contradicts 02a_seed_customers.sql's own statement that "the naive `%test%`
    pattern catches 7 of them and 7 real people". The scorer re-measures this at run
    time; if the printed figure is not 14 / 7 / 0.500, this field set is the first place
    to look.

    Case-INSENSITIVE, matching the SQL it stands in for: SQL Server's default collation
    would flag C4975 'TEST@FUNDO.COM' too. That the naive rule catches that row at all
    is an accident of collation, not a property of the rule.
    """
    haystacks = (customer.first_name, customer.last_name, customer.email)
    return any(value is not None and "test" in value.casefold() for value in haystacks)


# ─────────────────────────────────────────────────────────────────────────────────────
# A3 -- the proof tuple. The ONLY thing in this module that may cause a merge.
# ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProofKey:
    """A complete proof tuple. Frozen and hashable, so it can key a group directly."""

    ssn_last4: str
    date_of_birth: str
    last_name: str


def proof_key(customer: CustomerFields) -> ProofKey | None:
    """The exact tuple (ssn_last4, date_of_birth, last_name), each component required to
    pass `Normalized.is_evidence`. Returns None if ANY component fails -- a partial key is
    not a weaker key, it is a different question.

    first_name is deliberately EXCLUDED: G11 is Robert/Bobby Ellison, one person.
    last_name is deliberately INCLUDED: G10 is Ferraro/Okonkwo, two unrelated people in
    different states who share ssn_last4 AND date_of_birth; surname is the only thing that
    separates them. The measured cost of including it is G09 (Nowak -> Nowak-Brennan on
    marriage), which falls to review instead of merging.

    The `is_evidence` gate is what makes G12 a pair rather than a triple: C4855's
    ssn_last4 is '0000', a placeholder rather than a value, so it forms NO key and cannot
    join C0338 and C2411. The fixture's ground truth agrees -- P-YOUSUF is not P-YUSUF.
    """
    ssn = normalize_ssn_last4(customer.ssn_last4)
    dob = normalize_dob(customer.date_of_birth)
    surname = normalize_name(customer.last_name)
    if not (ssn.is_evidence and dob.is_evidence and surname.is_evidence):
        return None
    # is_evidence is defined as `status is VALID and normalized is not None`, so all
    # three components are non-None here.
    return ProofKey(ssn.normalized, dob.normalized, surname.normalized)


# ─────────────────────────────────────────────────────────────────────────────────────
# A4 -- the suggestive tier. Annotates candidates; merges nothing, at any count.
# ─────────────────────────────────────────────────────────────────────────────────────

# The order signals are reported in. Fixed rather than incidental, so an evidence string
# on a review row is byte-identical across runs for the same pair.
_SUGGESTIVE_ORDER = ("email", "phone", "address", "surname", "card_fingerprint")


def _agrees(left: Normalized, right: Normalized) -> bool:
    """Two normalized values agree only when BOTH are evidence AND equal.

    The `is_evidence` half is the load-bearing half. Two customers sharing the email
    'n/a' or the phone '0000000000' are not agreeing about anything, and under raw
    equality those shared placeholders alone propose four merges of strangers.
    """
    return left.is_evidence and right.is_evidence and left.normalized == right.normalized


def _instruments(cards: Sequence[CardFields]) -> frozenset[str]:
    """The set of payment instruments on file, as comparable tokens.

    No normalizer applies: card_fingerprint is an opaque issuer token, not a contact
    field, so there is no casing convention to fold and no placeholder vocabulary to gate
    against. The only gate is non-empty, and casefolding covers the fixture's mix of
    lowercase hex and 'fp_a1'-style literals.
    """
    return frozenset(
        card.card_fingerprint.strip().casefold()
        for card in cards
        if card.card_fingerprint and card.card_fingerprint.strip()
    )


def suggestive_signals(a: CustomerFields, b: CustomerFields,
                       cards_a: Sequence[CardFields],
                       cards_b: Sequence[CardFields]) -> tuple[str, ...]:
    """The weak signals that AGREE between two customers. Never a merge decision.

    THIS OUTPUT NEVER CAUSES A MERGE, at any count, and the fixture spends four exhibits
    proving that "two agreeing weak fields is enough" has to be refused outright:
      G03 -- a household mailbox. Email, phone, address and surname agree, and the
             mother's card is on the son's account too, so ALL FIVE signals agree -- on a
             mother born 1971 and her son born 2004. Promoting email to proof moves her
             paid-off advance onto a different human being.
      G04 -- a shared accounts-receivable inbox and one switchboard number: two
             coworkers, plus one company card, so email, phone, address and fingerprint
             all agree.
      G05 -- roommates. Phone and address agree; the names do not.
      G06 -- Sr / Jr. The highest suggestive agreement in the fixture, and C1503 is
             funded, so the wrong merge is a money-movement error.

    Card fingerprint is in this tier and not the proof tier for a measured reason: of the
    three fingerprints shared across customers in 02d_seed_cards.sql, only ONE (fp_a1,
    G01's genuine duplicate) belongs to one person. The other two are G03 and G04.
    """
    agreeing: set[str] = set()

    if _agrees(normalize_email(a.email), normalize_email(b.email)):
        agreeing.add("email")
    if _agrees(normalize_phone(a.phone), normalize_phone(b.phone)):
        agreeing.add("phone")
    # address_line1 only, through normalize_name -- the one normalizer whose contract
    # (casefold, strip diacritics, collapse whitespace, PRESERVE hyphens) suits a
    # free-text address line. There is deliberately no address normalizer: folding
    # 'St' into 'Street' invents a canonical form the source never held. City, state and
    # postal are not folded in either, because a coarser signal in a tier that cannot
    # merge costs an annotation and never a wrong merge -- and '1 Test St' is shared by
    # all 16 synthetic accounts, which is one more reason exclusion runs first.
    if _agrees(normalize_name(a.address_line1), normalize_name(b.address_line1)):
        agreeing.add("address")
    if _agrees(normalize_name(a.last_name), normalize_name(b.last_name)):
        agreeing.add("surname")
    if _instruments(cards_a) & _instruments(cards_b):
        agreeing.add("card_fingerprint")

    return tuple(name for name in _SUGGESTIVE_ORDER if name in agreeing)


# ─────────────────────────────────────────────────────────────────────────────────────
# A5 -- survivorship.
# ─────────────────────────────────────────────────────────────────────────────────────

def choose_survivor(members: Sequence[CustomerFields],
                    money_moved_ids: AbstractSet[int]) -> tuple[int, str]:
    """Return (survivor_customer_id, reason).

    exactly one money-moved member -> that member, reason 'money_moved'
    zero money-moved members       -> freshest by COALESCE(updated_at, created_at),
                                      tie-break lowest customer_id, reason 'freshest'
    two or more money-moved        -> ValueError. The caller must have refused already.

    MONEY BEATS FRESHNESS, which is the whole of G01: C1041 is the older record and holds
    the paid-off advance, C4788 looks fresher, and "most recent record wins" therefore
    loses the money.

    NO FIELD-LEVEL COALESCING. The survivor's row is taken whole. G12's survivor C2411
    carries a MALFORMED 8-digit phone -- the seeded value is '(555) 012-33', which the
    normalizer reports as defect_class 'digit_count_8' -- while the loser C0338 had a valid
    one; that shows up as a non-zero printed count rather than a hidden repair, because a
    warehouse that stitches a 'best' record out of several rows produces a customer who
    never existed.
    """
    if not members:
        raise ValueError(
            "choose_survivor was given an empty group; there is nothing to survive."
        )

    moved = sorted(
        (m for m in members if m.customer_id in money_moved_ids),
        key=lambda m: m.customer_id,
    )

    if len(moved) > 1:
        # Not a fallback and not a warning. Two money-moved members is either one person
        # with two funding instruments or a first-party fraud signal, and both answers
        # belong to a human -- so this raises rather than picking one and recording a
        # reason that would read as a decision. resolve.py step 5 refuses first.
        ids = ", ".join(str(m.customer_id) for m in moved)
        raise ValueError(
            f"refusing to choose a survivor for a group with {len(moved)} money-moved "
            f"members ({ids}); that group must be routed to review before survivorship "
            f"is asked about at all."
        )

    if len(moved) == 1:
        return moved[0].customer_id, "money_moved"

    # Freshest wins, lowest customer_id breaks the tie. Ordering on the tuple rather
    # than on a timestamp() call: these datetimes are naive, and timestamp() on a naive
    # datetime is interpreted in local time and raises outright for pre-epoch values on
    # Windows -- a platform-dependent survivor is not a survivorship rule.
    freshest = max(members, key=lambda m: (m.freshness, -m.customer_id))
    return freshest.customer_id, "freshest"
