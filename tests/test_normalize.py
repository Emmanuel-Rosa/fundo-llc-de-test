"""Normalization tests -- plain asserts, no test framework.

Run:  python -m tests.test_normalize

No pytest, deliberately. Two small test files do not justify a dependency the reviewer
has to install, and `python -m tests.test_normalize` works from a clean checkout with
nothing but the stdlib. There is no import of pymssql or duckdb here either, so these
run without a database at all.

Every case below is a value that ACTUALLY APPEARS in the seed, or a rule boundary the
seed depends on. Tests over invented values would pass while the fixture broke.
"""

from __future__ import annotations

from datetime import date

from pipeline.identity.normalize import (
    Status,
    normalize_dob,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_ssn_last4,
)

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


# ─────────────────────────────────────────────────────────────────────────────────────
# ssn_last4 -- the field that looks strongest and is contaminated
# ─────────────────────────────────────────────────────────────────────────────────────
for value in ["3392", "8130", "5501", "6640", "7723", "4417", "1188", "9902"]:
    r = normalize_ssn_last4(value)
    check(f"ssn {value!r} is valid evidence", (r.status, r.normalized, r.is_evidence),
          (Status.VALID, value, True))

# Placeholders. '0000' is repeated digits; '1234' is a sequential run. Both are what a
# required-field-with-no-validation collects, and NEITHER may be used as evidence.
check("ssn '0000' -> placeholder", normalize_ssn_last4("0000").status, Status.PLACEHOLDER)
check("ssn '0000' not evidence", normalize_ssn_last4("0000").is_evidence, False)
check("ssn '1234' -> placeholder", normalize_ssn_last4("1234").status, Status.PLACEHOLDER)
check("ssn '1234' not evidence", normalize_ssn_last4("1234").is_evidence, False)
check("ssn '1234' defect class", normalize_ssn_last4("1234").defect_class, "sequential_digits")
check("ssn '1111' -> placeholder", normalize_ssn_last4("1111").status, Status.PLACEHOLDER)
check("ssn '9876' -> placeholder (descending)",
      normalize_ssn_last4("9876").status, Status.PLACEHOLDER)

# THE REGRESSION THIS FILE EXISTS FOR. Sixteen synthetic accounts share ssn '1234' and
# dob 1900-01-01, and their surnames collide: 'Account' x3, 'One' x2, 'Chan' x2. With
# '1234' accepted as a value, the proof tuple proposed 12 pairs of which only 7 were
# right -- precision 0.583 against a claimed 1.000. Gating it makes those 5 impossible
# regardless of whether test-account exclusion ran first.
check("the five false pairs are unreachable: '1234' is never evidence",
      normalize_ssn_last4("1234").is_evidence, False)

# Not placeholders, not values either.
check("ssn ' 12' -> malformed", normalize_ssn_last4(" 12").status, Status.MALFORMED)
check("ssn ' 12' is not padded to '0012'", normalize_ssn_last4(" 12").normalized, None)
check("ssn '' -> missing", normalize_ssn_last4("").status, Status.MISSING)
check("ssn None -> missing", normalize_ssn_last4(None).status, Status.MISSING)
check("ssn 'abcd' -> malformed", normalize_ssn_last4("abcd").status, Status.MALFORMED)
# Raw is preserved in every branch, so nothing is ever unrecoverable.
check("ssn raw preserved on refusal", normalize_ssn_last4(" 12").raw, " 12")

# ─────────────────────────────────────────────────────────────────────────────────────
# date_of_birth
# ─────────────────────────────────────────────────────────────────────────────────────
check("dob 1989-03-14 valid", normalize_dob(date(1989, 3, 14)).is_evidence, True)
check("dob 1900-01-01 -> placeholder",
      normalize_dob(date(1900, 1, 1)).status, Status.PLACEHOLDER)
check("dob 1900-01-01 not evidence", normalize_dob(date(1900, 1, 1)).is_evidence, False)
check("dob None -> missing", normalize_dob(None).status, Status.MISSING)
# G08: the transposed pair must stay DIFFERENT. If normalization made these equal the
# group would auto-merge on unproven evidence.
check("G08 transposition stays distinct",
      normalize_dob(date(1987, 4, 11)).normalized != normalize_dob(date(1987, 11, 4)).normalized,
      True)

# ─────────────────────────────────────────────────────────────────────────────────────
# names -- diacritic folding is a MEASURED component, not a nicety
# ─────────────────────────────────────────────────────────────────────────────────────
check("name diacritics fold", normalize_name("José").normalized,
      normalize_name("Jose").normalized)
check("name case folds", normalize_name("MCDONALD").normalized,
      normalize_name("McDonald").normalized)
check("name padding collapses", normalize_name(" Nguyen ").normalized, "nguyen")
check("name internal whitespace collapses",
      normalize_name("Van  der   Berg").normalized, "van der berg")
# G09: the measured cost of surname-in-the-tuple. Collapsing the hyphen would recover
# that pair for the wrong reason and silently change the precision/recall trade.
check("G09 hyphenated surname stays distinct",
      normalize_name("Nowak").normalized != normalize_name("Nowak-Brennan").normalized,
      True)
check("name None -> missing", normalize_name(None).status, Status.MISSING)

# ─────────────────────────────────────────────────────────────────────────────────────
# email -- a SUGGESTS field, so these statuses gate candidacy, never a merge
# ─────────────────────────────────────────────────────────────────────────────────────
for value in ["alicia.moreau@gmail.com", "ar@brightpath-staffing.com",
              "priya.n@fundo.com", "marcus.testerman@gmail.com",
              "greatest.deals@hotmail.com", "protest.organizer@riseup.net"]:
    check(f"email {value!r} valid", normalize_email(value).is_evidence, True)

