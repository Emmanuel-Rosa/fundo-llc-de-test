"""Identity-rule tests -- plain asserts, no test framework.

Run:  python -m tests.test_rules

THE FAILURE THIS FILE PREVENTS: a rule test written over invented inputs. That test
passes forever while the fixture underneath it moves, so it certifies the rules against
data that no longer exists -- and the rules are the only thing standing between a
duplicate group and a merged borrowing history. Every input below is copied out of
sql/source/02a_seed_customers.sql or sql/source/02c_seed_advances.sql with its exhibit
named, so a seed edit that moves an exhibit breaks a check here instead of quietly
re-scoring the run. tests/test_normalize.py says the same thing about the normalizer.

This is the file pipeline/identity/rules.py cites (rules.py:12-15) as the proof that its
whole rule surface is testable on a bare interpreter with no container running. The only
imports are `.rules` and `.normalize`, so there is no duckdb, no pymssql and no database
handle anywhere below -- and that claim is CHECKED at the end rather than asserted in
prose.

No pytest, deliberately, matching tests/test_normalize.py: two test files do not justify
a dependency the reviewer has to install.

THREE CHECKS BELOW FAIL ON PURPOSE. They are the two open review findings against
rules.py's prose, and they are marked OPEN FINDING where they sit. A finding with no
failing check is a finding that gets closed by editing a comment.
"""

from __future__ import annotations

import inspect
import sys
from datetime import date, datetime
from typing import Any

from pipeline.identity import rules
from pipeline.identity.normalize import normalize_phone
from pipeline.identity.rules import (
    MONEY_MOVED_MEANINGS,
    AdvanceFields,
    CardFields,
    CustomerFields,
    ProofKey,
    choose_survivor,
    classify_test_account,
    money_moved,
    money_moved_evidence,
    naive_test_filter,
    proof_key,
    status_meaning,
    suggestive_signals,
)

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


# ─────────────────────────────────────────────────────────────────────────────────────
# Row builders.
#
# The seed rows are transcribed once, at module level, and reused. Written as builders
# rather than as warehouse dicts because a dict typo (`ssn_last` for `ssn_last4`) would
# make from_row raise here, in a test, instead of in a run -- which is the trade
# CustomerFields.from_row itself makes and says so.
# ─────────────────────────────────────────────────────────────────────────────────────

_ABSENT: dict[str, Any] = dict.fromkeys(
    ("first_name", "last_name", "email", "phone", "ssn_last4", "date_of_birth",
     "address_line1", "city", "state_code", "postal_code", "employer_name",
     "signup_channel", "created_at", "updated_at")
)


def cust(customer_id: int, **fields: Any) -> CustomerFields:
    """One seeded customer, every value copied verbatim from 02a_seed_customers.sql.

    A field a row does not name is None, and that is NOT a stand-in for a seeded value:
    city, state_code, postal_code, employer_name and signup_channel are read by no
    function in rules.py at all -- suggestive_signals folds address_line1 only, and
    states why -- so supplying them would imply a rule consults them. created_at and
    updated_at are supplied for exactly the rows whose survivorship is under test.

    An unknown keyword raises TypeError, so a renamed column cannot be absorbed here.
    """
    return CustomerFields(customer_id=customer_id, **{**_ABSENT, **fields})


def adv(advance_id: int, customer_id: int, status: str | None,
        principal: float | None = None, instrument: str | None = None) -> AdvanceFields:
    """One seeded advance, reduced to the columns the identity rules may read.

    funded_at and paid_off_at are None deliberately. money_moved reads `status` and
    nothing else and names that seam in its own docstring; populating those columns here
    would suggest the veto corroborates against them, which is the separately-scoped
    change rules.py declines to make.
    """
    return AdvanceFields(advance_id, customer_id, status, principal, None, None, instrument)


def ts(stamp: str) -> datetime:
    """02a writes its timestamps as ISO-8601 literals, so they are copied as written."""
    return datetime.fromisoformat(stamp)


# REPLICATE('a', 64) and friends -- 02c writes the funding instrument that way. The hash
# is compared and printed WHOLE: money_moved_evidence exists to answer "do these two
# funded members repay from the same account", and a truncated hash cannot answer it.
INSTRUMENT_A = "a" * 64      # G01, C1041's paid-off advance
INSTRUMENT_9 = "9" * 64      # G02a, BOTH members -> one person with two records
INSTRUMENT_1 = "1" * 64      # G02b, C1298
INSTRUMENT_2 = "2" * 64      # G02b, C4903 -- different account, overlapping windows
INSTRUMENT_3 = "3" * 64      # G03, the mother's paid-off advance
INSTRUMENT_6 = "6" * 64      # G06, Harold Senior
INSTRUMENT_D = "d" * 64      # G13, C1777
INSTRUMENT_E = "e" * 64      # G14, Priya
INSTRUMENT_F = "f" * 64      # G15, Marcus Testerman
INSTRUMENT_0 = "0" * 64      # the two canonical-artifact advances


# ─────────────────────────────────────────────────────────────────────────────────────
# SECTION 1 of 02a -- the 38 hand-built exhibits, in the file's own order.
# ─────────────────────────────────────────────────────────────────────────────────────

# G01 -- same person; "most recent record wins" loses the money.
C1041 = cust(1041, first_name="Alicia", last_name="Moreau",
             email="alicia.moreau@gmail.com", phone="+15125550142",
             ssn_last4="3392", date_of_birth=date(1989, 3, 14),
             address_line1="901 Rio Grande St",
             created_at=ts("2023-04-11T09:22:14"), updated_at=ts("2025-11-02T16:04:00"))
C4788 = cust(4788, first_name="Alicia", last_name="Moreau",
             email="a.moreau+new@gmail.com", phone="+15125550142",
             ssn_last4="3392", date_of_birth=date(1989, 3, 14),
             address_line1="901 Rio Grande St",
             created_at=ts("2026-05-30T11:41:02"), updated_at=ts("2026-08-16T08:15:33"))

# G02a -- two funded members, SAME instrument.
C0777 = cust(777, first_name="Marcus", last_name="Adeyemi",
             email="marcus.adeyemi@gmail.com", phone="+14045550190",
             ssn_last4="8130", date_of_birth=date(1994, 7, 22),
             address_line1="55 Peachtree Pl",
             created_at=ts("2024-02-09T13:10:00"), updated_at=ts("2026-07-30T09:00:00"))
C3512 = cust(3512, first_name="Marcus", last_name="Adeyemi",
             email="m.adeyemi88@yahoo.com", phone="+14045550191",
             ssn_last4="8130", date_of_birth=date(1994, 7, 22),
             address_line1="55 Peachtree Pl",
             created_at=ts("2026-01-15T10:05:00"), updated_at=ts("2026-08-05T14:22:00"))

# G02b -- two funded members, DIFFERENT instruments, overlapping windows.
C1298 = cust(1298, first_name="Priyanka", last_name="Raghavan",
             email="priyanka.r@gmail.com", phone="+15125550233",
             ssn_last4="5501", date_of_birth=date(1990, 12, 1),
             address_line1="404 Congress Ave",
             created_at=ts("2024-06-01T08:00:00"), updated_at=ts("2026-08-01T12:00:00"))
