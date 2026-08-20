# Fundo LLC: change capture, identity resolution, reconciliation

This copies data out of a SQL Server database into a warehouse, and it only copies what
changed. It also finds duplicate customer records and merges them, and it checks its own
work by comparing both databases against each other.

> I built this with AI agents doing the typing, working from specs I wrote. The
> architecture, the schema and the deliberate flaws in it, the choice of capture strategy per
> table, and all of the verification are mine. There's a section in `SOLUTION.md` called
> "How this was built" with the split written out and the evidence for it.

`SOLUTION.md` explains the decisions. This file is how to run it and what you'll see.

---

## Run it

**You need Docker**, able to give one container about 5 GB. That's under the default on any
machine that can run SQL Server at all.

The source database is **SQL Server 2022**, which is what you said you use, so there's no
substitution to explain. The warehouse is **DuckDB**, standing in for BigQuery. I picked
DuckDB because it's just a file, with no server to run, and because it's strict about types.
That strictness caught two real problems a more forgiving database would have hidden.

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
docker compose up --abort-on-container-exit --exit-code-from pipeline
```

That's everything: it builds the schema, seeds it, runs four loads with two days of
simulated activity in between, resolves identities after each one, then runs the checks and
scores the result. Takes about eight minutes, most of it in the first load.

**Those two flags matter, and I tested both.** `--abort-on-container-exit` stops everything
once the pipeline finishes. `--exit-code-from pipeline` makes the pipeline's exit code the
command's exit code, so the checks decide whether the run passed. The flag that looks right,
`--abort-on-container-failure`, only stops things when a container fails, so on a run that
*succeeds* it stops nothing and the command never comes back to your prompt. I found that out
by running it.

### Then one more command: the failing-check demo

```bash
docker compose run --rm pipeline break
```

Two commands total: one to run it, one to watch a check catch a problem. `break` edits two
old transaction amounts in the source database, runs a load (which reports a completely clean
run, because editing a row doesn't change its ID), then runs the checks, which fail and name
the exact rows. Then it puts the values back and proves it put them back. More on this below.

### Everything else is optional

You don't need any of these. `docker compose up` runs all of it.

```bash
docker compose run --rm pipeline test     # just the tests: 478 of them, no database needed
docker compose run --rm pipeline check    # just the checks, and the exit code
docker compose run --rm pipeline runs     # a table of every load and what it moved
```

Every command writes what it printed to `docs/<command>.log` on your machine, so you can read
a run back after it has scrolled past. Those files are git-ignored, so following these
instructions won't leave you with uncommitted changes.

**If you'd rather read a run than wait for one**, `docs/reference-run.txt` is the complete
output of `docker compose up` on a fresh checkout. Every number in this file and in
`SOLUTION.md` is in there.

To start over: `docker compose down -v`.

### Nothing here is destructive

- `break` puts back every value it changed, then **re-reads them to confirm**. It doesn't
  trust a rowcount. The values it writes back are the ones it read a moment earlier, so
  nothing is hardcoded and it can't corrupt the data if the seed ever changes.
- Merging duplicates never edits or deletes a customer row. It writes a separate mapping
  table. Undoing a merge is a one-row change rather than a restore from backup.
- The pipeline container mounts the source SQL files **read-only**, so it can't rewrite its
  own inputs and quietly change the numbers it reports.
- Everything lives in two Docker volumes. `docker compose down -v` removes them and touches
  nothing else.

---

## What you'll see

### Four loads, and the fourth one is the interesting one

| load | what happened | rows read | data moved |
|---|---|---|---|
| 1 | first load, everything | 288,000 | 22,397.1 KiB |
| 2 | after one day of activity | 4,179 | 327.9 KiB |
| 3 | after another day | 1,326 | 100.6 KiB |
| 4 | **run again, nothing changed** | **0** | **0.0 KiB** |

The busier of the two simulated days moves **1.46%** of what a full copy moves, about **68×
less**; the quieter one moves 0.45%. Run 4 matters too: running it again when nothing has
changed reads nothing at all, which is what makes it safe to put on a schedule.

About that "data moved" number: it's the size of the *values* transferred. It leaves out
protocol overhead and encryption, so it's a floor rather than a network bill. It's a fair
basis for comparing a delta against a full copy because both are measured the same way.

### Two capture strategies, one per table

| table | rows | strategy | why |
|---|---|---|---|
| `customers` | 4,984 | Change Tracking | rows get edited and deleted |
| `advances` | 8,021 | Change Tracking | rows get edited |
| `cards` | 6,999 | Change Tracking | rows get edited |
| `transactions` | 253,700 | high-water mark | only ever inserted, and it's 86.8% of all the rows |
| `customer_history` | 19,600 | high-water mark | only ever inserted, fastest-growing table here |

Change Tracking is a SQL Server feature that tells you which rows changed since last time,
including which ones were *deleted*. A high-water mark is simpler: remember the highest ID you
copied, and next time ask for anything above it. That only works if rows are never edited
after insert, which is true for these two tables.

One table is most of the rows and almost none of the daily change, which is why the strategy
is chosen per table instead of once for everything.

**The high-water mark uses the ID column, not a timestamp.** There's a `posted_at` timestamp
that looks like the obvious choice, and it's wrong. About 150 of each day's 2,800 inserts are
backdated, so their timestamp is older than rows already copied. I worked out what that
would actually have cost: of the 3,700 rows that arrived after the first load, a
timestamp-based mark would have missed **129 of them**, 84 on the first day and 45 on the
second, about 3.5%. Nothing in the load report would have flagged it.

**And the high-water mark isn't safe when several things write at once. The code says so.**
SQL Server hands out an ID when a row is inserted, but the row only becomes visible when its
transaction commits, and those are two different moments. So a row can be invisible to your
read while a *higher* ID has already committed. I set that up with two connections: one
inserts ID 3 and holds the transaction open, another inserts ID 4 and commits. A reader then
sees IDs 1, 2 and 4, and records 4 as the high-water mark. When ID 3 finally commits it's
already below the mark and never gets read. In the probe that's 50,000 cents that never reach
the warehouse, with nothing in the output to say so.

There's no way to fix this by picking a smarter mark; SQL Server has a function for exactly
this problem (`MIN_ACTIVE_ROWVERSION`) but only for a different column type. So what ships is
the mark **plus a check that detects the gap**. The full argument is in
`pipeline/source.py`, and the script that proved it is in `probes/`, so you can run it
yourself.

### Three results, not two

| result | what it means | exit code |
|---|---|---|
| `PASS` | both databases agree | 0 |
| `SOURCE-DIRTY` | they agree, and what they agree on is bad data that really is in the source | **0** |
| `FAIL` | the warehouse doesn't match the source, so the copy is broken | 1 |

The middle one is the design decision worth arguing about. A clean run of this ends
**SOURCE-DIRTY**: the source has 13 card rows, 17 transaction rows and 20 history rows whose
customer was deleted out from under them. All of them are orphaned in *both* databases, so the
copy is faithful. That's a problem with the source, not with the pipeline, and cleaning it up
in transit would leave the warehouse looking fine while the source stayed broken.

With only pass and fail, every clean run would fail on those orphans. The usual fix is to
hardcode "expect 20 orphans", which goes stale the first time the data changes. An earlier
version of this code did exactly that and printed a discrepancy that wasn't real.

### Five checks

| check | clean result |
|---|---|
| key parity | PASS: 4,984 / 8,021 / 6,999 / 253,700 / 19,600 rows matched |
| value parity | PASS: 37 / 26 / 34 / 26 / 19 column comparisons agree, **2 skipped** |
| append-only | PASS: old history is unchanged in both databases |
| no gaps in IDs | PASS: nothing missing below the high-water mark |
| referential integrity | SOURCE-DIRTY: 13 / 17 / 20 orphans, in both databases |

The two skipped columns are `advances.principal_amount` and `advances.fee_amount`, both
floating-point. Floats don't add up identically across two different database engines, so I
skip them **based on their type, not by name**. That's how I found the second one, since
nothing had it written down anywhere. The alternative was to compare them with a fudge factor
and call it a pass, and I'd rather report the skip.

Each check also prints what it *can't* catch. The row-matching check can't spot a swap that
keeps the count, sum, minimum and maximum all the same. The history check can't see two edits
that cancel out. The gap check can't see a gap below the lowest ID it holds, and it reports a
gap for a harmless cause too: a transaction that rolls back uses up an ID permanently, so a
gap is a strong hint rather than proof. Those are real limits, so each check prints its own
rather than leaving you to find them.

### The failing-check demo

`docker compose run --rm pipeline break` breaks things twice and puts them back both times.

**First: editing history.** Two old transaction amounts get changed. Then the loader runs and
reads **0 rows**, because editing a row doesn't change its ID and the high-water mark only
looks at IDs. So the load report is spotless while the warehouse is now wrong. The checks are
what tell you:

```
value parity  FAIL  transactions.amount_cents SUM: source 1261751082 vs warehouse 1261713427  delta -37,655
                      transaction 1  amount_cents  source 78765 -> warehouse 28765
                      transaction 2  amount_cents  source -20947 -> warehouse -8602
