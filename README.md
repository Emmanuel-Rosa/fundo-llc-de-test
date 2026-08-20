# Fundo LLC — change capture, identity resolution, reconciliation

A replication pipeline from a SQL Server source into a local warehouse, with per-table
change-capture strategies, an identity resolver for duplicate customers, and a scorecard
that reads **both sides** to decide whether the copy is faithful.

> Built with agents executing against written contracts. The architecture, the schema and
> every deliberate defect in it, the capture strategy per table, and the verification are
> mine. **"How this was built"** in `SOLUTION.md` has the delegation boundary as a table,
> and the evidence for it.

`SOLUTION.md` is the reasoning. This file is how to run it and what you will see.

---

## Run it

**Prerequisite:** Docker, able to give one container ~5 GB. That is under the Docker Desktop
default on any machine that can run SQL Server 2022 at all.

The source is **SQL Server 2022**, which the brief prefers, so there is no
substitution trade-off to declare. The warehouse is **DuckDB** — a local stand-in for
BigQuery, chosen because it is a file in a volume with no server to run and it is strict
enough about types to catch the cross-engine problems that matter (see "value parity" below,
where two `FLOAT` columns are omitted by type rather than reconciled with a tolerance).

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
docker compose up --abort-on-container-exit --exit-code-from pipeline
```

That is the whole thing: schema, seed, four load runs with two days of simulated churn in
between, four identity resolutions, the reconciliation scorecard, and the identity score.
It takes about eight minutes, most of it in the first load.

**Those two flags are load-bearing**, and I measured both rather than trusting the obvious
one. `--abort-on-container-exit` stops everything when the one-shot pipeline exits;
`--exit-code-from pipeline` makes the pipeline's code the command's code, so the scorecard's
verdict becomes the build's verdict. The flag that looks right — `--abort-on-container-failure`
— stops containers only when one exits *non-zero*, so on a **successful** run it stops nothing:
the demo finishes and `up` streams SQL Server's log forever without returning.

What that buys you: the run fails on a **replication defect** and not on the source's own known
dirt. See "Three verdicts" below.

### Then the one extra command — the failing-check demo

```bash
docker compose run --rm pipeline break
```

That is the whole submission: **one command to run it, one command to see a check fail.**
`break` rewrites two historical amounts in the source, runs a load (which reports a
perfectly clean run, because no id moved), runs the scorecard (which FAILs and names the
rows), restores, and verifies the restore by re-reading. It exits 0 when all of its own
predictions held. Nothing it changes is left changed — see "Safety" below.

### Everything else is a convenience, not a step

You never need these to evaluate the submission; `up` runs all of it.

```bash
docker compose run --rm pipeline test     # the 465 pure checks on their own
docker compose run --rm pipeline check    # just the scorecard, just the exit code
docker compose run --rm pipeline runs     # the ops.load_run history as a table
```

Every command writes a transcript to `docs/<command>.log` on the host, so a run is evidence
rather than scrollback. Those are git-ignored, so following this README does not leave you
with a dirty working tree.

**If you would rather read a run than wait for one:** `docs/reference-run.txt` is a complete
transcript of `docker compose up` on a clean checkout — every number quoted in this file and
in `SOLUTION.md` can be found in it. It is the only transcript committed, and no command
overwrites it.

To start over: `docker compose down -v`.

### Safety — nothing destructive without a way back

- `break` restores every value it changed and **verifies the restore by re-reading**, rather
  than trusting a rowcount. The values it writes back are the ones it read; nothing is
  hardcoded, so it cannot corrupt the fixture the day the seed changes.
- The identity merge is **indirection, never mutation**. No source row is rewritten or
  deleted, so an unmerge is a one-row edit in `meta.customer_map`, not a restore.
- The source is mounted **read-only** into the pipeline container, so the pipeline cannot
  rewrite its own inputs and quietly change the numbers it reports.
- Everything lives in two named volumes. `docker compose down -v` is the full reset and it
  touches nothing outside Docker.

---

## What you will see

### Four load runs, and the fourth is the point

| run | what happened | rows read | payload |
|---|---|---|---|
| 1 | initial load | 288,000 | 22,397.1 KiB |
| 2 | churn day 1 | 4,179 | 327.9 KiB |
| 3 | churn day 2 | 1,326 | 100.6 KiB |
| 4 | **replay, zero churn** | **0** | **0.0 KiB** |

A day's delta moves **1.46%** of a full copy's bytes — a **68×** reduction — and a replay
with no source activity reads nothing at all. Run 4 is the idempotency proof: the second
identical run is free, which is the property that makes a schedule safe.

Payload is labelled honestly. It is the size of the **values** transferred, excluding TDS
framing, column metadata and TLS overhead — a floor, and the right basis for a
delta-versus-full-copy comparison because both sides are measured the same way. It is the
wrong basis for a bandwidth bill.

### Two capture strategies, chosen per table

| table | rows | strategy | why |
|---|---|---|---|
| `customers` | 4,984 | Change Tracking | rows mutate and get hard-deleted |
| `advances` | 8,021 | Change Tracking | rows mutate |
| `cards` | 6,999 | Change Tracking | rows mutate |
| `transactions` | 253,700 | bounded high-water | append-only, and 86.8% of everything replicated |
| `customer_history` | 19,600 | bounded high-water | append-only, fastest-growing table in the source |

One table dominates the row count and contributes almost none of the daily change. That
asymmetry is the entire argument for choosing per table instead of uniformly, and it only
exists because one table dominates.

**The watermark is the clustered IDENTITY key, not a timestamp.** `posted_at` exists, is
indexed, and is the obvious choice — and it is wrong: 150 of each day's 2,800 inserts are
backdated, so a `posted_at` watermark loses **745 of 50,000** rows, permanently, with no
error. That number is measured, not argued.

**And the high-water mark is not safe under concurrent writers, which the code says out
loud.** An IDENTITY value is assigned at INSERT and made visible at COMMIT, so a row can be
uncommitted when the read's snapshot opens and still carry an id *below* the ceiling that
snapshot takes. Measured with two connections: A holds id 3 open, B commits id 4, the
snapshot sees `[1, 2, 4]` and takes a ceiling of 4 — and row 3 is then permanently below the
watermark. There is no `MIN_ACTIVE_IDENTITY`, so bounding the ceiling cannot fix it. What
ships is the bound **plus detection**: the scorecard's contiguity check. The full argument is
in `pipeline/source.py::high_water_ceiling`.

### Three verdicts, not two

The scorecard's middle verdict is the one that matters.

| verdict | meaning | exit |
|---|---|---|
| `PASS` | the two sides agree | 0 |
| `SOURCE-DIRTY` | they agree, and what they agree on is bad data the source really holds | **0** |
| `FAIL` | the warehouse does not say what the source says — a replication defect | 1 |

A clean run of this fixture ends **SOURCE-DIRTY, exit 0**: the source orphans 13 card rows,
17 transaction rows and 20 history rows whose parent customers were hard-deleted, and all of
them are orphaned on *both* sides. Faithful replication of bad data is not a replication
defect, and repairing it in flight would hide a source problem behind a clean-looking
warehouse.

A two-valued verdict would fail every clean run on those orphans, which forces a hardcoded
expected-orphan count — a stale constant with a maintenance schedule. This repo deleted one
of those already; it is not adding another.

### Five checks

| check | clean result |
|---|---|
| key parity | PASS — 4,984 / 8,021 / 6,999 / 253,700 / 19,600 keys matched |
| value parity | PASS — 37 / 26 / 34 / 26 / 19 column aggregates agree, **2 omitted** |
| append-only | PASS — frozen segments agree on row count and every summable column |
| contiguity | PASS — both high-water windows dense, no id missing below the watermark |
| referential integrity | SOURCE-DIRTY — 13 / 17 / 20 orphans, all on both sides |

The two omitted columns are `advances.principal_amount` and `advances.fee_amount`, both
`FLOAT`. They are omitted **by type, not by name** — which is how the second one was found,
since nothing had listed it. A float sum is not reproducible across two engines, so the
choice is to state the omission or to reconcile with a tolerance and call it a pass. It is
stated.

Each check also prints what it *cannot* see. Key parity cannot catch a swap that preserves
count, sum, min and max simultaneously. Append-only cannot see two edits that cancel exactly.
Contiguity cannot see a gap below the lowest id present. Those are limits of the method, and
a check that hides its limits is worse than one that names them.

### The break demo

`docker compose run --rm pipeline break` rewrites history below the watermark, twice, and
restores both times.

**Break 1 — the rewrite.** Two of the oldest transaction amounts altered in place. The loader
then runs and reads **0 rows**, because no id moved: a perfectly clean load report over a
warehouse that is now wrong. The scorecard is what tells you.

```
value parity  FAIL  transactions.amount_cents SUM: source 1261751082 vs warehouse 1261713427  delta -37,655
                      transaction 1  amount_cents  source 78765 -> warehouse 28765
                      transaction 2  amount_cents  source -20947 -> warehouse -8602