C4903 = cust(4903, first_name="Priyanka", last_name="Raghavan",
             email="p.raghavan@outlook.com", phone="+12145550233",
             ssn_last4="5501", date_of_birth=date(1990, 12, 1),
             address_line1="1200 Elm St",
             created_at=ts("2026-06-20T15:30:00"), updated_at=ts("2026-08-06T09:45:00"))

# G03 -- household mailbox. Mother born 1971, son born 2004.
C2044 = cust(2044, first_name="Denise", last_name="Kowalczyk",
             email="kowalczyk.home@gmail.com", phone="+14045550188",
             ssn_last4="6612", date_of_birth=date(1971, 5, 9),
             address_line1="118 Larkspur Ln")
C2045 = cust(2045, first_name="Tomasz", last_name="Kowalczyk",
             email="kowalczyk.home@gmail.com", phone="+14045550188",
             ssn_last4="4487", date_of_birth=date(2004, 2, 17),
             address_line1="118 Larkspur Ln")

# G04 -- shared accounts-receivable inbox and one switchboard number.
C3311 = cust(3311, first_name="Naomi", last_name="Fletcher",
             email="ar@brightpath-staffing.com", phone="+12065550110",
             ssn_last4="7781", date_of_birth=date(1986, 11, 23),
             address_line1="2200 1st Ave")
C3312 = cust(3312, first_name="Curtis", last_name="Vale",
             email="ar@brightpath-staffing.com", phone="+12065550110",
             ssn_last4="2094", date_of_birth=date(1979, 4, 8),
             address_line1="2200 1st Ave")

# G05 -- roommates. Phone and address agree; the names do not.
C0912 = cust(912, first_name="Ibrahim", last_name="Sow",
             email="ibrahim.sow@gmail.com", phone="+13125550177",
             ssn_last4="4410", date_of_birth=date(1995, 6, 30),
             address_line1="44 Delaney St Apt 3")
C4650 = cust(4650, first_name="Renata", last_name="Villalobos",
             email="renata.v@yahoo.com", phone="+13125550177",
             ssn_last4="9963", date_of_birth=date(1992, 10, 12),
             address_line1="44 Delaney St Apt 3")

# G06 -- Sr / Jr, the highest suggestive agreement in the fixture. C1503 is funded.
C1503 = cust(1503, first_name="Harold", last_name="Whitfield",
             email="harold.whitfield@outlook.com", phone="+19195550123",
             ssn_last4="2210", date_of_birth=date(1958, 8, 30),
             address_line1="7 Cedar Bluff")
C4102 = cust(4102, first_name="Harold", last_name="Whitfield",
             email="harold.whitfield.jr@outlook.com", phone="+19195550123",
             ssn_last4="7745", date_of_birth=date(1986, 4, 3),
             address_line1="7 Cedar Bluff")

# G07 -- NULL ssn, and C0203 is also a NULL-updated_at legacy row: missed twice by a
# naive design.
C0203 = cust(203, first_name="Elena", last_name="Vasquez",
             email="elena.vasquez@yahoo.com", phone="+16025550144",
             ssn_last4=None, date_of_birth=date(1983, 9, 27),
             address_line1="88 Roosevelt St",
             created_at=ts("2018-03-12T10:15:00"), updated_at=None)
C3844 = cust(3844, first_name="Elena", last_name="Vasquez",
             email="elena.vasquez@yahoo.com", phone="+16025550145",
             ssn_last4="9014", date_of_birth=date(1983, 9, 27),
             address_line1="88 Roosevelt St",
             created_at=ts("2025-08-08T13:00:00"), updated_at=ts("2026-05-19T13:00:00"))

# G08 -- transposed dob entered at a call centre.
C1120 = cust(1120, first_name="Kwame", last_name="Boateng",
             email="kwame.boateng@gmail.com", phone="+17705550166",
             ssn_last4="3376", date_of_birth=date(1987, 4, 11),
             address_line1="19 Ponce Way")
C2957 = cust(2957, first_name="Kwame", last_name="Boateng",
             email="kwame.boateng@gmail.com", phone="+17705550166",
             ssn_last4="3376", date_of_birth=date(1987, 11, 4),
             address_line1="19 Ponce Way")

# G09 -- surname change on marriage. THE MEASURED COST of surname-in-the-tuple.
C2680 = cust(2680, first_name="Hanna", last_name="Nowak",
             email="hanna.nowak@gmail.com", phone="+16025550101",
             ssn_last4="1188", date_of_birth=date(1992, 1, 19),
             address_line1="250 Camelback Rd")
C4471 = cust(4471, first_name="Hanna", last_name="Nowak-Brennan",
             email="h.nowak@gmail.com", phone="+16025550101",
             ssn_last4="1188", date_of_birth=date(1992, 1, 19),
             address_line1="250 Camelback Rd")

# G10 -- chance collision on (ssn_last4, dob). Two unrelated people, different states.
C0654 = cust(654, first_name="Diego", last_name="Ferraro",
             email="diego.ferraro@icloud.com", phone="+13055550171",
             ssn_last4="4417", date_of_birth=date(1991, 6, 2),
             address_line1="700 Brickell Ave")
C3078 = cust(3078, first_name="Amara", last_name="Okonkwo",
             email="amara.okonkwo@gmail.com", phone="+12065550172",
             ssn_last4="4417", date_of_birth=date(1991, 6, 2),
             address_line1="315 Pine St")

# G11 -- NULL ssn plus a nickname. C1866 is the other NULL-updated_at legacy row.
C1866 = cust(1866, first_name="Robert", last_name="Ellison",
             email="r.ellison@comcast.net", phone="+16175550199",
             ssn_last4=None, date_of_birth=date(1978, 10, 5),
             address_line1="12 Beacon St",
             created_at=ts("2018-07-22T14:00:00"), updated_at=None)
C4290 = cust(4290, first_name="Bobby", last_name="Ellison",
             email="bobby.ellison@gmail.com", phone="+16175550199",
             ssn_last4="5528", date_of_birth=date(1978, 10, 5),
             address_line1="12 Beacon St",
             created_at=ts("2026-02-14T10:30:00"), updated_at=ts("2026-02-14T10:30:00"))

# G12 -- the triple-LOOKING group. C4855's ssn is a placeholder, and the survivor
# C2411 is the member carrying the malformed phone.
C0338 = cust(338, first_name="Yusuf", last_name="Karim",
             email="yusuf.karim@gmail.com", phone="+13035550155",
             ssn_last4="7723", date_of_birth=date(1996, 11, 30),
             address_line1="1600 Blake St",
             created_at=ts("2024-08-19T09:00:00"), updated_at=ts("2026-06-01T09:00:00"))
C2411 = cust(2411, first_name="Yusuf", last_name="Karim",
             email="y.karim@outlook.com", phone="(555) 012-33",
             ssn_last4="7723", date_of_birth=date(1996, 11, 30),
             address_line1="1600 Blake St",
             created_at=ts("2025-10-02T15:00:00"), updated_at=ts("2026-08-10T15:00:00"))