append-only   FAIL  transactions: HISTORY WAS REWRITTEN below the watermark
OVERALL FAIL  exit 1
```

**Second: swapping two amounts.** The same two rows trade values. Now the count, the total,
the minimum and the maximum are all *identical*, so every check passes while two transactions
stay attributed to the wrong rows. This limitation was written down in two files before anyone
ran it, and running it is what turned a claim into a demonstration. Catching the swap needs a
row-by-row comparison, which the checks only do for a table that already looks wrong, because
doing it every time would cost as much as the full copy this replaces.

---

## Duplicate customers

**Merging never edits a customer row.** There's a mapping table that says "customer 4290 is
really the same person as customer 1866". Nothing is rewritten, nothing is deleted, and
undoing a merge means changing one row in that map.

**Some fields prove two records are the same person. Others only hint at it.** Getting this
backwards merges people who aren't the same person, which for a lender means mixing up two
people's money.

| proves it | only hints at it |
|---|---|
| the customer ID itself | email address |
| last 4 of SSN + date of birth + surname, together | phone number |
| a human who already merged them by hand | postal address |
| | two records sharing a saved card |

Email is the trap. I tested promoting it to "proves": it correctly merges 2 more pairs and
**wrongly merges 2 pairs that aren't the same person**. Households share a mailbox, so an email
address identifies a mailbox, not a person. There are four cases in the test data that make
that happen: a family address, a shared office inbox, roommates, and a father and son with the
same name.

First names are left out. Robert and Bobby can be the same person and the field gives you no
way to tell, so a first-name match adds noise rather than evidence. Surnames are in, and I
tested taking them out too: it correctly catches one person who changed their name after
marriage, and wrongly merges two strangers who happen to share a birth date and the last four
digits of their SSN.

**Fake-looking values are caught by their shape, not by a list.** `0000`, `1234`, `5555555555`,
anything that's all the same digit or a straight run. A list of known-bad values needs someone
to keep adding to it, and when it misses one the failure is silent: an unrecognised
placeholder looks like strong evidence and merges two strangers.

### The four rules you gave me

**1. A customer with a funded or paid-off advance is untouchable.** They're never
auto-excluded as test data. And if a group has *two* people who have both moved money, I don't
merge it at all and I don't guess. The merge is **withdrawn** and it goes to a human. You can
watch that happen: across the two days of activity, merged-away rows go 42 → 39 and the review
queue goes 4 → 5, because someone in an already-merged group got funded.

The review note includes each person's full repayment-account fingerprint, which is a stable
identifier for the bank account their repayments come from. The question a human actually needs
to answer is whether these two repay from the *same* account.
Two funded people repaying from different accounts with overlapping dates isn't a data-quality
ticket; for a cash-advance lender that's a fraud signal. A shortened fingerprint can't answer
that question, so it isn't shortened.

**2. Test data is excluded, not merged, and the obvious pattern catches real people.**
Three rules: a company-domain email *and* an automation-looking name before the @; a
`+test`/`+qa` tag in the address; or placeholder values like SSN `0000` with birth date
1900-01-01. That excludes 14 accounts, and vetoes 2 more because they've moved money.

Then I ran the naive version, `LIKE '%test%'` on name and email, as a comparison. It flags
14 customers too (a different 14, and the coincidence is unhelpful), and **7 of them are real
people**: someone with the surname Testerman who has a
funded advance, two other real surnames, and four people whose email addresses merely contain
the letters "test" (`greatest.deals@`, `protest.organizer@`, `contest.winner1994@`,
`latest.news@`). Half of what it catches is wrong, and one of the wrong ones has real money
attached. Staff testing from company addresses survive my version because it needs the company
domain *and* an automation-shaped name, rather than either one on its own.

**3. Bad phone numbers and emails: found, counted, and left alone.** There are four
categories rather than two (valid, malformed, placeholder, and missing), and keeping them
apart matters because `placeholder` is the dangerous one: `5555555555` is a perfectly
well-formed phone number that identifies nobody. Out of 4,970 customers there are **6
malformed phone numbers and 1 placeholder email**.

I don't fix them and I don't drop the records. I refuse to use them as evidence of identity.
Fixing a phone number invents a way to contact somebody. Dropping the record loses a real
customer. Refusing the field only costs the matches that field would have made, and a broken
value shouldn't be making matches.

That choice has a cost and it's also counted: when two records merge, the survivor's details
are kept whole rather than cherry-picking the best value from each. So one surviving customer
keeps a broken phone number when a merged-away duplicate had a good one. The alternative is to
assemble one contact record out of pieces of several people, which is worse to unpick when the
merge turns out to be wrong.

**4. Cards stay where they are, and this is where I found something I didn't expect.** A
card keeps pointing at the customer it was added to, and gets to the right person through the
same mapping table the merge uses. Nothing is re-pointed, so undoing a merge doesn't need to
undo anything about cards.

Here's what breaks if you get it wrong, as a number rather than a warning. In the source
database **no customer has two default cards**, so that rule is intact. After merging,
**27 customers have two**, and every one of those is a case where two people were merged
into one. All 27 are caused by the merge and none were already there. Two people each
picked their own default card, they turn out to be the same person, and now nothing says
which card to charge.

The pipeline reports this and deliberately **does not fix it**. Picking a winner between two
cards a customer chose is a billing decision, not a data decision, and silently choosing one
would have the pipeline decide something about a customer's money that nobody asked it to
decide. Of everything in here, this is the one I'd take to the business rather than settle
myself.

### The numbers

| | |
|---|---|
| customers in the warehouse | 4,984 |
| excluded as test accounts | 14, plus 2 vetoed for having moved money |
| customers actually considered | 4,970 |
| real duplicate groups in the data | 41, covering 91 records, 61 pairs |
| groups the resolver found | 35 |
| | *precision = how often a proposed merge was right;* |
| | *recall = how many of the real duplicates it found* |
| **what it proposed** | 47 pairs, 47 of them correct, **0 wrong**, 14 missed |
| | **precision 1.000, recall 0.770** |
| bad contact details | 6 phone numbers, 1 email |
| customers with 2 default cards after merging | 27, all of them caused by merging |

**That precision of 1.000 is less impressive than it looks, and I'd rather say so than let it
sit there.** It means the resolver didn't propose anything the test data disagrees with. It
does *not* mean these rules would score 1.000 on your real customers, and nothing built on
made-up data could tell you that. Only labelling a sample of your actual duplicates would.
Recall, the lower number at 0.770, is the more useful one here.

The 14 misses are broken down by cause, worked out from the run rather than written down in
advance: 4 have no usable identifying fields, 2 have identifying fields that disagree, and 8
are cases the resolver deliberately refused. Those last 8 are refusals rather than failures,
which is why they're counted apart from the rest.

### Merges get withdrawn, not patched

The map is rebuilt from scratch on every run and never edited in place. That sounds like a
detail, and it's what makes the withdrawal above work: day one gives a merged group a fourth
member, day two gives that member a funded advance, and the next run *un-merges* what the
previous run merged, without anyone editing any data. Resolving only once at the end would
never show it, which is why the demo resolves four times.

Run 4 produces an identical map to run 3 on all eight counts, which is the other half of the
"running it twice is safe" claim.

### What each rule is worth

I removed one rule at a time, re-ran the whole thing, and re-scored it.

| | change | precision | recall | customers scored | what it cost |
|---|---|---|---|---|---|
| | as shipped | 1.000 | 0.770 | 4,970 | 47 proposed, 47 correct, 0 wrong, 14 missed |
| CF-1 | trust email as proof | 0.961 | 0.803 | 4,970 | 2 wrong merges, 2 correct ones found |
| CF-2 | stop filtering placeholder values | 1.000 | 0.770 | 4,970 | nothing changes |
| CF-3 | add first name to the match | 1.000 | 0.770 | 4,970 | nothing changes |
| CF-4 | drop surname from the match | 0.980 | 0.787 | 4,970 | 1 wrong merge, 1 correct one found |
| CF-5 | exclude test accounts *after* merging | 1.000 | 0.770 | **4,984** | same merges, the 14 excluded accounts back in |
| CF-6 | use the naive `%test%` filter | 1.000 | 0.770 | **a different 4,970** | same merges, 7 real people dropped, 7 fakes kept |

Four of those rows carry the shipped precision and recall, and the **customers scored** column
is what tells them apart. CF-2 and CF-3 were graded on the same 4,970 people. CF-5 was graded
on 14 more. CF-6 was graded on a set of the same size, with 7 real people swapped out for 7
fakes, so its 1.000 and its 0.770 aren't comparable to the shipped ones at all.

**CF-2 and CF-3 genuinely change nothing here.** Their value rests on reasoning rather than
measurement, and I'd rather say that than quote a number from somewhere else. CF-3 is there
because of one Robert/Bobby pair, which never even reaches the first-name test, because it
was already missed for a missing SSN.

**CF-5 and CF-6 are a different story, and finding that out is why I rewrote this part.** Each
variation is scored against the customers *it* ended up with. So a variation that deletes
customers also deletes its own mistakes from the marking scheme. CF-6 drops 7 real people and
keeps 7 fakes that the proper rules catch; CF-5 puts all 14 excluded accounts back in. Both
end up with a headline row identical to shipped while being graded on a different set of
people. The report used to print "changes nothing on this fixture" one sentence before pricing
what it changed. It now reports a change in who got scored as a change, which is what the
extra column above is for.

---

## Tests

```bash
docker compose run --rm pipeline test
```

**478 checks across 6 files, no database and no network needed.** Plain `assert`-style checks,
no test framework. The whole dependency list is three packages, and a framework would add a
fourth without doing anything these checks can't already do.

| file | checks | what it covers |
|---|---|---|
| `test_rules` | 274 | the identity rules and which ones outrank which |
| `test_normalize` | 83 | cleaning up field values, including what it refuses to clean |
| `test_watermark` | 50 | the high-water window, and the gap it can't close |
| `test_checks` | 44 | the comparison arithmetic, and two engine quirks a fake can't catch |
| `test_break_demo` | 14 | the two ways the break demo could stop proving anything |
| `test_connect` | 13 | the cold-start race described below |

Test files are found by pattern rather than listed anywhere, so adding one can't be silently
skipped: `test_connect` was picked up automatically the moment it existed. Each file runs in
its own process, because two of them install a fake database driver, and sharing a process
would make the result depend on which file ran first.

`test` runs first inside `docker compose up` and stops before building anything if something
is red. None of it needs a database and the build takes minutes, so a broken test costs you
seconds instead of the whole run.

A few checks assert on the **SQL text** the code generates rather than on a result. That's
deliberate: the first real run of the checks died on `MIN()` applied to a boolean column, which
SQL Server refuses. No fake database could have caught that, because a fake answers whatever
you hand it. So those tests record the engine rules that actually broke a run.

### One test exists because of a bug that came and went

`docker compose up` on a fresh volume once died after 11 seconds with "SQL Server rejected the
login". The password was right. SQL Server accepts network connections a couple of seconds
before it has finished setting up its own login accounts, and during that window it rejects
logins. The connection code treated any rejected login as fatal, which reads like careful
error handling but is wrong here. Worse, it *worked* several times before it failed, because
whether it passed depended on how fast the database started. `test_connect` is there so it
can't come back.

---

## Layout

```
docker-compose.yml     two containers: SQL Server 2022 (pinned exactly), and the pipeline
Dockerfile             Python 3.12, because both database drivers ship prebuilt for it
pipeline/
  tables.yml           which tables, which columns, which strategy. Anything not listed is not copied
  config.py            everything adjustable, so no number is buried in code where it can drift
  source.py            reading SQL Server, and three ways it can fail silently
  warehouse.py         DuckDB: schema and applying changes in one transaction
  load.py              one run across all five tables
  identity/            clean up fields -> rules -> merge -> score
  checks/              the five comparisons and the scorecard
  break_demo.py        break history, catch it, put it back, prove it
  selftest.py          runs the test files
  transcript.py        saves each run's output into docs/
