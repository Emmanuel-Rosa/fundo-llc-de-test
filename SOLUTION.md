# SOLUTION

All three problems attempted. Numbers are tagged **[M]** measured from run output or **[E]**
estimated. `README.md` holds the full tables and the command-by-command output; this is the
reasoning.

## What I took on, and what I did not

**Skipped deliberately, because a confident guess costs more than an honest gap:**

- **Duplicate *prevention* at the source.** The map is re-derived every run, so a duplicate
  created tomorrow is merged tomorrow — that is detection. Prevention is a uniqueness control
  on the proof key in the operational database, and it needs a writable source plus a product
  decision about what happens at signup when it fires.
- **Repairing malformed contacts** — found, counted and refused as evidence instead.
- **Corroborating `funded` status.** `money_moved()` reads `status` only. Checking it against
  `funded_at` and `repayment_account_hash` turns a veto into a judgement and every group it
  moved would need re-scoring. The seam is named in the code, not crossed.
- **Float reconciliation.** Two `FLOAT` columns are omitted from value parity **by type** —
  stated, rather than reconciled with a tolerance and called a pass.

---

## 1. Move only what changed

**Two strategies, because one table dominates.** `transactions` is 86.8% of everything
replicated and about 1.1% of the daily change **[M]** — that asymmetry is the whole argument for
choosing per table rather than uniformly.

Change Tracking for `customers`, `advances` and `cards`: rows mutate and get hard-deleted, and a
change feed reports deletes where a watermark cannot. A bounded high-water mark on the clustered
IDENTITY key for `transactions` and `customer_history`: append-only, so Change Tracking would
buy per-row bookkeeping for rows that never update. `TRACK_COLUMNS_UPDATED = OFF` — current
state, not an audit trail.

**The watermark is the key, not a timestamp, and that is measured.** `posted_at` exists, is
indexed, and is the obvious choice. It is also wrong: 150 of each day's 2,800 inserts are
backdated, so a `posted_at` watermark silently loses **745 of 50,000** rows **[M]**.

**Reliability.** Run 1 is a snapshot read, not a change-feed replay — capture is enabled *after*
the seed, and `CHANGETABLE` from version 0 correctly returns nothing. The change-feed join is a
`LEFT JOIN`: a deleted row is in `CHANGETABLE` and absent from the base table, so an inner join
drops every delete while row counts still reconcile. Run 4 replays with zero churn and reads
**0 rows, 0 bytes** **[M]**. And rows *and* watermarks commit in **one** transaction across
all five tables, so a crash mid-run rolls back to a coherent point instead of leaving
`customers` at version 51 and `advances` at 47 — a state no check can see and every mart is
wrong about.

**I changed my mind about the high-water mark, and this is the part I would want read.** Its
docstring claimed the closed window made it safe. Two connections say otherwise: A inserts id 3
and holds the transaction open, B inserts id 4 and commits, a snapshot then sees `[1, 2, 4]` and
takes a ceiling of **4** — so row 3, once committed, sits permanently below the watermark
carrying 50,000 cents **[M]**. An IDENTITY value is assigned at INSERT and made visible at
COMMIT, and there is no `MIN_ACTIVE_IDENTITY`, so no choice of ceiling fixes it: the bound is on
the wrong side of the hazard, and the lost row is a gap *inside* the window. Change Tracking is
immune — its versions are assigned at commit. What ships is the bound **plus detection**: a
contiguity check counting `COUNT(*)` against `MAX(id) - MIN(id) + 1`, reported as a heuristic
because a rolled-back insert burns an id permanently. The claim had spread to five sites and is
retracted in all of them; the probe is in `probes/`, so the retraction is reproducible.

---

## 2. Resolve duplicate customers

**The merge is indirection, never mutation.** `meta.customer_map` maps each customer to a
canonical id; no source row is rewritten or deleted, so an unmerge is a one-row edit. That
matters because every rule below is a judgement that can be wrong.

**Proves:** `customer_id`, the tuple `(ssn_last4, date_of_birth, last_name)`, a prior human
`manual_merge`. **Suggests:** email, phone, postal address, a shared card fingerprint.