C4855 = cust(4855, first_name="Yousuf", last_name="Karim",
             email="yousuf.k@yahoo.com", phone="+13035550156",
             ssn_last4="0000", date_of_birth=date(1996, 11, 30),
             address_line1="2 Larimer Sq",
             created_at=ts("2026-05-11T12:00:00"), updated_at=ts("2026-07-15T12:00:00"))

# G13 -- the group that acquires a second money-moved member on churn day 2.
C0021 = cust(21, first_name="Marisol", last_name="Duarte",
             email="marisol.duarte@gmail.com", phone="+15035550120",
             ssn_last4="6640", date_of_birth=date(1985, 2, 25),
             address_line1="800 SW 6th Ave",
             created_at=ts("2021-05-04T08:00:00"), updated_at=ts("2024-09-09T08:00:00"))
C1777 = cust(1777, first_name="Marisol", last_name="Duarte",
             email="m.duarte@yahoo.com", phone="+15035550121",
             ssn_last4="6640", date_of_birth=date(1985, 2, 25),
             address_line1="800 SW 6th Ave",
             created_at=ts("2024-04-14T10:00:00"), updated_at=ts("2026-07-20T10:00:00"))
C3260 = cust(3260, first_name="Marisol", last_name="Duarte",
             email="marisol.d@outlook.com", phone="+15035550122",
             ssn_last4="6640", date_of_birth=date(1985, 2, 25),
             address_line1="800 SW 6th Ave",
             created_at=ts("2025-12-19T16:00:00"), updated_at=ts("2026-01-30T16:00:00"))

# G14 -- a real staff member on the internal domain.
C0088 = cust(88, first_name="Priya", last_name="Nadkarni",
             email="priya.n@fundo.com", phone="+15125550188",
             ssn_last4="4471", date_of_birth=date(1993, 8, 8),
             address_line1="110 Guadalupe St",
             created_at=ts("2023-01-16T09:00:00"), updated_at=ts("2026-05-05T09:00:00"))

# G15 + G15b -- the seven real people the naive '%test%' pattern flags.
C0402 = cust(402, first_name="Marcus", last_name="Testerman",
             email="marcus.testerman@gmail.com", phone="+16145550177",
             ssn_last4="8890", date_of_birth=date(1990, 2, 19),
             address_line1="400 High St")
C1188 = cust(1188, first_name="Greta", last_name="Lindqvist",
             email="greatest.deals@hotmail.com", phone="+12065550301",
             ssn_last4="5512", date_of_birth=date(1988, 7, 4),
             address_line1="99 Union St")
C2733 = cust(2733, first_name="Owen", last_name="Barlowe",
             email="protest.organizer@riseup.net", phone="+13035550302",
             ssn_last4="6603", date_of_birth=date(1996, 1, 28),
             address_line1="20 Wynkoop St")
C3560 = cust(3560, first_name="Farida", last_name="Aziz",
             email="contest.winner1994@yahoo.com", phone="+13125550303",
             ssn_last4="7714", date_of_birth=date(1994, 5, 16),
             address_line1="77 Wacker Dr")
C4318 = cust(4318, first_name="Neel", last_name="Varma",
             email="latest.news@gmail.com", phone="+16175550304",
             ssn_last4="8825", date_of_birth=date(1991, 11, 9),
             address_line1="5 Newbury St")
C1902 = cust(1902, first_name="Dana", last_name="Tester",
             email="dana.tester@gmail.com", phone="+19195550305",
             ssn_last4="3341", date_of_birth=date(1985, 9, 2),
             address_line1="31 Hillsborough St")
C4501 = cust(4501, first_name="Gio", last_name="Testani",
             email="gio.testani@icloud.com", phone="+13055550306",
             ssn_last4="9902", date_of_birth=date(1997, 3, 21),
             address_line1="1 Ocean Dr")

EXHIBITS: tuple[CustomerFields, ...] = (
    C1041, C4788, C0777, C3512, C1298, C4903, C2044, C2045, C3311, C3312,
    C0912, C4650, C1503, C4102, C0203, C3844, C1120, C2957, C2680, C4471,
    C0654, C3078, C1866, C4290, C0338, C2411, C4855, C0021, C1777, C3260,
    C0088, C0402, C1188, C2733, C3560, C4318, C1902, C4501,
)

# 02a:98 calls section 1 "these 38 rows". If this count moves, an exhibit was added or
# dropped in the seed and every derived figure below is being measured over a different
# population than the one the write-up describes.
check("all 38 hand-built exhibits are transcribed", len(EXHIBITS), 38)


# ─────────────────────────────────────────────────────────────────────────────────────
# SECTION 2 of 02a -- the 16 synthetic accounts, with the rule that must catch each.
#
# Chosen so no single signal covers them all. The expected `rules` tuple is the whole
# point of the table: "is_test is True" would pass even if one rule were doing all the
# work and the other two were dead code.
# ─────────────────────────────────────────────────────────────────────────────────────

SYNTHETIC_ADDRESS = "1 Test St"       # shared by all 16 -- one more reason exclusion
                                      # runs before resolution


def synth(customer_id: int, first_name: str, last_name: str, email: str,
          ssn_last4: str = "1234", dob: date = date(1990, 1, 1)) -> CustomerFields:
    """One of the 16 synthetic accounts. ssn '1234' and dob 1990-01-01 are what 14 of
    the 16 carry; the two canonical-artifact rows override both."""
    return cust(customer_id, first_name=first_name, last_name=last_name, email=email,
                ssn_last4=ssn_last4, date_of_birth=dob,
                address_line1=SYNTHETIC_ADDRESS)


C4960 = synth(4960, "Test", "One", "test@fundo.com")
C4961 = synth(4961, "Test", "Two", "test1@fundo.com")
C4962 = synth(4962, "Test", "Three", "test.2@fundo.com")
C4963 = synth(4963, "Test", "Four", "test_03@fundo.com")
C4964 = synth(4964, "Test", "Five", "test-04@fundo.com")
C4965 = synth(4965, "QA", "Account", "qa@fundo.com")
C4966 = synth(4966, "QA", "Seventeen", "qa.17@fundo.com")
C4967 = synth(4967, "Demo", "Account", "demo@fundo.com")
C4968 = synth(4968, "Dev", "One", "dev1@fundo.com")
C4969 = synth(4969, "Staging", "Account", "staging@fundo.com")
C4970 = synth(4970, "Automation", "Rig", "automation@fundo.com")
C4971 = synth(4971, "Rebecca", "Chan", "rebecca.chan+test@fundo.com")
C4972 = synth(4972, "Rebecca", "Chan", "rebecca.chan+qa2@fundo.com")
C4973 = synth(4973, "Nina", "Rowe", "nina.rowe@fundo.com",
              ssn_last4="0000", dob=date(1900, 1, 1))
C4974 = synth(4974, "Carl", "Smith", "carl.smith@example.com",
              ssn_last4="0000", dob=date(1900, 1, 1))
C4975 = synth(4975, "TEST", "UPPER", "TEST@FUNDO.COM")