probes/                scripts that proved specific things. No command needs them
sql/source/            schema, then seed data, then turn on change tracking, in that order
sql/demo/              two days of simulated activity
tests/                 478 checks
docs/                  run output lands here
```

Start with `pipeline/tables.yml` and `sql/source/01_schema.sql`. The schema has deliberate
flaws in it, each one labelled, and each one shows up somewhere in the output rather than only
in a comment that claims it would.

## A few notes for whoever runs this

- **The SQL Server image is pinned by exact digest**, not just a version tag, because `latest`
  moves and your numbers should be my numbers. There's no ARM build of SQL Server, so on an
  Apple Silicon Mac this runs under emulation Microsoft doesn't test.
- **It asks for 4 GB of memory, and that's measured rather than picked.** At 2 GB, which is
  the documented minimum, SQL Server 2022 reports 213 MB free and dies with a stack overflow.
  It isn't the container running out of memory; Docker had 16 GB free. I set it to 4 GB so the
  run doesn't depend on what else your machine is doing that day.
- **The schema and seed data are applied by Python, not `sqlcmd`.** The `.sql` files keep their
  `GO` separators so you can paste them straight into a SQL editor, and the code splits on
  them. `sqlcmd` moved directory between SQL Server updates and some published images shipped
  without it entirely.
- **No `run.sh` and no Makefile.** `make` isn't on a stock Windows machine, and a shell script
  mounted into a container fails on Windows line endings with an error that points nowhere
  near the cause.
- **All output is plain ASCII.** Box-drawing characters crash the default Windows console, and
  piping `docker compose logs` through one is a thing people actually do.
