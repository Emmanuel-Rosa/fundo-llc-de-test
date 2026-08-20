# SOLUTION

I took on all three problems. Every number is marked **[measured]**, meaning it came out of a
run, or **[estimated]**, meaning I'm reasoning rather than counting. `README.md` has the full
tables and what each command prints. This is why I did it this way.

## What I skipped, and why

The brief says an honest gap costs less than a confident guess, so these come first.

- **Stopping duplicates from being created again.** I rebuild the duplicate map from scratch
  on every run, so a duplicate created tomorrow gets merged tomorrow. That catches them, it
  doesn't prevent them. Preventing them means a uniqueness rule inside the operational
  database, which needs write access to it and a product decision about what a customer sees
  when signup gets blocked.
- **Fixing bad phone numbers and emails.** I find them, count them, and refuse to use them.
  Reasoning below.
- **Double-checking whether "funded" is really funded.** The code trusts the `status` column.
  Checking it against the funding date and the repayment account as well would turn a hard rule
  into a judgement call, and every group it changed would need re-scoring. I marked the spot in
  the code and left it.
- **Comparing the two floating-point columns.** Floats don't add up identically on two
  different database engines, so I skip those two and say so rather than comparing them with a
  tolerance and calling it a pass.

---

## 1. Copying only what changed

**I use two strategies because one table dominates.** `transactions` holds 86.8% of all the
rows, and only about 1.1% of it changes on a given day **[measured]**. That imbalance is why
the strategy is chosen per table rather than once for everything.

For `customers`, `advances` and `cards` I use SQL Server's Change Tracking, which keeps a list
of the rows that changed since you last asked. Those tables get edited and rows get deleted,
and the list includes deletes. A "highest ID I've seen" marker can't tell you about a delete,
because a deleted row doesn't get a new ID. For `transactions` and `customer_history`, which
are only ever inserted into, I use that ID marker instead. Change Tracking there would mean the
database keeping per-row records for rows that never change.

**The marker is the ID column, not the timestamp, and I checked before deciding.** There's a
`posted_at` timestamp that's indexed and looks like the obvious choice. It's wrong. About 150 of
each day's 2,800 inserts are backdated, so their timestamp lands below rows that were already
copied. Of the 3,700 rows that arrived after the initial load, a timestamp-based marker would
have silently missed **129** of them: 84 on day one, 45 on day two **[measured]**.

**Reliability.** The first load reads everything directly rather than replaying the change
list, because change tracking is switched on *after* the data is seeded, so asking it for
"everything since the beginning" correctly returns nothing. Deletes are the reason the query
uses an outer join: a deleted row appears in the change list but no longer exists in the table,
so a plain inner join would throw every delete away silently. Running it again with nothing
changed reads **0 rows** **[measured]**. And the copied rows and the updated markers are saved
in **one transaction** across all five tables. A crash halfway through rolls back to a
consistent point rather than leaving one table further ahead than another. None of the checks
would catch that skew, so every report built on top of it would be wrong.

**I changed my mind about the ID marker.** The code used to claim it was safe under
concurrent writes. It isn't: SQL Server assigns an ID when a row is inserted, but the row only
becomes visible when its transaction commits, and those are different moments. So a reader can
miss a low ID while a higher one is already visible, record the higher one as its marker, and
never come back for the row it missed, which was 50,000 cents in the row I tested with
**[measured]**. Picking a smarter marker doesn't help; SQL Server has a function built for this
problem but only for `rowversion` columns, not ID columns. Change Tracking doesn't have the
issue at all, because its version numbers are handed out at commit time, but switching the two
big tables to it brings back the per-row cost I was avoiding. So what ships is the marker
**plus a check that detects the gap**: count the rows and compare against the range of IDs they
cover. That's a strong hint rather than proof, because a rolled-back insert also uses up an ID,
and the check says so. The script that proved all this is in `probes/`.

---

## 2. Duplicate customers

**Merging never edits or deletes a customer record.** A separate table says which customer ID
stands for which person, so undoing a merge is one row. That matters because every rule below is
a judgement call, and I want the wrong ones to be cheap to undo.

**What proves identity:** the customer ID; the combination of last-4 of SSN, date of birth and
surname; or a human who already merged them by hand. **What only hints at it:** email, phone,
postal address, and two records sharing a saved card.