SYNTHETIC_RULES: tuple[tuple[CustomerFields, tuple[str, ...]], ...] = (
    (C4960, ("A",)),   # local part 'test'
    (C4961, ("A",)),   # 'test1'   -- digits, no separator
    (C4962, ("A",)),   # 'test.2'  -- dot separator
    (C4963, ("A",)),   # 'test_03' -- underscore separator
    (C4964, ("A",)),   # 'test-04' -- hyphen separator
    (C4965, ("A",)),   # 'qa'      -- invisible to the naive %test% pattern from here on
    (C4966, ("A",)),   # 'qa.17'
    (C4967, ("A",)),   # 'demo'
    (C4968, ("A",)),   # 'dev1'
    (C4969, ("A",)),   # 'staging'
    (C4970, ("A",)),   # 'automation'
    (C4971, ("B",)),   # ONLY B: the local part is a real employee name
    (C4972, ("B",)),   # ONLY B: '+qa2'
    (C4973, ("C",)),   # ONLY C: artifact ssn/dob, and an artifact principal
    (C4974, ("C",)),   # ONLY C: not even on the internal domain
    (C4975, ("A",)),   # A only after casefolding -- not because a collation is lenient
)
SYNTHETICS: tuple[CustomerFields, ...] = tuple(row for row, _ in SYNTHETIC_RULES)
SYNTHETIC_IDS = frozenset(row.customer_id for row in SYNTHETICS)

check("all 16 synthetic accounts are transcribed", len(SYNTHETICS), 16)


# ─────────────────────────────────────────────────────────────────────────────────────
# The hand-built advances from 02c. Every one is cited by an identity exhibit.
# ─────────────────────────────────────────────────────────────────────────────────────

A2210 = adv(2210, 1041, "Paid Off", 300.00, INSTRUMENT_A)   # G01
A0455 = adv(455, 777, "FUNDED ", 250.00, INSTRUMENT_9)      # G02a, trailing space
A6031 = adv(6031, 3512, "funded", 350.00, INSTRUMENT_9)     # G02a, same instrument
A1877 = adv(1877, 1298, "Funded", 400.00, INSTRUMENT_1)     # G02b
A7402 = adv(7402, 4903, "funded", 500.00, INSTRUMENT_2)     # G02b, different instrument
A3390 = adv(3390, 2044, "Paid Off", 200.00, INSTRUMENT_3)   # G03, the mother's
A5510 = adv(5510, 1503, "funded", 275.00, INSTRUMENT_6)     # G06, Harold Senior
A4408 = adv(4408, 1777, "funded", 425.00, INSTRUMENT_D)     # G13
A0091 = adv(91, 88, "Paid Off", 250.00, INSTRUMENT_E)       # G14, Priya
A0403 = adv(403, 402, "FUNDED ", 325.00, INSTRUMENT_F)      # G15, Testerman
A7990 = adv(7990, 4973, "funded", 0.00, INSTRUMENT_0)       # rule C, principal 0.00
A7991 = adv(7991, 4974, "funded", 999999.99, INSTRUMENT_0)  # rule C, 999999.99
A7992 = adv(7992, 1188, "pending", 150.00, None)            # trailing-space exhibit
A7993 = adv(7993, 2733, "approved", 175.00, None)
A7994 = adv(7994, 3560, "declined", 125.00, None)


# ─────────────────────────────────────────────────────────────────────────────────────
# A1 -- the status vocabulary and the funded veto.
#
# The 15 raw spellings and the meaning each carries, copied verbatim from the #status
# table (02c:159-174). That table is the source of truth for this map, so it is
# transcribed rather than paraphrased: 'FUNDED ' keeps its trailing space here because
# it has one in the database.
# ─────────────────────────────────────────────────────────────────────────────────────

STATUS_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("funded", "funded"),
    ("FUNDED ", "funded"),
    ("Funded", "funded"),
    ("paid_off", "paid_off"),
    ("Paid Off", "paid_off"),
    ("PAID OFF", "paid_off"),
    ("pending", "pending"),
    ("Pending", "pending"),
    ("approved", "approved"),
    ("APPROVED", "approved"),
    ("declined", "declined"),
    ("Declined", "declined"),
    ("cancelled", "cancelled"),
    ("canceled", "cancelled"),
    ("expired", "expired"),
)

check("15 raw spellings, no more and no fewer", len(STATUS_SPELLINGS), 15)
check("7 meanings behind them", len({meaning for _, meaning in STATUS_SPELLINGS}), 7)

for raw, meaning in STATUS_SPELLINGS:
    check(f"status {raw!r} means {meaning}", status_meaning(raw), meaning)

# The trailing space, called out on its own because it is the spelling that defeats an
# exact-match predicate without looking like it should.
check("'FUNDED ' keeps its trailing space in this file", STATUS_SPELLINGS[1][0], "FUNDED ")
check("'FUNDED ' with a trailing space is funded", status_meaning("FUNDED "), "funded")
check("'paid_off' and 'Paid Off' are one meaning",
      status_meaning("paid_off") == status_meaning("Paid Off"), True)
check("'canceled' and 'cancelled' are one meaning",
      status_meaning("canceled") == status_meaning("cancelled"), True)

# How much work the vocabulary actually needs, COUNTED rather than quoted. rules.py:195
# and rules.py:219-220 both say "only 6 of the 15 ... the other 9"; counting the table
# gives 7 spellings already identical to their meaning and 8 needing a transformation.
# Reported as a finding rather than reconciled by picking a different definition -- under
# every reading (raw == meaning, raw == a map key, raw unchanged by status_meaning) the
# answer is 7.
identical = tuple(raw for raw, meaning in STATUS_SPELLINGS if raw == meaning)
check("spellings already identical to their meaning", len(identical), 7)
check("spellings needing casefold, trim, separator folding or an alias",
      len(STATUS_SPELLINGS) - len(identical), 8)
check("and status_meaning is a no-op on exactly those",
      tuple(raw for raw, _ in STATUS_SPELLINGS if status_meaning(raw) == raw), identical)

# A status outside the vocabulary is unrecognised, and that is a FIRST-CLASS answer. No
# fixture row holds this spelling -- there is no CHECK constraint on the column, so it
# stands for the sixteenth spelling rules.py's docstring says must not fold into "not
# funded".
check("an unknown spelling is unrecognised", status_meaning("charged_off"), "unrecognised")
check("an unknown spelling is NOT silently not-funded",
      money_moved([adv(7995, 1188, "charged_off")]), False)
check("...and unrecognised is not in the money-moved set",
      "unrecognised" in MONEY_MOVED_MEANINGS, False)
check("NULL status is missing", status_meaning(None), "missing")
check("blank status is missing", status_meaning(""), "missing")
check("whitespace-only status is missing", status_meaning("   "), "missing")

# money_moved over the whole vocabulary. advance_id and customer_id are neutral here and
# named as such: these spellings are dealt out to the bulk rows, whose advance_id is a
# function of the row ordinal (02c:202-245), so most of them cannot be pinned to a single
# citable row. The SPELLINGS are verbatim; only the ids are stand-ins.
MONEY_MOVED_SPELLINGS = tuple(
    raw for raw, meaning in STATUS_SPELLINGS if meaning in MONEY_MOVED_MEANINGS
)
check("exactly six of the 15 spellings moved money", len(MONEY_MOVED_SPELLINGS), 6)
for raw, meaning in STATUS_SPELLINGS:
    check(f"money_moved for {raw!r}",
          money_moved([adv(0, 0, raw)]), meaning in MONEY_MOVED_MEANINGS)