check("email trims", normalize_email(" jamal.pierce@gmail.com ").normalized,
      "jamal.pierce@gmail.com")
check("email lowercases", normalize_email("Renee.Duval@Hotmail.COM").normalized,
      "renee.duval@hotmail.com")
check("email TEST@FUNDO.COM lowercases", normalize_email("TEST@FUNDO.COM").normalized,
      "test@fundo.com")

check("email 'n/a' -> placeholder", normalize_email("n/a").status, Status.PLACEHOLDER)
check("email 'none' -> placeholder", normalize_email("none").status, Status.PLACEHOLDER)
check("email 'unknown' -> placeholder", normalize_email("unknown").status, Status.PLACEHOLDER)
# Two customers share 'n/a'. Under raw equality that is a proposed merge of strangers.
check("shared placeholder is not evidence", normalize_email("n/a").is_evidence, False)
check("email example.com -> placeholder (RFC 2606)",
      normalize_email("jsmith@example.com").status, Status.PLACEHOLDER)
check("email carl.smith@example.com -> placeholder",
      normalize_email("carl.smith@example.com").status, Status.PLACEHOLDER)

check("email missing @ -> malformed", normalize_email("kmartinez.gmail.com").defect_class,
      "missing_at")
check("email double dot -> malformed", normalize_email("sofia@outlook..com").defect_class,
      "double_dot_domain")
check("email trailing dot -> malformed",
      normalize_email("dan.oyelaran@yahoo.com.").defect_class, "trailing_punctuation")
check("email internal space -> malformed",
      normalize_email("r johnson@aol.com").defect_class, "internal_whitespace")
check("email no TLD -> malformed", normalize_email("pat@localhost").status, Status.MALFORMED)
check("email long local part -> malformed",
      normalize_email("a" * 65 + "@gmail.com").defect_class, "local_part_length")
check("email None -> missing", normalize_email(None).status, Status.MISSING)
check("email '' -> missing", normalize_email("").status, Status.MISSING)

# The homoglyph. 'gm<CYRILLIC a>il.com' is INVISIBLE in a terminal, so the codepoint has
# to be in the defect class or the finding is unactionable.
homoglyph = normalize_email("anna@gmаil.com")
check("homoglyph -> malformed", homoglyph.status, Status.MALFORMED)
check("homoglyph reports the codepoint", homoglyph.defect_class, "non_ascii[U+0430]")

# ─────────────────────────────────────────────────────────────────────────────────────
# phone
# ─────────────────────────────────────────────────────────────────────────────────────
check("phone E.164 passes through", normalize_phone("+15125550142").normalized,
      "+15125550142")
check("phone punctuation only", normalize_phone("(512) 555-0142").normalized,
      "+15125550142")
check("phone dots", normalize_phone("512.555.0142").normalized, "+15125550142")
check("phone leading 1", normalize_phone("15125550142").normalized, "+15125550142")
check("phone bare 10 digits", normalize_phone("5125550142").normalized, "+15125550142")
# All four spellings must land on ONE value, or the same instrument reads as four.
check("all four spellings agree", len({
    normalize_phone("+15125550142").normalized,
    normalize_phone("(512) 555-0142").normalized,
    normalize_phone("512.555.0142").normalized,
    normalize_phone("15125550142").normalized,
}), 1)

# The ONE class that normalizes with loss -- flagged as such rather than silently dropped.
ext = normalize_phone("5125550142 ext 214")
check("extension: main number extracted", ext.normalized, "+15125550142")
check("extension: still valid", ext.status, Status.VALID)
check("extension: loss is recorded", ext.defect_class, "extension_dropped")

# Placeholders. Three unrelated customers share 0000000000 -- without this gate that is
# a proposed THREE-WAY merge of three strangers.
check("phone 0000000000 -> placeholder",
      normalize_phone("0000000000").status, Status.PLACEHOLDER)
check("phone 0000000000 not evidence", normalize_phone("0000000000").is_evidence, False)
check("phone 1111111111 -> placeholder",
      normalize_phone("1111111111").status, Status.PLACEHOLDER)
check("phone 1234567890 -> placeholder (sequential, wraps 9->0)",
      normalize_phone("1234567890").status, Status.PLACEHOLDER)

# G12's survivor carries this one. It is 8 digits, and NOT paddable.
short = normalize_phone("(555) 012-33")
check("G12 short phone -> malformed", short.status, Status.MALFORMED)
check("G12 short phone not evidence", short.is_evidence, False)
check("G12 short phone is not padded", short.normalized, None)
check("G12 raw preserved", short.raw, "(555) 012-33")

check("phone vanity -> malformed", normalize_phone("800-FLOWERS").defect_class,
      "vanity_or_letters")
check("phone 12+ digits -> malformed",
      normalize_phone("5125550142000").status, Status.MALFORMED)
check("phone full-width digits -> malformed",
      normalize_phone("５１２５５５０１４２").status,
      Status.MALFORMED)
check("phone None -> missing", normalize_phone(None).status, Status.MISSING)
check("phone '' -> missing", normalize_phone("").status, Status.MISSING)

# The STATED limitation: US-only. A real E.164 parser would accept this as GB.
gb = normalize_phone("44205550123")
check("11 digits not starting 1 -> malformed (US-only assumption)",
      gb.defect_class, "eleven_digits_non_us")

# NANP structural rules -- an area code or exchange starting 0 or 1 cannot be dialled.
check("phone area code starting 0 -> malformed",
      normalize_phone("0125550142").status, Status.MALFORMED)
check("phone exchange starting 1 -> malformed",
      normalize_phone("5121550142").defect_class, "invalid_nanp_prefix")


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_normalize: {checks} checks passed")