Email is the trap, priced rather than argued: promoting it to *proves* introduces 2 wrong
pairs and recovers 2 true ones — precision 1.000 → 0.961 **[M]**. A household shares a
mailbox, so email identifies a *mailbox*, not a person. First name is excluded (nicknames are
not evidence); surname is included, and dropping it costs 1 wrong pair for 1 recovered
**[M]** — the marriage-rename case bought by paying with two strangers who share `ssn_last4`
and `date_of_birth`. Placeholders are caught by **shape**, never a blocklist, because an
unrecognised placeholder is treated as strong evidence and merges strangers.

**Funded or paid-off is untouchable.** A money-moved customer is never auto-excluded, and a
group with **two** money-moved members is not merged at all — I do not guess. The merge is
**withdrawn** and a review row filed; across churn, rows merged away go 42 → 39 and review rows
4 → 5 **[M]**. The review row carries each member's repayment-account hash *whole*, because the
reviewer's real question is whether they repay from the same account: two funded members with
overlapping windows on *different* accounts is a first-party fraud signal for a cash-advance
lender, not a data-quality ticket.

**Test data is excluded, not merged — and the naive pattern really does catch real people.**
Three rules ship: internal domain *with* an anchored automation local part; a `+test`/`+qa`
subaddress tag; canonical artifact values. Result: 14 excluded, 2 more money-vetoed **[M]**. Run
as a counterfactual, the naive `'%test%'` filter flags 14 customers of which **7 are real
people** — including a funded Marcus **Testerman** — precision 0.500 **[M]**. Staff at the
company domain survive because rule A needs the domain *and* an anchored local part.

**Malformed contacts: found, counted, refused.** Four states, not two, and `placeholder` is the
dangerous one — `5555555555` passes every shape check and identifies nobody. Over a population
of 4,970: **6 malformed phones and 1 placeholder email** **[M]** — exactly the 7 canonical
customers reported as carrying a bad contact. The decision is to **refuse them as identity
evidence, repair nothing, drop nothing**: repairing a phone invents a way to contact somebody,
dropping the row loses a real customer, and refusing the field costs only the matches that
field would have made — precisely the matches that should not be made on a broken value. The
cost is counted too: the survivor's row is taken whole, so one canonical customer keeps a
malformed phone a merged-away duplicate had valid. Stitching a "best" record from several rows
produces a customer who never existed.

**Cards end up where they already were, and the number surprised me.** A card keeps its
`customer_id` and reaches its person *through* the map — the same indirection as the merge — so
an unmerge needs no card restore. In the source, **0** customers hold more than one default card
**[M]**. After resolution, **27** canonical customers do, and **all 27 span more than one
original customer** **[M]**. Every conflict is created by the merge; none is pre-existing dirt.
Two people who each chose their own default card become one person with two, and nothing can say
which card a charge should hit. It is reported split by cause and deliberately **not** repaired:
choosing between two customer-chosen default cards is the billing owner's decision, and silently
picking one would be the pipeline making a financial call nobody asked it to make. Of everything
here, this is what I would put in front of the business first.

**Result.** 4,984 mirror customers → **4,970** reaching resolution. Truth holds 41 duplicate
groups over 91 rows containing 61 pairs; the resolver forms 35 groups. Shipped: **47 proposed,
47 correct, 0 wrong, 14 missed → precision 1.000, recall 0.770** **[M]**.

**The precision figure is a self-consistency check, not a measurement.** It says the resolver
proposed nothing the seeded truth disagrees with. It cannot say these rules hold on your real
book — only a labelled sample of your real duplicates could. Recall is the more useful number,
and it is the lower one. The 14 misses are partitioned by cause from the run with nothing
unexplained: 4 have no proof tuple, 2 have tuples that disagree, 8 are refusals the resolver
*chose* **[M]**.

---

## 3. Prove the data is correct

Five checks, both sides read, one table — and **three verdicts** rather than two. `PASS`;
`SOURCE-DIRTY`, where the two sides agree and what they agree on is bad data the source really
holds; and `FAIL`. Only `FAIL` exits non-zero.

The middle verdict is the design decision. A clean run ends SOURCE-DIRTY: the source orphans 13
card, 17 transaction and 20 history rows whose parents were hard-deleted, **on both sides**
**[M]**. Two-valued, every clean run fails on those, which forces a hardcoded expected-orphan
constant — and a stale constant is how an earlier version of the scoring code came to print a
fabricated discrepancy against a correct run.

Completeness is reported as **missing, extra and matched — never one netted number**, because one
lost row plus one stale row net to a count that matches exactly while two rows are wrong.
Aggregates first, full key sets only where the scalars disagree. Every check also prints what
it *cannot* see — a check that hides its limits gets trusted past them.