# The same veto over the real rows, where the ids ARE citable.
check("G01: C1041's paid-off advance moved money", money_moved([A2210]), True)
check("G02a: C0777's 'FUNDED ' advance moved money", money_moved([A0455]), True)
check("G02a: C3512's 'funded' advance moved money", money_moved([A6031]), True)
check("G02b: C1298's 'Funded' advance moved money", money_moved([A1877]), True)
check("G14: Priya's paid-off advance moved money", money_moved([A0091]), True)
check("G15: Testerman's advance moved money", money_moved([A0403]), True)
check("a pending advance did not", money_moved([A7992]), False)
check("an approved advance did not", money_moved([A7993]), False)
check("a declined advance did not", money_moved([A7994]), False)
check("no advances at all is not money moved", money_moved([]), False)
check("a NULL status is not money moved", money_moved([adv(7995, 1188, None)]), False)

# The evidence lines. Ordered by advance id so a recurring refusal reads identically on
# every run, and the instrument printed whole so a reviewer can compare the two sides.
check("G02a evidence: both members on the same account",
      money_moved_evidence([A6031, A0455]),
      (f"advance 455 funded instrument {INSTRUMENT_9}",
       f"advance 6031 funded instrument {INSTRUMENT_9}"))
check("G02b evidence: two different accounts, printed whole",
      money_moved_evidence([A7402, A1877]),
      (f"advance 1877 funded instrument {INSTRUMENT_1}",
       f"advance 7402 funded instrument {INSTRUMENT_2}"))
check("G14 evidence names the meaning, not the raw spelling",
      money_moved_evidence([A0091]),
      (f"advance 91 paid_off instrument {INSTRUMENT_E}",))
check("a pending advance contributes no evidence line",
      money_moved_evidence([A7992]), ())


# ─────────────────────────────────────────────────────────────────────────────────────
# A2 -- test-account classification. Three rules, and money outranks all three.
# ─────────────────────────────────────────────────────────────────────────────────────

# The two canonical-artifact rows carry funded advances, so they are the money-precedence
# exhibits and are checked separately below.
SYNTHETIC_ADVANCES: dict[int, list[AdvanceFields]] = {4973: [A7990], 4974: [A7991]}

for row, expected_rules in SYNTHETIC_RULES:
    verdict = classify_test_account(row, [])
    check(f"C{row.customer_id} is caught by rule(s) {'/'.join(expected_rules)}",
          verdict.rules, expected_rules)
    check(f"C{row.customer_id} is a test account", verdict.is_test, True)
    check(f"C{row.customer_id} is not blocked by money", verdict.blocked_by_money, False)
    check(f"C{row.customer_id}'s detail is printable ASCII",
          verdict.detail.isascii() and verdict.detail != "", True)

# Rule A's two halves, each shown to be load-bearing on a real row. C4974 is on
# example.com, so the domain half fails and only rule C can see it; C0088 is on the
# internal domain and the local-part half is the only thing keeping her in the book.
check("rule A needs the internal domain: C4974 is not on it",
      C4974.email.endswith("@fundo.com"), False)
check("rule A needs the local part: C0088 IS on the internal domain",
      C0088.email.endswith("@fundo.com"), True)
check("C4975 is caught after casefolding, not by a lenient collation",
      classify_test_account(C4975, []).rules, ("A",))
check("C4975's raw email is still uppercase in the source", C4975.email, "TEST@FUNDO.COM")

# Rule B, and the real customer it must NOT fire on. C4788's 'a.moreau+new@gmail.com' is
# a plus tag on a real person holding a real card; a rule reading "has a plus tag" would
# excise her.
check("C4971 is caught only by the +test tag", classify_test_account(C4971, []).rules, ("B",))
check("C4972's '+qa2' tag also fires B", classify_test_account(C4972, []).rules, ("B",))
check("G01's C4788 has a plus tag and is NOT a test account",
      classify_test_account(C4788, []).is_test, False)
check("...and no rule fired on her at all", classify_test_account(C4788, []).rules, ())

# Rule C, both halves. The ssn/dob half fires before any advance is read; the artifact
# principal shows up in the detail for the reviewer.
c4973_artifact_only = classify_test_account(C4973, [])
check("rule C fires on the artifact ssn/dob pair with no advance at all",
      c4973_artifact_only.rules, ("C",))
check("...and names the pair", "ssn 0000 with dob 1900-01-01" in c4973_artifact_only.detail,
      True)
c4973 = classify_test_account(C4973, SYNTHETIC_ADVANCES[4973])
check("C4973's artifact principal is named too",
      "advance 7990 principal 0.00" in c4973.detail, True)
c4974 = classify_test_account(C4974, SYNTHETIC_ADVANCES[4974])
check("C4974's 999999.99 survives the FLOAT column exactly",
      "advance 7991 principal 999999.99" in c4974.detail, True)

# THE PRECEDENCE. Money outranks every test-data pattern, so these two are NOT excluded
# and the rules that fired stay listed for the caller to route to a human.
for row, verdict in ((C4973, c4973), (C4974, c4974)):
    check(f"C{row.customer_id} has moved money, so it is not auto-excluded",
          verdict.is_test, False)
    check(f"C{row.customer_id} is blocked by money", verdict.blocked_by_money, True)
    check(f"C{row.customer_id} still reports the rule that fired", verdict.rules, ("C",))
    check(f"C{row.customer_id}'s detail says why it was not excluded",
          "NOT EXCLUDED" in verdict.detail, True)
    check(f"C{row.customer_id}'s detail carries the funding evidence",
          f"instrument {INSTRUMENT_0}" in verdict.detail, True)
    check(f"C{row.customer_id}'s detail is ASCII", verdict.detail.isascii(), True)

# THE BLOCK IS GATED ON A RULE HAVING FIRED. C0402 has moved money and no rule fires, so
# he is an ordinary customer -- not a review row. Roughly two thirds of this book has
# moved money, so blocking on money alone would file a review queue nobody reads.
testerman = classify_test_account(C0402, [A0403])
check("C0402 has moved money", money_moved([A0403]), True)
check("C0402 is not a test account", testerman.is_test, False)
check("C0402 is NOT a review row: no rule fired", testerman.blocked_by_money, False)
check("C0402's detail says exactly that", testerman.detail, "no test-account rule fired")

# G14. The one the naive filter costs money on.
priya = classify_test_account(C0088, [A0091])
check("C0088 Priya Nadkarni is not a test account", priya.is_test, False)
check("no rule fires on C0088", priya.rules, ())
check("C0088 is not a review row either", priya.blocked_by_money, False)

