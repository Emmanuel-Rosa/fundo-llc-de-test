"""Contact-field normalization, and the placeholder detection that gates matching.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE: a value whose status is not `valid` is
never match evidence. Not for proof, not for suggestion, not for a score.

That gate is worth more than it looks. Three unrelated customers in the fixture share
the phone `0000000000` and two share the email `n/a`; under raw equality that is four
proposed merges of unrelated people. The gate makes it zero. Measured against the
counterfactuals, the gate alone is worth ~0.06 precision -- which is why flagging
malformed contacts is an identity control here and not cosmetic data cleaning.

Every field returns three things, never one:
    raw         -- preserved untouched, always
    normalized  -- None whenever the value is not usable
    status      -- valid | malformed | placeholder | missing

`malformed` and `placeholder` and `missing` are deliberately three states rather than
one "bad" state, because they imply three different operational actions: chase the
customer, ignore the field entirely, and ask for the field respectively. Collapsing
them throws away the only information that makes the scorecard actionable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class Status(StrEnum):
    VALID = "valid"
    MALFORMED = "malformed"
    PLACEHOLDER = "placeholder"
    MISSING = "missing"


@dataclass(frozen=True)
class Normalized:
    """The outcome of normalizing one field value."""

    raw: str | None
    normalized: str | None
    status: Status
    defect_class: str | None = None

    @property
    def is_evidence(self) -> bool:
        """Whether this value may be used as matching evidence, at any tier.

        The single gate. Anything not `valid` returns False -- see the module docstring.
        """
        return self.status is Status.VALID and self.normalized is not None


# ─────────────────────────────────────────────────────────────────────────────────────
# Digit-shape predicates -- the generic form of "this is a placeholder, not a value".
#
# These are written as RULES rather than as a blocklist of observed values on purpose.
# A hardcoded list of {'0000', '1234'} is a list that the next placeholder walks
# straight through, and the failure is silent: an unrecognised placeholder is treated
# as strong evidence and merges unrelated people.
# ─────────────────────────────────────────────────────────────────────────────────────

def _is_repeated_digits(digits: str) -> bool:
    """'0000', '1111', '9999999999' -- one digit repeated."""
    return len(digits) >= 3 and len(set(digits)) == 1


def _is_sequential_digits(digits: str) -> bool:
    """'1234', '0123', '1234567890', and the descending forms.

    A run of consecutive digits is something a human types to get past a required
    field, not something an issuer assigns. '1234567890' wraps 9->0, which is why the
    ascending check tolerates that one step.
    """
    if len(digits) < 4:
        return False
    ascending = all(
        (int(digits[i + 1]) - int(digits[i])) % 10 == 1 for i in range(len(digits) - 1)
    )
    descending = all(
        (int(digits[i]) - int(digits[i + 1])) % 10 == 1 for i in range(len(digits) - 1)
    )
    return ascending or descending


# ─────────────────────────────────────────────────────────────────────────────────────
# ssn_last4
# ─────────────────────────────────────────────────────────────────────────────────────

def normalize_ssn_last4(raw: str | None) -> Normalized:
    """Normalize the last four SSN digits.

    The column is VARCHAR(10) and should be CHAR(4); it holds '0000', '', ' 12' and
    '1234'. It is the field that LOOKS like the strongest evidence available and is
    contaminated by exactly the values a required-field-with-no-validation collects.

    '1234' IS TREATED AS A PLACEHOLDER, and that is a deliberate trade with a real cost.
    Roughly 1 in 10,000 genuine customers has a last4 of 1234, and for those customers
    this rule removes the strongest component of the proof tuple, so they can no longer
    be auto-merged and fall to the review queue instead. That cost is accepted, and the
    scorer prints it, for two reasons:

      * the failure direction is safe. Refusing to prove identity sends a pair to a
        human; wrongly proving it merges two people's borrowing history, and in a
        lending book that is not recoverable.
      * the fixture measures the alternative. Sixteen synthetic accounts all carry
        '1234', and three of them share the surname 'Account' while two share 'One' and
        two share 'Chan'. Without this rule the proof tuple confidently proposes five
        merges of unrelated synthetic identities -- and it proposes them with the same
        confidence it uses for real ones.

    That second point is the load-bearing one. Test-account exclusion normally runs
    BEFORE identity resolution and would remove those rows first, so in the shipped
    order this rule is redundant. It is here anyway because "correct only because
    another stage ran first" is a property that silently stops being true the day
    someone reorders the pipeline, and nothing would fail loudly when it does.
    """
    if raw is None:
        return Normalized(raw, None, Status.MISSING)

    stripped = raw.strip()
    if not stripped:
        return Normalized(raw, None, Status.MISSING)

    if not stripped.isdigit():
        return Normalized(raw, None, Status.MALFORMED, "non_numeric")

    if len(stripped) != 4:
        # ' 12' -> '12'. Not paddable: '12' could be 0012 or 1200 and guessing which
        # invents a value the source never held.
        return Normalized(raw, None, Status.MALFORMED, f"wrong_length_{len(stripped)}")

    if _is_repeated_digits(stripped):
        return Normalized(raw, None, Status.PLACEHOLDER, "repeated_digits")

    if _is_sequential_digits(stripped):
        return Normalized(raw, None, Status.PLACEHOLDER, "sequential_digits")

    return Normalized(raw, stripped, Status.VALID)


# ─────────────────────────────────────────────────────────────────────────────────────
# date_of_birth
# ─────────────────────────────────────────────────────────────────────────────────────

_DOB_PLACEHOLDERS = frozenset({date(1900, 1, 1), date(1901, 1, 1), date(1970, 1, 1)})


def normalize_dob(raw: date | None) -> Normalized:
    """1900-01-01 is a placeholder, not a birth date.

    So are 1901-01-01 and the Unix epoch: all three are what a form emits when a date
    is required and unknown. A customer genuinely born in 1900 would be 126 years old.
    """
    if raw is None:
        return Normalized(None, None, Status.MISSING)
    if raw in _DOB_PLACEHOLDERS:
        return Normalized(raw.isoformat(), None, Status.PLACEHOLDER, "canonical_epoch")
    if raw.year < 1910 or raw > date(2026, 8, 18):
        return Normalized(raw.isoformat(), None, Status.MALFORMED, "implausible_year")
    return Normalized(raw.isoformat(), raw.isoformat(), Status.VALID)


# ─────────────────────────────────────────────────────────────────────────────────────
# names
# ─────────────────────────────────────────────────────────────────────────────────────

def normalize_name(raw: str | None) -> Normalized:
    """Casefold, strip diacritics, collapse internal whitespace.

    Diacritic folding is not decoration: six generated duplicate families differ only
    by casing, diacritics or padding ('Jose'/'Jose', 'MCDONALD'/'McDonald',
    ' Nguyen '). Removing the folding drops recall by exactly those six pairs, which is
    what makes this function a measured component rather than a stylistic choice.

    Hyphens are PRESERVED. 'Nowak' and 'Nowak-Brennan' must stay distinct, because that
    pair is the measured cost of putting surname in the proof tuple and collapsing the
    hyphen would silently recover it for the wrong reason.
    """
    if raw is None:
        return Normalized(None, None, Status.MISSING)

    decomposed = unicodedata.normalize("NFKD", raw)
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    collapsed = re.sub(r"\s+", " ", folded).strip().casefold()

    if not collapsed:
        return Normalized(raw, None, Status.MISSING)
    return Normalized(raw, collapsed, Status.VALID)


# ─────────────────────────────────────────────────────────────────────────────────────
# email
# ─────────────────────────────────────────────────────────────────────────────────────

_EMAIL_PLACEHOLDERS = frozenset({"n/a", "na", "none", "null", "unknown", "x@x", "-", "."})

# RFC 2606 reserved names, plus the addresses that only ever appear in documentation.
_RESERVED_DOMAINS = frozenset({"example.com", "example.org", "example.net", "test",
                               "example", "invalid", "localhost"})

_ASCII_EMAIL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+$")


def normalize_email(raw: str | None) -> Normalized:
    """Normalize an email address, or refuse to.

    The classes below are the ones actually present in the fixture, and the split
    between "formatting only" and "unusable" is the whole decision: 40 of the 96 defective
    email values are reversibly normalizable (whitespace and casing), and the rest are
    not repairable without inventing data. A warehouse that invents a corrected address
    hands ops something to contact that the source disagrees with.

    NOTE ON SCOPE: this does not implement dot-folding or plus-tag stripping for gmail.
    Those would make 'alicia.moreau@gmail.com' and 'a.moreau+new@gmail.com' equal --
    and email is a SUGGESTS field, never a PROVES field, so making two suggestive values
    agree more often changes no merge decision. It would only inflate the candidate
    queue. Left out on purpose.
    """
    if raw is None:
        return Normalized(None, None, Status.MISSING)

    trimmed = raw.strip()
    if not trimmed:
        return Normalized(raw, None, Status.MISSING)

    lowered = trimmed.casefold()

    if lowered in _EMAIL_PLACEHOLDERS:
        return Normalized(raw, None, Status.PLACEHOLDER, "placeholder_literal")

    # Non-ASCII anywhere is refused, and the offending codepoint is REPORTED. A Cyrillic
    # 'a' in 'gmail.com' is invisible in a terminal, so "malformed email" with no detail
    # reads as a false positive to whoever has to action it.
    if not _ASCII_EMAIL.match(trimmed):
        offenders = [ch for ch in trimmed if ord(ch) > 127]
        if offenders:
            codepoints = " ".join(f"U+{ord(ch):04X}" for ch in dict.fromkeys(offenders))
            return Normalized(raw, None, Status.MALFORMED, f"non_ascii[{codepoints}]")
        if "@" not in trimmed:
            return Normalized(raw, None, Status.MALFORMED, "missing_at")
        if re.search(r"\s", trimmed):
            return Normalized(raw, None, Status.MALFORMED, "internal_whitespace")
        return Normalized(raw, None, Status.MALFORMED, "illegal_character")

    if trimmed.count("@") != 1:
        return Normalized(raw, None, Status.MALFORMED, "multiple_at")

    local, _, domain = lowered.partition("@")

    if not local or len(local) > 64:
        return Normalized(raw, None, Status.MALFORMED, "local_part_length")
    if ".." in domain:
        # A fix here is a guess: 'outlook..com' could be outlook.com or a typo for
        # something else entirely.
        return Normalized(raw, None, Status.MALFORMED, "double_dot_domain")
    if domain.endswith(".") or domain.startswith(".") or domain.startswith("-"):
        return Normalized(raw, None, Status.MALFORMED, "trailing_punctuation")
    if "." not in domain:
        return Normalized(raw, None, Status.MALFORMED, "missing_tld")
    if domain in _RESERVED_DOMAINS or domain.rsplit(".", 1)[-1] in _RESERVED_DOMAINS:
        return Normalized(raw, None, Status.PLACEHOLDER, "reserved_domain")

    # Trimming and lowercasing are the only transformations applied, and both are
    # reversible from `raw`, which is retained.
    return Normalized(raw, lowered, Status.VALID)


# ─────────────────────────────────────────────────────────────────────────────────────
# phone
# ─────────────────────────────────────────────────────────────────────────────────────

_EXTENSION = re.compile(r"\b(?:ext|x|ext\.|extension)\s*\.?\s*(\d{1,6})\s*$", re.IGNORECASE)


def normalize_phone(raw: str | None) -> Normalized:
    """Normalize a US phone number to E.164, or refuse to.

    STATED LIMITATION, because it is a real one rather than a hidden bug: this assumes
    US numbering. An 11-digit value not starting with 1 -- '44205550123' -- is refused
    here, where a full E.164 parse would accept it as a GB number. Fundo's book is
    US-only so the assumption holds today; it is named so that the day it stops holding,
    the behaviour is documented rather than discovered.

    One class normalizes WITH LOSS: an embedded extension. '5125550142 ext 214' yields
    the main number and records `extension_dropped`, because the extension is real
    information being discarded. Every other valid class is a pure formatting change.
    """
    if raw is None:
        return Normalized(None, None, Status.MISSING)

    trimmed = raw.strip()
    if not trimmed:
        return Normalized(raw, None, Status.MISSING)

    # Full-width digits and other unicode numerals: refused rather than transliterated.
    if any(ord(ch) > 127 for ch in trimmed):
        offenders = [ch for ch in trimmed if ord(ch) > 127]
        codepoints = " ".join(f"U+{ord(ch):04X}" for ch in dict.fromkeys(offenders))
        return Normalized(raw, None, Status.MALFORMED, f"non_ascii[{codepoints}]")

    if re.search(r"[A-Za-z]", trimmed) and not _EXTENSION.search(trimmed):
        # '800-FLOWERS'. Resolving vanity strings needs a keypad mapping and produces a
        # number nobody stored.
        return Normalized(raw, None, Status.MALFORMED, "vanity_or_letters")

    defect: str | None = None
    body = trimmed
    extension_match = _EXTENSION.search(body)
    if extension_match:
        body = body[: extension_match.start()]
        defect = "extension_dropped"

    digits = re.sub(r"\D", "", body)

    if not digits:
        return Normalized(raw, None, Status.MISSING)

    if _is_repeated_digits(digits):
        return Normalized(raw, None, Status.PLACEHOLDER, "repeated_digits")
    if _is_sequential_digits(digits):
        return Normalized(raw, None, Status.PLACEHOLDER, "sequential_digits")

    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif len(digits) == 11:
        return Normalized(raw, None, Status.MALFORMED, "eleven_digits_non_us")

    if len(digits) != 10:
        return Normalized(raw, None, Status.MALFORMED, f"digit_count_{len(digits)}")

    # NANP: neither the area code nor the exchange may begin with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        return Normalized(raw, None, Status.MALFORMED, "invalid_nanp_prefix")

    return Normalized(raw, f"+1{digits}", Status.VALID, defect)