append-only   FAIL  transactions: HISTORY WAS REWRITTEN below the watermark
OVERALL FAIL  exit 1
```

**Break 2 — the swap.** The same two rows exchange amounts. COUNT, SUM, MIN and MAX are
identical *by construction*, so every aggregate check passes and the scorecard returns
SOURCE-DIRTY with two transactions still attributed to the wrong rows. That blind spot was
documented in two modules before it was ever executed; this runs it. Catching it needs a
row-level comparison, which the checks deliberately do only for a table whose aggregates
already disagree — because doing it always costs what the full copy this pipeline replaced
cost.

Both restores write back values that were **read, not hardcoded**, and are verified by
re-reading. The demo grades its own predictions and exits non-zero on a surprise in either
direction: the rewrite escaping would be a hole in the checks, and the swap being caught
would mean two modules describe their own limits wrongly.

---

## Identity resolution

**The merge is indirection, never mutation.** `meta.customer_map` maps every customer to a
canonical id; no source row is ever rewritten or deleted, so an unmerge is a one-row edit
rather than a restore.

**Email SUGGESTS, it never PROVES.** What proves identity here is `customer_id`, the tuple
`(ssn_last4, date_of_birth, last_name)`, or a prior human `manual_merge`. First name is
excluded — nicknames are not evidence. Surname is included, and CF-4 below prices that.

**Money outranks test-data patterns.** A customer who has moved money is never
auto-excluded as a test artifact; the verdict records which rules *would* have fired and a
human is told why. Deleting a funded advance to tidy up test data removes real money from the
book with no error anywhere.

**Cards are never re-pointed.** A card keeps the `customer_id` it was issued against and
reaches its person *through* the map — the same indirection the merge is. An unmerge therefore
needs no card restore. And the consequence is counted rather than warned about: **0** customers
hold two default cards on their own, so the source's one-default-per-customer invariant is
intact — while **27** canonical customers hold two after resolution, and *all* of them span
more than one original customer. Every conflict is created by merging. Nothing can then say
which card a charge should hit, so it is reported and deliberately not repaired.

**Placeholders are detected by shape, not by a blocklist** — `repeated_digits`,
`sequential_digits`. A blocklist is a list someone has to keep adding to, and it fails
silently in the expensive direction.

### Measured, on this fixture

| | |
|---|---|
| mirror customers | 4,984 |
| excluded as test accounts | 14, plus 2 money-vetoed |
| population reaching resolution | 4,970 |
| duplicate groups / rows / truth pairs | 41 / 91 / **61** |
| duplicate groups the resolver formed | 35 |
| **shipped result** | 47 proposed, 47 correct, **0 wrong**, 14 missed |
| | **precision 1.000, recall 0.770** |
| malformed / placeholder contacts | 6 phones, 1 email — out of 4,970 each |
| canonical customers with 2+ default cards | 27, all of them merge-caused |

The resolver forms **35** groups against **41** in truth; that gap is the 14 missed pairs, and
it is the honest version of "41 groups found".

The 61 truth pairs solve uniquely to 34 pairs + 5 triples + 2 quads: `34 + 3·5 + 6·2 = 61`
and `68 + 15 + 8 = 91`. That composition is **derived from the run**, never asserted — a
written-down fixture composition drifts away from the fixture, silently.

**On the precision figure: it is a self-consistency check, not a measurement.** It says the
resolver proposed nothing that the seeded ground truth disagrees with. It does not say the
rules would hold on Fundo's real book, and nothing in a synthetic fixture can say that. Only
a labelled sample of your real duplicates could measure this. The recall figure is the more
useful of the two, and it is the lower one.

**The 14 misses are partitioned by cause, from the run, with nothing unexplained:** 4 have no
proof tuple, 2 have proof tuples that disagree, and 8 are refused by the resolver. The 8
refusals are one four-member group contributing `C(4,2) = 6` pairs, plus two more. A miss
this resolver *chose* to make is a different thing from a miss it failed to find, and the
partition is what makes them distinguishable.

### The withdrawal exhibit

The map is re-derived from current state on every run and never patched forward. Churn day 1
gives one group a fourth member; churn day 2 gives that member a funded advance; the next
resolution **withdraws** the merge it previously made — rows merged away drop 42 → 39 and
review rows rise 4 → 5 — with no source-data repair anywhere. Resolved once at the end, that
is invisible. It is the reason the demo resolves four times instead of one.

Resolution 4 matches resolution 3 on all eight counts, which is the identity half of the
idempotency proof.

### What each removed rule costs

Six counterfactuals, each re-resolved and re-scored against the baseline.

| | variant | prec | rec | measured cost on this fixture |
|---|---|---|---|---|
| SHIPPED | | 1.000 | 0.770 | 47 proposed, 47 correct, 0 wrong, 14 missed |
| CF-1 | email promoted to PROVES | 0.961 | 0.803 | +2 wrong (C2044+C2045, C3311+C3312), +2 recovered |
| CF-2 | placeholder shape gate removed | 1.000 | 0.770 | nothing moves — same pairs, same population |
| CF-3 | `first_name` added to the proof tuple | 1.000 | 0.770 | nothing moves — same pairs, same population |
| CF-4 | `last_name` dropped from the proof tuple | 0.980 | 0.787 | +1 wrong (C0654+C3078), +1 recovered (C2680+C4471) |
| CF-5 | exclusion moved to *after* resolution | 1.000 | 0.770 | pairs identical, **population +14**; priced jointly with CF-2 at 0.904 |
| CF-6 | naive `'%test%'` filter | 1.000 | 0.770 | pairs identical, **population −7 real / +7 synthetic** |

Read that table carefully, because four of the six rows look like the shipped one and only two
of those four mean it.

**CF-2 and CF-3 genuinely change nothing** — same pairs, same population — so their value rests
on argument rather than measurement, and that is better said than dressed up. CF-3 is justified
by one Robert/Bobby group that never even reaches the first-name test, because it was already
missed for a missing `ssn_last4`.

**CF-5 and CF-6 are a different thing entirely, and finding that out is what the harness was
fixed for.** Every variant is scored against a truth denominator bounded by *its own*
population, so a variant that deletes customers deletes its own errors from the denominator
too. CF-6's naive filter removes 7 real people and fails to exclude 7 synthetics the shipped
rules catch; CF-5 puts all 14 excluded accounts back. Both therefore produce a headline row
identical to shipped while grading themselves over a different population — and the report used
to print *"changes NOTHING on this fixture … does not price the rule"* immediately before
pricing what it changed. It now reports a population change **as** a change, in both
directions, and the "prices nothing" sentence is reachable only when the population really is
unchanged.

---

## Tests

```bash
docker compose run --rm pipeline test
```

**465 checks across 5 suites, no database and no network.** Plain asserts, no test framework
— the dependency list is `pymssql`, `duckdb` and `PyYAML`, and a test runner would be a
fourth for no gain at this size.

| suite | checks | covers |
|---|---|---|
| `test_rules` | 274 | the identity rules and their precedences |
| `test_normalize` | 83 | normalization, including what it refuses to normalize |
| `test_watermark` | 50 | the closed window, ceiling discipline, and the gap the bound cannot close |
| `test_checks` | 44 | the reconciliation arithmetic, and engine rules a fake cannot enforce |
| `test_break_demo` | 14 | the two ways the break demo could stop demonstrating anything |

Suites are discovered by glob, not listed, so a new file cannot be silently skipped. Each
runs in its own interpreter, because two of them install a `pymssql` stub and a harness whose
verdict depends on run order is worse than no harness.

`test` runs first inside `demo` and aborts before the source build if anything is red. None of
it needs a database and the build takes minutes; failing the cheap deterministic half before
spending them is the point of having a cheap half.

Some assertions check **generated SQL text** rather than a result. That is deliberate and it
is not thoroughness for its own sake: the first live scorecard run died on `MIN([is_default])`
because SQL Server refuses min/max on `bit`, and no fake engine could have caught it — a fake
answers whatever it is handed. Those tests encode the engine rules that bit the real thing.

---

## Layout

```
docker-compose.yml     two services: SQL Server 2022 (pinned by tag AND digest), and the pipeline
Dockerfile             Python 3.12 — pymssql and duckdb both ship cp312 wheels, so no compiler
pipeline/
  tables.yml           the replication manifest: which tables, which columns, which strategy
  config.py            everything tunable. A number buried in a loader disagrees with the
                       write-up six weeks later
  source.py            SQL Server reads, and the three silent-failure modes they defend against
  warehouse.py         DuckDB, schema and the transactional apply
  load.py              one run across all five tables, one snapshot for the whole run
  identity/            normalize → rules → resolve → score
  checks/              key parity, value parity, and the scorecard
  break_demo.py        rewrite history, catch it, restore, verify
  selftest.py          the suite runner
  transcript.py        duplicates a run's output into docs/