# The seven real people the naive filter flags, with the advance each actually holds.
NAIVE_FALSE_POSITIVES: tuple[tuple[CustomerFields, list[AdvanceFields]], ...] = (
    (C0402, [A0403]),   # funded -- this filter deletes a funded advance
    (C1188, [A7992]),
    (C2733, [A7993]),
    (C3560, [A7994]),
    (C4318, []),
    (C1902, []),
    (C4501, []),
)
check("seven real people, as 02a:376 counts them", len(NAIVE_FALSE_POSITIVES), 7)
for row, advances in NAIVE_FALSE_POSITIVES:
    check(f"C{row.customer_id} is not a test account under the shipped rules",
          classify_test_account(row, advances).is_test, False)
    check(f"C{row.customer_id}: no rule fires",
          classify_test_account(row, advances).rules, ())
    check(f"C{row.customer_id} IS flagged by the naive filter", naive_test_filter(row), True)

check("the naive filter does not flag C0088", naive_test_filter(C0088), False)
check("the naive filter does not flag G01's C1041", naive_test_filter(C1041), False)

# The naive filter's reach over the synthetics: 7 of 16, and the 9 it misses are exactly
# the mailboxes that hold no 'test' substring.
naive_synthetic_hits = tuple(row.customer_id for row in SYNTHETICS if naive_test_filter(row))
check("the naive filter reaches 7 of the 16 synthetic accounts",
      naive_synthetic_hits, (4960, 4961, 4962, 4963, 4964, 4971, 4975))
check("...and misses the qa/demo/dev/staging/automation mailboxes",
      naive_test_filter(C4965) or naive_test_filter(C4967) or naive_test_filter(C4969)
      or naive_test_filter(C4970), False)

# address_line1 IS NOT MATCHED, which is rules.py's stated deviation from the contract's
# wording. All 16 synthetics share '1 Test St', so matching that field would flag all 16
# on one signal and RAISE this filter's precision above the 0.500 the write-up measured.
check("C4965's address does contain 'Test'", C4965.address_line1, SYNTHETIC_ADDRESS)
check("...and the naive filter still misses it, because it reads name and email only",
      naive_test_filter(C4965), False)

# The counterfactual's two numbers, DERIVED from the rows above rather than quoted.
ALL_SEEDED: tuple[CustomerFields, ...] = EXHIBITS + SYNTHETICS
flagged = tuple(row for row in ALL_SEEDED if naive_test_filter(row))
flagged_real = tuple(row for row in flagged if row.customer_id not in SYNTHETIC_IDS)
check("the naive filter flags 14 of these 54 rows", len(flagged), 14)
check("half of what it flags is a real person", len(flagged_real), 7)
check("naive precision on these rows is 0.500",
      (len(flagged) - len(flagged_real)) / len(flagged), 0.500)

# The shipped detector over the same population: a rule fires on all 16 synthetics and on
# no real person. 14 are excluded and 2 are routed to review, because money outranks the
# pattern -- so "catches all 16" and "excludes all 16" are different statements.
shipped = {
    row.customer_id: classify_test_account(row, SYNTHETIC_ADVANCES.get(row.customer_id, []))
    for row in ALL_SEEDED
}
check("a rule fires on all 16 synthetic accounts",
      len([cid for cid in SYNTHETIC_IDS if shipped[cid].rules]), 16)
check("no rule fires on any of the 38 real exhibits",
      [row.customer_id for row in EXHIBITS if shipped[row.customer_id].rules], [])
check("14 are excluded", len([v for v in shipped.values() if v.is_test]), 14)
check("2 are routed to review instead",
      sorted(cid for cid, v in shipped.items() if v.blocked_by_money), [4973, 4974])
check("shipped precision on these rows is 1.000",
      len([cid for cid, v in shipped.items() if v.is_test and cid in SYNTHETIC_IDS])
      / len([v for v in shipped.values() if v.is_test]), 1.000)


# ─────────────────────────────────────────────────────────────────────────────────────
# A3 -- the proof tuple. The only thing in rules.py that may cause a merge.
# ─────────────────────────────────────────────────────────────────────────────────────

check("G01: C1041's proof key", proof_key(C1041), ProofKey("3392", "1989-03-14", "moreau"))
check("G01: C4788 produces the SAME key", proof_key(C4788), proof_key(C1041))
check("G12: C0338 and C2411 produce the same key",
      proof_key(C0338), ProofKey("7723", "1996-11-30", "karim"))
check("G12: C2411's key equals C0338's", proof_key(C2411), proof_key(C0338))
check("a ProofKey is hashable, so it can key a group directly",
      len({proof_key(C0338), proof_key(C2411)}), 1)
check("G13: all three Marisol rows produce one key",
      len({proof_key(C0021), proof_key(C1777), proof_key(C3260)}), 1)
check("G13's key", proof_key(C1777), ProofKey("6640", "1985-02-25", "duarte"))

# G10 -- the chance collision. ssn and dob agree; surname is the only separator, and
# without it these two unrelated people merge.
check("G10: C0654 and C3078 share ssn_last4", C0654.ssn_last4, C3078.ssn_last4)
check("G10: ...and date_of_birth", C0654.date_of_birth, C3078.date_of_birth)
check("G10: C0654's key", proof_key(C0654), ProofKey("4417", "1991-06-02", "ferraro"))
check("G10: C3078's key", proof_key(C3078), ProofKey("4417", "1991-06-02", "okonkwo"))
check("G10: the keys DIFFER, so the collision cannot merge",
      proof_key(C0654) == proof_key(C3078), False)

# G09 -- the measured cost of that decision, on the other side of the trade.
check("G09: C2680's key", proof_key(C2680), ProofKey("1188", "1992-01-19", "nowak"))
check("G09: C4471's key", proof_key(C4471),
      ProofKey("1188", "1992-01-19", "nowak-brennan"))
check("G09: a surname change costs the merge",
      proof_key(C2680) == proof_key(C4471), False)

# G11 -- first_name is excluded from the tuple, so the nickname is not the blocker. The
# missing ssn on C1866 is.
check("G11: C4290 has a key", proof_key(C4290), ProofKey("5528", "1978-10-05", "ellison"))
check("G11: Robert and Bobby share the surname component",
      proof_key(C4290).last_name, "ellison")
check("G11: C1866's NULL ssn forms no key at all", proof_key(C1866), None)

# The is_evidence gate, on each contaminated value the fixture actually holds.
check("G12: C4855's ssn '0000' forms no key -- this is what makes G12 a pair",
      proof_key(C4855), None)
check("G07: C0203's NULL ssn forms no key", proof_key(C0203), None)
check("G07: C3844 does have one", proof_key(C3844),
      ProofKey("9014", "1983-09-27", "vasquez"))
check("the synthetics' ssn '1234' forms no key either", proof_key(C4960), None)
check("C4973's artifact ssn/dob forms no key", proof_key(C4973), None)
# A partial key is not a weaker key. In this fixture the dob placeholder 1900-01-01 never
# appears without ssn '0000' beside it, so C4973 fails on both components at once; the
# dob half is pinned in tests/test_normalize.py instead.
check("no synthetic account forms a proof key",
      [row.customer_id for row in SYNTHETICS if proof_key(row) is not None], [])

