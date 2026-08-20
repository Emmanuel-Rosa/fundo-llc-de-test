# probes

Scripts that produced findings the code now depends on. **Nothing in the pipeline runs these
and no command in the README needs them** — they are here so a claim of the form "measured"
can be re-measured by someone who does not take my word for it.

They are kept for the same reason the checks print what they cannot see: a number nobody can
reproduce is an assertion wearing a number's clothes.

## `watermark_race.py`

**What it proves: the bounded high-water mark is not safe under concurrent writers, and its
docstring used to say it was.**

An IDENTITY value is assigned when a row is INSERTED and made visible when its transaction
COMMITS, and those are not the same moment. So a row can be uncommitted at the instant a
reader's snapshot opens while a *higher* id has already committed — which puts the invisible
row **below** the ceiling that snapshot takes.

```
A: INSERT (gets id 3), transaction LEFT OPEN
B: INSERT (gets id 4), COMMITTED
C: opens SNAPSHOT     -> ceiling MAX(id) = 4, rows visible [1, 2, 4], id 3 invisible
A: COMMITS            -> id 3 is now visible, and 3 <= 4
```

A loader that advanced its watermark to 4 reads `id > 4` next run. Row 3 is never read again,
nothing raises, and the probe row carried 50,000 cents.

There is no `MIN_ACTIVE_IDENTITY`, so no choice of ceiling closes this — the bound is on the
wrong side of the hazard and the lost row is a gap *inside* the window. `rowversion` with
`MIN_ACTIVE_ROWVERSION()` is the mechanism that does close it, and that is the change
`SOLUTION.md` names as the one to make before trusting this under real write concurrency.
Change Tracking, used for the three mutable tables, was never exposed to it: its versions are
assigned at commit.

What ships instead is the bound **plus detection** — the scorecard's contiguity check, which
counts `COUNT(*)` against `MAX(id) - MIN(id) + 1` over each loaded window. This probe also
measured that it works: the snapshot saw 3 rows under a ceiling of 4, which is one detectable
gap.

**Why the demo never shows this:** the fixture is single-writer, so no transaction is ever open
while another commits. Unreachable here, routine in production.

### Run it

The source must already be built (`docker compose up …`, or at least `pipeline build`).

```bash
docker compose run --rm --entrypoint python pipeline probes/watermark_race.py
```

It creates and drops its own throwaway table, `dbo.wm_probe`. That table is absent from
`pipeline/tables.yml`, which is a default-deny allow-list, so nothing it does is replicated
and nothing it does touches a table the pipeline reads.
