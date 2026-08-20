"""Connection-retry tests -- the cold-start login race, which was a real outage.

Run:  python -m tests.test_connect

WHAT THIS GUARDS. `docker compose up` on a fresh volume died after 11 seconds with
"SQL Server rejected the login for user 'sa'". The password was correct. SQL Server accepts
TCP connections while it is still creating its system databases, and during that window it
fails logins with 18456 State 7 -- "an error occurred while evaluating the password". The
connect loop treated any 18456 as fatal, on the reasonable-sounding argument that a server
answering "login failed" is up, so retrying cannot help.

It was luck that this ever worked. The same code had completed several clean runs before it
failed, which is the worst possible property for a claim that the thing runs from a clean
checkout: a reviewer hits it or does not depending on how fast their laptop starts a
database.

So the behaviour under test is the one that is easy to regress back to. Someone reading
`if "Login failed" in message: raise` will find it obviously correct -- it reads like
careful error handling -- and the tests below are what say otherwise.

No database and no driver: `pymssql` is stubbed, and `pymssql.connect` is replaced with a
scripted sequence of failures. What is being tested is the LOOP, not the server.
"""

from __future__ import annotations

import sys
import types

if "pymssql" not in sys.modules:
    _stub = types.ModuleType("pymssql")
    _stub.Error = type("Error", (Exception,), {})       # type: ignore[attr-defined]
    _stub.connect = lambda *a, **k: None                # type: ignore[attr-defined]
    sys.modules["pymssql"] = _stub

import pymssql                                          # noqa: E402  (the stub, or the real one)

from pipeline.config import SourceConfig                # noqa: E402
from pipeline.source import Source                      # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, actual: object, expected: object) -> None:
    global checks
    checks += 1
    if actual != expected:
        failures.append(f"{label}\n      expected: {expected!r}\n      actual:   {actual!r}")


# The message pymssql actually produced during the incident, trimmed to what matters.
LOGIN_FAILED = (
    "(18456, b\"Login failed for user 'sa'.DB-Lib error message 20018, severity 14:\\n\""
)
UNREACHABLE = "(20009, b'DB-Lib error message 20009, severity 9:\\nUnable to connect')"


def config(**over: object) -> SourceConfig:
    base = dict(
        host="mssql", port=1433, user="sa", password="pw", database="fundo_src",
        connect_timeout_seconds=30, connect_retry_interval_seconds=0.0,
        login_grace_seconds=10.0,
    )
    base.update(over)
    return SourceConfig(**base)                          # type: ignore[arg-type]


class Scripted:
    """Fails with the given messages in order, then succeeds. Counts its calls."""

    def __init__(self, *messages: str) -> None:
        self._messages = list(messages)
        self.calls = 0

    def __call__(self, *_a: object, **_k: object) -> object:
        self.calls += 1
        if self._messages:
            raise pymssql.Error(self._messages.pop(0))
        return object()                                  # stands in for a Connection


def with_connect(fake: Scripted, fn) -> object:
    original = pymssql.connect
    pymssql.connect = fake                               # type: ignore[assignment]
    try:
        return fn()
    finally:
        pymssql.connect = original                       # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────────────
# THE INCIDENT: a rejected login that clears on its own
# ─────────────────────────────────────────────────────────────────────────────────────

fake = Scripted(LOGIN_FAILED, LOGIN_FAILED, LOGIN_FAILED)
source = Source(config())
outcome = with_connect(fake, lambda: (source.connect(), "connected")[1])

check("a login rejected while SQL Server starts up is RETRIED, not fatal -- this is the "
      "11-second cold-start failure", outcome, "connected")
check("it retried until it got through", fake.calls, 4)


# A login failure mixed in with connection refusals still recovers: on a real cold start
# the server refuses the port first and rejects the login second.
fake = Scripted(UNREACHABLE, UNREACHABLE, LOGIN_FAILED, LOGIN_FAILED)
source = Source(config())
outcome = with_connect(fake, lambda: (source.connect(), "connected")[1])
check("refusals then login rejections then success -- the real cold-start sequence",
      outcome, "connected")
check("every stage was retried", fake.calls, 5)


# ─────────────────────────────────────────────────────────────────────────────────────
# AND THE CASE THE OLD BEHAVIOUR GOT RIGHT, which the fix must not lose
# ─────────────────────────────────────────────────────────────────────────────────────

# A genuinely wrong password never clears. It must still be reported as a PASSWORD problem
# and not as a timeout, because the two send a reader to completely different places.
fake = Scripted(*([LOGIN_FAILED] * 500))
source = Source(config(login_grace_seconds=0.0))
try:
    with_connect(fake, source.connect)
    verdict = "no error raised"
except SystemExit as exc:
    verdict = str(exc)

check("a login that never clears is still fatal", verdict.startswith("SQL Server kept "
      "rejecting the login"), True)
check("and it names the password, not a timeout", "MSSQL_SA_PASSWORD" in verdict, True)
check("and it names the baked-into-the-volume trap, which is the usual cause",
      "down -v" in verdict, True)
check("it did not retry 500 times", fake.calls <= 2, True)

# An unreachable server is a different diagnosis again.
fake = Scripted(*([UNREACHABLE] * 500))
source = Source(config(connect_timeout_seconds=0))
try:
    with_connect(fake, source.connect)
    verdict = "no error raised"
except SystemExit as exc:
    verdict = str(exc)

check("an unreachable server reports unreachable", verdict.startswith("Could not reach"), True)
check("and offers the OOM cause first, which is the one that looks like a boot",
      "OOM-killed" in verdict, True)
check("it does NOT blame the password", "MSSQL_SA_PASSWORD" in verdict, False)

# wait=False is a zero-budget probe: one attempt, no retries, whatever the failure.
fake = Scripted(UNREACHABLE, UNREACHABLE)
source = Source(config())
try:
    with_connect(fake, lambda: source.connect(wait=False))
    verdict = "no error raised"
except SystemExit:
    verdict = "raised"
check("wait=False makes exactly one attempt", fake.calls, 1)
check("and fails rather than blocking", verdict, "raised")


# ─────────────────────────────────────────────────────────────────────────────────────
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:\n")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"test_connect: {checks} checks passed")