# G08 -- the transposition stays two keys, so the pair falls to review rather than
# merging on a comparator nobody built.
check("G08: C1120's key", proof_key(C1120), ProofKey("3376", "1987-04-11", "boateng"))
check("G08: C2957's transposed dob makes a different key",
      proof_key(C1120) == proof_key(C2957), False)

# The false-positive families all fail the proof tuple, which is the whole reason four
# agreeing weak signals are safe to report.
check("G03: mother and son have different keys",
      proof_key(C2044) == proof_key(C2045), False)
check("G04: two coworkers have different keys",
      proof_key(C3311) == proof_key(C3312), False)
check("G05: roommates have different keys",
      proof_key(C0912) == proof_key(C4650), False)
check("G06: Sr and Jr have different keys",
      proof_key(C1503) == proof_key(C4102), False)


# ─────────────────────────────────────────────────────────────────────────────────────
# A4 -- the suggestive tier. Annotates; merges nothing, at any count.
#
# Cards are from 02d_seed_cards.sql. Only the 12 hand-built rows share a fingerprint
# across customers, and two of those three shared instruments belong to different people.
# ─────────────────────────────────────────────────────────────────────────────────────

K9001 = CardFields(9001, 1041, "fp_a1", True)     # G01, the genuine duplicate instrument
K9002 = CardFields(9002, 4788, "fp_b2", True)
K9003 = CardFields(9003, 4788, "fp_a1", False)    # same instrument, second row, demoted
K9004 = CardFields(9004, 2044, "fp_kow", True)    # G03, the mother's card...
K9005 = CardFields(9005, 2045, "fp_kow", True)    # ...on her son's account
K9006 = CardFields(9006, 3311, "fp_bps", True)    # G04, one company card...
K9007 = CardFields(9007, 3312, "fp_bps", True)    # ...on two coworkers
K9008 = CardFields(9008, 21, "fp_md1", True)
K9009 = CardFields(9009, 1777, "fp_md2", True)
K9011 = CardFields(9011, 338, "fp_yk1", True)
K9012 = CardFields(9012, 2411, "fp_yk2", True)

# G03 -- the maximum. All five signals agree on a mother born 1971 and her son born 2004.
check("G03: all five weak signals agree",
      suggestive_signals(C2044, C2045, [K9004], [K9005]),
      ("email", "phone", "address", "surname", "card_fingerprint"))
check("G03: and the proof tuple still refuses",
      proof_key(C2044) == proof_key(C2045), False)

# G04 -- four signals, two coworkers.
check("G04: shared inbox, switchboard, office and company card",
      suggestive_signals(C3311, C3312, [K9006], [K9007]),
      ("email", "phone", "address", "card_fingerprint"))

# G05 -- the pair that kills "two agreeing weak fields is enough". Surname must be absent.
check("G05: roommates agree on phone and address only",
      suggestive_signals(C0912, C4650, [], []),
      ("phone", "address"))
check("G05: surname is NOT among the agreeing signals",
      "surname" in suggestive_signals(C0912, C4650, [], []), False)

# G06 -- Sr / Jr. No card either side.
check("G06: Sr and Jr agree on phone, address and surname",
      suggestive_signals(C1503, C4102, [], []),
      ("phone", "address", "surname"))

# G01 -- the genuine duplicate. Note what merged her was the proof tuple, not this.
check("G01: the true duplicate agrees on four signals",
      suggestive_signals(C1041, C4788, [K9001], [K9002, K9003]),
      ("phone", "address", "surname", "card_fingerprint"))
check("G01: the emails do NOT agree -- no dot or plus folding is applied",
      "email" in suggestive_signals(C1041, C4788, [K9001], [K9002, K9003]), False)

# G12 -- the malformed phone is not agreement. C0338's phone is valid and C2411's is not,
# so the phone signal is absent even though both rows describe one person.
check("G12: only address and surname agree",
      suggestive_signals(C0338, C2411, [K9011], [K9012]),
      ("address", "surname"))
check("G12: C2411's malformed phone is not evidence",
      normalize_phone(C2411.phone).is_evidence, False)
check("G12: C0338's phone is", normalize_phone(C0338.phone).is_evidence, True)

# G13 -- three rows, different emails and different phones.
check("G13: C0021 and C1777 agree on address and surname",
      suggestive_signals(C0021, C1777, [K9008], [K9009]),
      ("address", "surname"))

# The synthetics agree on address and surname too, on a signal shared by all 16. Another
# reason the suggestive tier may never merge, and another reason exclusion runs first.
check("C4971 and C4972 agree on address and surname",
      suggestive_signals(C4971, C4972, [], []), ("address", "surname"))
check("...and neither forms a proof key",
      (proof_key(C4971), proof_key(C4972)), (None, None))

# Unrelated people share nothing, so the tier is not simply always-on.
check("G05's C0912 and G10's C3078 agree on nothing",
      suggestive_signals(C0912, C3078, [], []), ())

# The signal order is fixed, so an evidence string is byte-identical across runs.
check("signals report in a fixed order",
      suggestive_signals(C2045, C2044, [K9005], [K9004]),
      suggestive_signals(C2044, C2045, [K9004], [K9005]))


# ─────────────────────────────────────────────────────────────────────────────────────
# A5 -- survivorship.
# ─────────────────────────────────────────────────────────────────────────────────────

# G01 IS the money-beats-freshness exhibit: C1041 holds the paid-off advance and C4788 is
# the fresher record, so the two rules disagree and money has to win.
check("G01: the money-moved member survives", choose_survivor([C1041, C4788], {1041}),
      (1041, "money_moved"))
check("G01: 'most recent record wins' would have taken C4788 and lost the advance",
      choose_survivor([C1041, C4788], set()), (4788, "freshest"))

# G13. Money picks C1777 -- and so does freshness, because C1777 is also the freshest of
# the three. The two rules AGREE here, so this group cannot tell them apart; only the
# reason string changes.
check("G13: the funded member survives", choose_survivor([C0021, C1777, C3260], {1777}),
      (1777, "money_moved"))
check("G13: with no money moved the freshest is the same row",
      choose_survivor([C0021, C1777, C3260], set()), (1777, "freshest"))

# G12 -- zero money-moved members, so the freshest wins, and it is not the lowest id.
check("G12: the freshest member survives", choose_survivor([C0338, C2411], set()),
      (2411, "freshest"))

# The NULL-updated_at legacy rows. Ordering on updated_at alone would sort each of these
# as the oldest record in existence; COALESCE is what keeps them comparable.
check("G07: C0203's freshness falls back to created_at",
      C0203.freshness, ts("2018-03-12T10:15:00"))
check("G07: C3844 survives", choose_survivor([C0203, C3844], set()), (3844, "freshest"))
check("G11: C1866's freshness falls back to created_at",
      C1866.freshness, ts("2018-07-22T14:00:00"))
check("G11: C4290 survives", choose_survivor([C1866, C4290], set()), (4290, "freshest"))

# Two money-moved members is a refusal, not a fallback. resolve.py must have refused
# before survivorship is asked about at all.
raised: BaseException | None = None
try:
    choose_survivor([C0777, C3512], {777, 3512})
except ValueError as exc:
    raised = exc
