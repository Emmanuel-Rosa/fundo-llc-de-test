"""Duplicate a run's output into docs/, so a run is evidence rather than scrollback.

docker-compose.yml mounts `./docs:/app/docs` with the comment "Transcripts land on the host
so a run is evidence, not scrollback." Nothing wrote them. The directory was empty after
several complete successful runs, which made that line a claim about behaviour that did not
exist -- the same shape as the compose command that was never executed and the test suites
that nothing ran.

WHAT THIS IS NOT: a logging framework. It duplicates the two streams the reports already
write to, and nothing in the pipeline knows it exists. That is the property worth keeping --
every report remains a `print`, testable and readable, and the transcript is a side effect
of the entry point rather than a concern threaded through nine modules.

A DETERMINISTIC FILENAME, not a timestamped one. `docs/check.log` is citable from the README
and from a review comment; `docs/check-20260820-142233.log` is not, and a directory that
grows a file per run makes "the transcript" ambiguous the second time anyone runs anything.
The cost is that a run overwrites its predecessor, which is the right trade when the question
is "what did the last run say".

A FAILURE HERE NEVER FAILS THE RUN. If docs/ is missing, read-only, or on a full disk, the
pipeline says so in one line and carries on to stdout. Losing a transcript is an
inconvenience; losing the run that produced it because the log could not be opened is not.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from .config import DOCS_DIR


class _Tee:
    """Write to two streams. Only the methods print() and traceback printing use."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self._primary = primary
        self._secondary = secondary

    def write(self, data: str) -> int:
        written = self._primary.write(data)
        try:
            self._secondary.write(data)
        except (OSError, ValueError):
            pass          # a broken transcript must not break the stream being transcribed
        return written

    def flush(self) -> None:
        self._primary.flush()
        try:
            self._secondary.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        # Asked by libraries deciding whether to colour their output. Answer for the REAL
        # stream: the transcript is a file either way, and claiming to be a tty when the
        # primary is a pipe would put escape codes into both.
        return self._primary.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._primary, "encoding", "utf-8")


@contextmanager
def transcript(command: str) -> Iterator[Path | None]:
    """Duplicate stdout and stderr into docs/<command>.log for the duration.

    Yields the path being written, or None when no transcript could be opened.
    """
    path = DOCS_DIR / f"{command}.log"
    handle: TextIO | None = None
    try:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        # UTF-8 with replacement, even though every report in this repo is ASCII by policy:
        # the exception is text this code did not write -- a SQL Server error message carries
        # the server's collation, and a UnicodeEncodeError while reporting a failure would
        # replace the diagnosis with a different one.
        handle = path.open("w", encoding="utf-8", errors="replace", newline="\n")
    except OSError as exc:
        print(f"  NOTE: no transcript written to {path} ({exc.strerror or exc}). "
              f"The run continues; only the copy is lost.")
        yield None
        return

    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(real_out, handle)          # type: ignore[assignment]
    sys.stderr = _Tee(real_err, handle)          # type: ignore[assignment]
    try:
        yield path
    finally:
        sys.stdout, sys.stderr = real_out, real_err
        try:
            handle.flush()
            handle.close()
        except OSError:
            pass
        print(f"  transcript: {path}")
