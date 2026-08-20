"""Reconciliation checks: does the warehouse say what the source says?

The load reports what it MOVED. Nothing in it reports whether what arrived is what was
there -- a loader can be internally consistent, report clean runs forever, and still be
wrong. These checks are the other half, and they read both sides.

THE VERDICT HAS THREE VALUES, not two, and the middle one is the point:

    PASS          the two sides agree
    SOURCE-DIRTY  they agree, and what they agree on is bad data that really is in the
                  source. Replicated faithfully. NOT a failure, and repairing it in
                  flight would hide a source problem behind a clean-looking warehouse.
    FAIL          the warehouse does not say what the source says. A replication defect.

Only FAIL exits non-zero, which is what makes `--abort-on-container-failure` mean
something: it aborts on defects and not on known source dirt.

The rejected alternative is worth recording, because it is a trap this repo already fell
into once. "Any discrepancy fails" would fail every clean run on the three orphaned
customer_history rows, forcing a hardcoded `_EXPECTED_ORPHANS = 3` -- the same shape of
stale constant as the `_EXPECTED_MISSES = 6` that was deleted from score.py, which printed
a fabricated discrepancy against a correct run. An expected-exceptions list is a lie with
a maintenance schedule.
"""

from .scorecard import Scorecard, format_scorecard, run_scorecard

__all__ = ["Scorecard", "format_scorecard", "run_scorecard"]