check("G02a: two money-moved members raise", type(raised).__name__, "ValueError")
check("G02a: the message names both members", "777, 3512" in str(raised), True)

raised = None
try:
    choose_survivor([C1298, C4903], {1298, 4903})
except ValueError as exc:
    raised = exc
check("G02b: two money-moved members raise", type(raised).__name__, "ValueError")
check("G02b: the message names both members", "1298, 4903" in str(raised), True)

raised = None
try:
    choose_survivor([], set())
except ValueError as exc:
    raised = exc
check("an empty group raises rather than returning a sentinel",
      type(raised).__name__, "ValueError")

# NO FIELD-LEVEL COALESCING, and here is what it costs. The survivor C2411 keeps its own
# malformed phone while the loser C0338 had a valid one; that is a printed count in the
# run report, not a silent repair, because a record stitched out of several rows describes
# a customer who never existed.
survivor_id, reason = choose_survivor([C0338, C2411], set())
check("G12's survivor is C2411", survivor_id, 2411)
check("G12's survivor keeps its own contact fields",
      normalize_phone(C2411.phone).normalized, None)
check("...while the loser's phone was usable",
      normalize_phone(C0338.phone).normalized, "+13035550155")

# A row with neither timestamp raises rather than substituting an epoch. created_at is NOT
# NULL at the source (01_schema.sql), so this shape is a schema change and cannot come
# from the fixture -- an epoch substitution would make the row lose every comparison it
# takes part in, silently.
raised = None
try:
    cust(0).freshness
except ValueError as exc:
    raised = exc
check("no created_at and no updated_at raises", type(raised).__name__, "ValueError")


# ─────────────────────────────────────────────────────────────────────────────────────
# The row shapes: a missing column raises, and BIT becomes a real bool.
# ─────────────────────────────────────────────────────────────────────────────────────

C1041_ROW: dict[str, Any] = {
    "customer_id": 1041, "first_name": "Alicia", "last_name": "Moreau",
    "email": "alicia.moreau@gmail.com", "phone": "+15125550142", "ssn_last4": "3392",
    "date_of_birth": date(1989, 3, 14), "address_line1": "901 Rio Grande St",
    "city": "Austin", "state_code": "TX", "postal_code": "78701",
    "employer_name": "Halcyon Foods", "signup_channel": "web",
    "created_at": ts("2023-04-11T09:22:14"), "updated_at": ts("2025-11-02T16:04:00"),
}
built = CustomerFields.from_row(C1041_ROW)
check("from_row reads the identity fields", (built.customer_id, built.ssn_last4),
      (1041, "3392"))
check("from_row reads the columns no rule uses too", built.city, "Austin")
check("from_row agrees with the transcription above", proof_key(built), proof_key(C1041))

raised = None
try:
    CustomerFields.from_row({k: v for k, v in C1041_ROW.items() if k != "ssn_last4"})
except KeyError as exc:
    raised = exc
check("a missing column RAISES rather than defaulting to None",
      type(raised).__name__, "KeyError")

A0091_ROW: dict[str, Any] = {
    "advance_id": 91, "customer_id": 88, "status": "Paid Off",
    "principal_amount": 250.00, "funded_at": ts("2026-04-01T09:00:00"),
    "paid_off_at": ts("2026-04-29T09:00:00"), "repayment_account_hash": INSTRUMENT_E,
}
check("AdvanceFields.from_row keeps the raw spelling",
      AdvanceFields.from_row(A0091_ROW).status, "Paid Off")
check("...and the FLOAT principal as a float",
      AdvanceFields.from_row(A0091_ROW).principal_amount, 250.00)
check("a NULL principal stays None",
      AdvanceFields.from_row({**A0091_ROW, "principal_amount": None}).principal_amount, None)

# BIT arrives as 0/1 through pymssql, and `1 is True` is False -- so an `is True` check on
# an unconverted value is quietly wrong for every default card in the table.
card_row: dict[str, Any] = {
    "card_id": 9001, "customer_id": 1041, "card_fingerprint": "fp_a1", "is_default": 1,
}
check("CardFields.from_row converts BIT 1 to a real bool",
      CardFields.from_row(card_row).is_default is True, True)
check("...and BIT 0 to False",
      CardFields.from_row({**card_row, "is_default": 0}).is_default is False, True)


# ─────────────────────────────────────────────────────────────────────────────────────
# The purity claim, CHECKED rather than advertised.
#
# rules.py:12-15 says the entire rule surface is testable on a bare interpreter with no
# container running, and cites this file as the proof. While this file did not exist that
# was an unverified claim about the code.
# ─────────────────────────────────────────────────────────────────────────────────────

check("rules.py still cites this file", "tests/test_rules.py" in (rules.__doc__ or ""), True)
check("importing the rules pulled in no database driver",
      sorted({"duckdb", "pymssql"} & set(sys.modules)), [])


# ─────────────────────────────────────────────────────────────────────────────────────
# OPEN FINDINGS against rules.py's prose. THESE CHECKS FAIL until it is corrected, and
# that is deliberate: a finding with no failing check gets closed by editing a comment.
# Neither finding touches behaviour -- every check above passes -- but both mis-state a
# number a reviewer is told to go and look for, and the fixture is the measuring
# instrument, so a wrong number in it is not cosmetic.
# ─────────────────────────────────────────────────────────────────────────────────────

# FINDING 1 -- choose_survivor's docstring (and 02a:301-302) calls G12's survivor phone a
# "MALFORMED 9-digit phone". The seeded value is '(555) 012-33' (02a:313), which is 8
# digits, and that is what the normalizer reports. The material claim is right; the count
# a reviewer would grep for is not.
survivor_phone = normalize_phone(C2411.phone)
check("G12's survivor phone reports its real digit count",
      survivor_phone.defect_class, "digit_count_8")
measured_digits = (survivor_phone.defect_class or "").rsplit("_", 1)[-1]
check("choose_survivor's docstring quotes the measured digit count",
      f"{measured_digits}-digit" in (choose_survivor.__doc__ or ""), True)

# FINDING 2 -- rules.py:287-289 says C0088 holds "a paid-off advance and six real
# repayments" and that excluding her removes "$250 of genuine repayments". 02e:69-74 seeds
# SIX TRANSACTIONS of which FOUR are repayments (-6500, -6500, -6500, -6750 cents =
# $262.50); the $250.00 is transaction 212401, the disbursement, and it equals advance
# 91's principal. C0088 is deliberately kept out of the bulk transaction pass (02e:92-101)
# so that figure is exactly checkable -- so the one auditable number in the fixture is the
# one the prose states wrong. rules.py holds the claim in a `#` comment, which no
# attribute exposes, so this reads the module source: still stdlib, still no database.
rules_source = inspect.getsource(rules)
check("rules.py does not call C0088's disbursement a repayment total",
      "$250 of genuine repayments" in rules_source, False)
check("rules.py counts C0088's six rows as transactions, not repayments",
      "six real repayments" in rules_source, False)
check("the $250.00 in that sentence is advance 91's principal",
      A0091.principal_amount, 250.00)


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_rules: {checks} checks passed")