Email is the trap and I priced it rather than arguing about it. Treating email as proof finds 2
more real duplicates and wrongly merges 2 pairs who aren't the same person **[measured]**.
Households share a mailbox, so an email address identifies a mailbox. First names are out:
Robert and Bobby can be the same person and the field gives you no way to tell. Surnames are
in, and taking them out costs 1 wrong merge for 1 correct one **[measured]**: you catch someone
who changed their name after marriage by accepting that two strangers sharing a birth date and
four SSN digits get merged. Placeholder values are caught by their shape rather than by a list
of known-bad ones. A list is something someone has to maintain, and when it misses one the
failure is silent: an unrecognised placeholder looks like strong evidence and merges strangers.

**Funded customers are untouchable.** Anyone who has moved money is never auto-excluded as test
data. When a group contains *two* people who have both moved money, I don't merge it and I don't
guess. The merge is withdrawn and it goes to a person. You can watch it happen across the two
simulated days: merged-away records go 42 → 39 and the review queue goes 4 → 5 **[measured]**.
The review note carries each person's full repayment-account fingerprint, which is a stable
identifier for the bank account repayments come from. The question a human needs to answer is
whether these two repay from the *same* account: two funded people on different accounts with
overlapping dates is a fraud signal for a cash-advance lender rather than a data-cleanup ticket.

**Test data is excluded, not merged, and the obvious pattern really does catch real people.**
My version needs a company-domain email *and* an automation-shaped name before the @, or a
`+test`/`+qa` tag, or placeholder values like SSN `0000` with a 1900 birth date. That excludes
14 accounts and vetoes 2 more for having moved money **[measured]**. I then ran the naive
`LIKE '%test%'` version as a comparison: it flags 14 customers and **7 of them are real
people**, so half of what it catches is wrong, and one of the wrong ones has a funded advance
**[measured]**.

**Bad phone numbers and emails: found, counted, refused.** Four categories rather than two,
because `placeholder` is the dangerous one: `5555555555` is a perfectly well-formed number that
identifies nobody. Out of 4,970 customers there are **6 malformed phone numbers and 1
placeholder email** **[measured]**, and those are 7 different people, so nobody here has both
problems. I refuse them as evidence of identity, fix nothing, and drop nothing. Refusing the
field only costs the matches that field would have made, and a broken value shouldn't be making
matches. That choice has a price and I count it too: when records merge, the survivor's details
are kept whole rather than assembled from the best parts of each, so one surviving customer
keeps a broken phone number that a merged-away duplicate had valid.

**Cards stay where they are, and here a number surprised me.** A card keeps pointing at the
customer it was added to and reaches the right person through the same mapping table, so undoing
a merge doesn't need to undo anything about cards.

What breaks if you get this wrong, as a number rather than a warning: in the source database
**no customer has two default cards** **[measured]**. After merging, **27 customers do, and
every one of those comes from two people being merged into one** **[measured]**. None of it was
pre-existing mess. Nothing can then say which card to charge. I report it and deliberately don't
fix it, because choosing between two cards a customer picked is a billing decision, and having
the pipeline quietly pick one would mean it deciding something about a customer's money that
nobody asked it to decide. Of everything here, that's the one I'd take to you rather than settle
myself.

**The result.** 4,984 customers in the warehouse, 4,970 considered after exclusions. The data
really contains 41 duplicate groups covering 91 records and 61 pairs; the resolver finds 35
groups. It proposed **47 pairs, 47 of them correct, 0 wrong, 14 missed, giving precision 1.000
and recall 0.770** **[measured on my own test data, which is the caveat below]**. Precision is
how often a proposed merge was right; recall is how many of the real duplicates it found.

**That precision figure is a consistency check, not a measurement.** It means the resolver
proposed nothing my test data disagrees with. It says nothing about how these rules would do on
your real customers. Only labelling a sample of your actual duplicates would tell you that, and
that's in the production section below. Recall is the more useful number and it's the lower one.
The 14 misses are broken down by cause from the run itself, with none unexplained: 4 have no
usable identifying fields, 2 have fields that contradict each other, and 8 are cases the
resolver deliberately refused **[measured]**.

---

## 3. Proving the data is right

Five checks that read *both* databases, one summary table, and three possible results rather
than two: pass; **source-dirty**, meaning the two sides agree and what they agree on is bad data
that really is in the source; and fail. Only fail exits non-zero.

The middle result is the design decision. A clean run here ends source-dirty, because the source
has 13 card rows, 17 transaction rows and 20 history rows whose customer was deleted, and all of
them are orphaned in *both* databases **[measured]**. With only pass and fail, every clean run
would fail on those, and the usual fix is to hardcode "expect 20 orphans", which goes stale the
first time the data moves. An earlier version of this code did that and printed a discrepancy
that wasn't real.

Completeness is reported as missing, extra, and matched, never as one combined number, because
one missing row and one extra row cancel out: the total matches and two rows are still wrong.
The checks compare cheap summaries first and only pull every row for a table whose summaries
already disagree. Each one also prints what it can't catch.

