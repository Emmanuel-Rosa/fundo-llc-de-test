"""Run every pure test suite, report one table, decide the exit code.

The suites existed before this did, and nothing ran them. They were COPY'd into the image
and mounted into the container, and no command in this repo invoked them -- so a reviewer
had to know to type five separate `python -m tests.test_*` lines. That is the same defect
class as a compose file advertising a command that was never executed: shipped, plausible,
and never once run by the path the README points at.

DISCOVERED BY GLOB, never listed. A hardcoded tuple of suite names is a constant that goes
stale the first time someone adds a file, and it goes stale in the silent direction -- the
runner still passes, having skipped the new suite. `tests/test_*.py`, sorted, is the list.

ONE SUBPROCESS PER SUITE, not one interpreter for all of them. The suites are module-level
scripts and two of them install a `pymssql` stub into `sys.modules`; sharing an interpreter
would let import order in one suite change the result of another, and a harness whose
verdict depends on run order is worse than no harness. Five process spawns is a price worth
paying for a result that means what it says.

WHY THE DEMO RUNS THIS FIRST: none of it needs a database, and the source build that
follows takes minutes. Failing the cheap deterministic half before spending them is the
whole point of having a cheap deterministic half.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT

_WIDTH = 96


@dataclass(frozen=True)
class SuiteResult:
    name: str
    exit_code: int
    summary: str
    wall_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def checks(self) -> int | None:
        """How many assertions the suite made, or None if its line did not say.

        Parsed from the suite's own summary rather than counted here, because the suites are
        the authority on how many assertions they made and a second count is just a number
        to disagree with the first.

        TWO SHAPES, and getting this wrong produced a wrong number on the runner's first
        red run: a passing suite ends `test_x: 274 checks passed`, while a failing one opens
        `FAILED 1 of 44 checks:`. Taking the first integer read the FAILURE count as the
        total, so one broken assertion turned a 465-check run into a reported 422. Both
        shapes are matched explicitly and anything else returns None, which prints as `-`
        rather than as a plausible-looking zero.
        """
        failed = re.match(r"FAILED (\d+) of ([\d,]+) checks", self.summary)
        if failed:
            return int(failed.group(2).replace(",", ""))
        passed = re.search(r"([\d,]+) checks passed", self.summary)
        if passed:
            return int(passed.group(1).replace(",", ""))
        return None


def discover(tests_dir: Path) -> list[str]:
    return sorted(p.stem for p in tests_dir.glob("test_*.py"))


def run_suite(name: str) -> SuiteResult:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", f"tests.{name}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    wall_ms = int((time.monotonic() - started) * 1000)
    output = (proc.stdout + proc.stderr).strip()
    lines = [l for l in output.splitlines() if l.strip()]
    # On success the suite's last line is its own summary. On failure the FIRST line is the
    # count of failures and the rest is the detail, which the caller prints in full.
    summary = (lines[-1] if proc.returncode == 0 else lines[0]) if lines else "no output"
    return SuiteResult(name=name, exit_code=proc.returncode, summary=summary, wall_ms=wall_ms)


def run_selftest() -> int:
    """Run every discovered suite. Returns the process exit code."""
    tests_dir = REPO_ROOT / "tests"
    names = discover(tests_dir)

    print("  SELF TEST -- every pure suite, no database, no network")
    print("  " + "-" * _WIDTH)

    if not names:
        # An empty glob is not a pass. A container built without the tests directory would
        # otherwise report a clean self test having asserted nothing at all.
        print(f"  NO SUITES FOUND under {tests_dir}. Refusing to report a pass for an")
        print("  empty test run -- either the image was built without tests/ or the glob")
        print("  no longer matches the files.")
        return 1

    print(f"  {'suite':<26} {'result':<9} {'checks':>7} {'ms':>7}  detail")
    print("  " + "-" * _WIDTH)

    results = [run_suite(name) for name in names]
    failed = [r for r in results if not r.ok]

    for r in results:
        verdict = "pass" if r.ok else "FAIL"
        count = f"{r.checks:,}" if r.checks is not None else "-"
        print(f"  {r.name:<26} {verdict:<9} {count:>7} {r.wall_ms:>7,}  {r.summary}")

    print("  " + "-" * _WIDTH)
    counted = [r.checks for r in results if r.checks is not None]
    unknown = len(results) - len(counted)
    tail = f", {unknown} suite(s) reported no count" if unknown else ""
    print(f"  {len(results)} suite(s), {sum(counted):,} checks, "
          f"{sum(r.wall_ms for r in results):,} ms{tail}")

    if failed:
        print()
        for r in failed:
            print(f"  ---- {r.name} (exit {r.exit_code}) " + "-" * max(0, _WIDTH - 24 - len(r.name)))
            proc = subprocess.run(
                [sys.executable, "-m", f"tests.{r.name}"],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            for line in (proc.stdout + proc.stderr).rstrip().splitlines():
                print(f"  {line}")
        print()
        print(f"  {len(failed)} of {len(results)} suite(s) FAILED. Exiting non-zero.")
        return 1

    print("  every suite passed")
    return 0