probes/                evidence scripts. No command needs them; see probes/README.md
sql/source/            schema, seed, then enable capture — in that order, always
sql/demo/              two days of simulated churn
tests/                 465 plain-assert checks
docs/                  run transcripts land here
```

`tables.yml` and `sql/source/01_schema.sql` are the two files to read first. The schema
carries deliberate defects, each one labelled, and each one produces a measured consequence
in the run output rather than a comment claiming it would.

## Notes for a reviewer

- **The image is pinned by digest as well as tag**, both verified against the registry. `latest`
  is a moving target and the reviewer's numbers must be my numbers. There is no official arm64
  image; on Apple Silicon this runs under emulation Microsoft does not test.
- **`MSSQL_MEMORY_LIMIT_MB` is 4096, and that is measured rather than chosen.** At 2048 —
  exactly the documented floor — SQL Server 2022 CU20 reports 213 MB available and dies with
  a stack overflow in `sqlpal.dll`. It is not an OOM kill; Docker had 16.6 GB. A limit that
  depends on the host's mood is not a limit a reviewer can reproduce.
- **The schema and seed are applied by Python over `pymssql`, not by `sqlcmd`.** The `.sql`
  files keep their `GO` separators so they stay copy-pasteable into SSMS, and a splitter
  honours them. `sqlcmd`'s path moved between cumulative updates and some published images
  shipped without a tools directory at all.
- **No `run.sh`, no Makefile.** `make` is not present on a default Windows box, and a mounted
  `.sh` fails on CRLF with a message that points nowhere near the cause.
- **Report output is ASCII only.** Box-drawing characters crash a default cp1252 Windows
  console, and a reviewer piping `docker compose logs` through one is a real path.