**Breaking it on purpose** is the one extra command. It edits two old amounts; the loader then
reads 0 rows, because editing a row doesn't change its ID, so you get a spotless load report
over a warehouse that's now wrong, and the checks fail naming the column and the two rows
**[measured]**. Then it swaps the two amounts instead, which leaves the count, total, minimum
and maximum all unchanged, so every check passes with the corruption still sitting there
**[measured]**. That limitation was written down in two files before anyone ran it, and running
it is what turned a claim into a demonstration.

---

## Cost

**What you're paying for now.** Copying the whole database nightly to find a 1% change bills you
four times over: the full read of the operational database, the cost of moving the data out, the
load, and storage.

**What I measured:** the busier of two simulated days moves **1.46%** of what a full copy moves,
about **68× less**; the quieter day is 0.45%. Running it again with nothing changed moves nothing
**[measured]**. That figure counts values transferred and excludes protocol and encryption
overhead, so it's a floor, and it's a fair comparison because both sides are counted the same
way.

**And here's the part that turns the obvious conclusion upside down.** "Loading into BigQuery is
free" is true, because batch loads aren't billed. But applying updates means a `MERGE`, and
**`MERGE` is billed as a query.** A `MERGE` that can't narrow itself down scans the *whole
destination table* every single day. So a free load can quietly swap itself for a billed full
scan of the destination, and that scan grows with your total history rather than with today's
changes. **Copying incrementally does not by itself make the bill incremental.** Two things fix
it. Write the `MERGE` so BigQuery can skip most of the table, which needs the destination split
by date so a query can ignore the old partitions. And for tables that are only ever inserted
into, skip `MERGE` altogether and just append, which is free. That's why the two big tables here
never go through the update path.

**What I can't tell you.** There's no BigQuery in this exercise, so any dollar figure would be
**[estimated]**, and I'd rather hand you the ratio I measured and the cost model to check.
Before promising a saving I'd want your daily volume loaded, whether you apply changes with
`MERGE` or by replacing the table, whether the destination tables are split by date, and how
much of today's bill is query versus storage.

---

## How this was built

I wrote the specs; AI agents did the typing. The architecture, the schema and its deliberate
flaws, the capture strategy, the identity rules and which of them outranks which, and all the
verification are mine. I treated generated code as unverified until it ran, which is where the
value was: one bulk insert created zero rows *while reporting success*, and a hash reused across
two identity fields meant a matching SSN mathematically forced a matching surname.

**The two worst problems were false claims rather than broken code.** The ID-marker code
asserted a safety property it didn't have. And `docker compose up`, the first line of the
instructions, had never once actually run: the container config repeated a command already
built into the image, so the program received the word "python" as its argument, printed its own
help, and exited **[measured]**. Every run in the project's history had used an override that
bypassed it, so nothing caught it.

The rule I took from that is to treat anything I advertise but never execute as broken until I
have run it. It's why the tests that no command invoked now run first, and why a cold-start bug
that appeared or didn't depending on how fast the database started now has a test of its own.

---

## Making this production

**Tools.** I'd keep Change Tracking rather than move to Change Data Capture, SQL Server's
heavier alternative: CDC gives you per-column history and costs you a capture job, a log reader
and a retention window to operate, and nothing downstream needs history; it needs current
state. For the two append-only tables I'd move the marker from the ID column to `rowversion`, a
column type SQL Server maintains itself, and which comes with the function that closes the
concurrency hole above. That's the single change I'd make before trusting this with several
writers. Scheduling can be whatever you already use. I'd use dbt, the standard SQL
transformation tool, for the work *after* the raw layer, but not for the extract, which does
things dbt isn't built for. The checks stay as plain code with exit codes.

**One-time versus permanent.** One-time: the initial backfill, the seed, switching on change
tracking, and labelling a sample of your real duplicates, which is the only thing that turns my
consistency check into an actual measurement. Permanent: the incremental load, the resolver
(rebuilt every run, never patched), the checks on a schedule with the exit code wired to
whatever pages someone, and the review queue, which needs a named owner.

**What I'd ship first: the checks, before the incremental load.** They're the smallest piece,
they don't depend on how you capture changes, and they answer the problem you described as *no
trust*. Point them at the pipeline you have today and they'll tell you whether the full copy
you're already paying for is actually complete. If it isn't, that's worth knowing before you
optimise the thing producing it. If it is, you now have a regression test to hold the migration
to. Shipping the incremental load first changes the pipeline and the thing that measures the
pipeline at the same time, and if the numbers move you won't know which change did it.