**Breaking it on purpose** is the one extra command. Two historical amounts are rewritten in
place; the loader then reads **0 rows**, because no id moved — a spotless load report over a
wrong warehouse — and the scorecard FAILs, naming the column, the 37,655-cent shortfall and the
two rows **[M]**. Then the sharper half: the same two rows *swap* amounts, so COUNT, SUM, MIN and
MAX are identical by construction and every aggregate check passes with the corruption still
present **[M]**. That blind spot was documented in two modules before it was ever executed, and a
documented limitation nobody ran is a claim rather than a finding. Both restores write back
values that were read, not hardcoded, and verify by re-reading.

---

## Cost impact

**Today**, a full daily copy to detect ~1% change pays four times: a full-table read against the
operational server every night (contention before it is a dollar), egress, the load, and
storage.

**Measured:** a day's delta moves **1.46%** of a full copy's bytes — a **68×** reduction — and
a zero-churn replay moves nothing **[M]**. Payload is values transferred, excluding TDS framing
and TLS: a floor, and the right basis for a ratio because both sides are measured the same way.

**The part that inverts the naive conclusion.** "Loading into BigQuery is free" is true — batch
loads are not billed. But an upsert is a `MERGE`, and **`MERGE` is billed as a query**. An
unpruned `MERGE` scans the *entire target table* daily, so a pipeline congratulating itself on a
free load has swapped it for a billed full scan of the destination that grows with total history
rather than with today's change. **Extracting incrementally does not by itself make the bill
incremental.** The fix is a static partition filter in the `MERGE` predicate so BigQuery can
prune, plus skipping `MERGE` entirely for append-only tables and appending, which is free — which
is why `transactions` and `customer_history` never touch the upsert path here.

**What I cannot tell you:** there is no BigQuery in this exercise, so any dollar figure would be
**[E]**. Before promising a saving I would want your daily bytes loaded, whether the apply is
`MERGE` or truncate-and-load, whether the targets are partitioned, and the query/storage split.

---

## How this was built

Built with agents executing against written contracts. The architecture, the schema and each
deliberate defect in it, the capture strategy, the identity rules and their precedences, and the
verification are mine. Generated code was treated as unverified until it ran, and that is where
the value was: card-id arithmetic that was unsatisfiable, so a bulk insert produced zero rows
*while reporting success*. Measurement caught what reading did not — one salted hash reused
across two identity fields, so a matching SSN mathematically forced a matching surname.

**The two worst defects were false claims, not broken code.** The high-water docstring asserted
an immunity it did not have. And `docker compose up` had never once run the demo: the service
`command` duplicated the image's `ENTRYPOINT`, so `argv[1]` was `"python"` — it printed help
and exited 2 **[M]**. Every prior run had used an entrypoint override, so nothing caught it.
**Anything advertised and never executed is presumed broken** — which is also why the 465 tests
no command invoked now run first, before the source build.

Generation is the cheap step. Specifying and verifying is the work.

---

## How this becomes production

**Tools.** Keep Change Tracking over CDC: CDC buys per-column history at the cost of a capture
job, log reader and retention window, and nothing downstream needs an audit trail — it needs
current state. For the append-only tables, move the watermark from IDENTITY to **`rowversion`
with `MIN_ACTIVE_ROWVERSION()`**, the one mechanism that closes the concurrency hole above; that
is the single change I would make before trusting this under real write concurrency. dbt for
transformations *after* the raw layer, not for the extract. The checks stay plain code with exit
codes.

**One-time:** the backfill, the seed, enabling capture, and a labelling exercise on real
duplicates — the only thing that can turn that precision self-consistency check into a
measurement. **Permanent:** the incremental load, the resolver (re-derived every run, never
patched forward), the scorecard on a schedule with its exit code wired to alerting, and the
review queue — which needs an owner, because a queue nobody reads is worse than no queue.

**First, I would ship the scorecard — before the incremental load.** It is the smallest piece, it
is independent of the capture strategy, and it answers the pain you called *no trust*. Run it
against the pipeline you have today and it tells you whether the full copy you are already
paying for is actually complete. If it is not, that is worth knowing before optimising the thing
that produces it; if it is, you have a regression test to hold the migration to. Shipping the
incremental load first changes the pipeline and the trust mechanism at once, and if the numbers
move you will not know which one did it.
