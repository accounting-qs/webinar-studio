# Deploy: "Invited less than X" filter (Planning → assign lists)

Adds a per-contact invite cap to the Planning assign panel: alongside the reuse cutoff,
the operator can restrict the claim to contacts invited **fewer than** X times
(1x / 2x / 3x / 5x / 10x / custom). The predicate is `contacts.times_invited < X` —
`times_invited` is the denormalized count of `'used'` memberships already maintained by
`recompute_contact_caches`, so there is **no new column and no backfill**.

The cap is exclusive: "3x" means at most 2 prior invites. It is applied identically to
the per-bucket eligible counts (`GET /outreach/buckets/eligible?max_invited=`) and to the
claim (`POST /outreach/webinars/{id}/assign`, `max_invited` in the body), so the panel's
"remaining" ties out to what an assign actually grabs — and it lands in the created
list's description as e.g. `· <3x invited`.

**Inert without a reuse cutoff**, by design: the fresh-only pool is
`last_invited_at IS NULL`, which is exactly `times_invited = 0`, so no cap ≥ 1 can change
it. `invite_count_filter()` returns `[]` when `cutoff_ts is None`, which keeps fresh-only
counts on their rollup fast path, and the UI hides the control for "Fresh only" — the same
rule the "Previously invited only" checkbox already follows.

## Required pre-deploy step

**Build migration 078's index CONCURRENTLY, out of band, before the app code goes live.**

```sql
CREATE INDEX CONCURRENTLY ix_contacts_claim_cover_v2
  ON contacts (user_id, bucket_id, last_invited_at)
  INCLUDE (id, country, list_location, employee_count, times_invited)
  WHERE NOT is_blocklisted AND assigned_membership_count = 0;

DROP INDEX CONCURRENTLY ix_contacts_claim_cover;
ALTER INDEX ix_contacts_claim_cover_v2 RENAME TO ix_contacts_claim_cover;
```

Run it with **`SET lock_timeout = 0; SET statement_timeout = 0;`** first. Both matter, and
the lock one is the trap: CREATE INDEX CONCURRENTLY finishes by waiting out every
transaction that was already open, and that wait is implemented as a lock on each
transaction's virtual xid — so `lock_timeout` cancels it and leaves an INVALID index
behind. That is exactly what happened on the first prod attempt (2026-09-07): a 30s
lock_timeout killed the build at 116s behind an unrelated 3.5-minute app query on
`webinar_calendar_invites`, leaving a 622 MB invalid `_v2` that had to be dropped
concurrently before retrying. Waiting is safe for the app — CIC holds only
ShareUpdateExclusiveLock, which blocks DDL and VACUUM, not reads or writes. Note this is
the opposite of the `SET LOCAL lock_timeout = '5s'` the alembic migrations use: there a
short timeout protects against blocking the table, here it is what breaks the build.

If a build does fail, check `pg_index.indisvalid` and drop any invalid leftover
(`DROP INDEX CONCURRENTLY ix_contacts_claim_cover_v2`) before retrying — an invalid index
is skipped by the planner but still maintained on every write.

Postgres cannot add an INCLUDE column in place, so this is build-new / drop-old. Budget
~700 MB of extra disk for the overlap; the old index keeps serving reads throughout.
`migrations/versions/078_*.py` is the idempotent non-concurrent equivalent for fresh
environments and becomes a no-op on prod once the rename lands — it must not be the thing
that builds this on prod, because the Render start command runs `alembic upgrade head`
and a migration that blocks on locks crash-loops the service (same operational note as
068/069/072).

### Why it is required, not optional

Both scans the cap touches — the panel's per-bucket counts and the claim's phase-1
candidate scan — are **Index Only Scans** over `ix_contacts_claim_cover`, and that holds
only while every column they filter on is in the index. An INCLUDE payload is readable
*only* by an Index Only Scan, so a predicate on a column outside it pushes the planner off
that plan and onto one that reads the 3.1 GB heap. Measured with EXPLAIN (ANALYZE, BUFFERS)
on prod 2026-09-04:

| query | plan | time | buffers (read from disk) |
| --- | --- | --- | --- |
| counts, 6mo cutoff, no cap | Parallel Index **Only** Scan | 6.5s | 354,720 (81,205) |
| counts, 6mo cutoff, `+ times_invited < 2` | Parallel Seq Scan | **22.2s** | 403,497 (375,072) |
| counts, same shape, always-true `employee_count` predicate (control — in the payload) | Parallel Index **Only** Scan | 6.5s | 354,716 (81,267) |
| claim chunk of 600, "General" bucket + 3 countries, no cap | Index **Only** Scan | 7.2s | 50,795 (27,719) |
| claim chunk of 600, same, `+ times_invited < 2` | Parallel Bitmap Heap Scan | **12.6s** | 43,831 (43,331) |

The control row is the one that settles it: same query shape, same 100% selectivity, the
same 416,311×2 rows out and 2,328,234 filtered away — but on a column already in the
payload, and the plan and the 6.5s both hold. **The payload decides, not the predicate.**

3.4x on the counts and 1.75x on the claim is most of the headroom under prod's 120s
statement cap — the *uncapped* counts request was already observed hitting that cap
through the app on a cold cache the same day (see the visibility-map drift note: the
"index only" scan still takes 364,502 heap fetches). This is the same failure mode
migration 072 exists to fix.

## Verified on prod, 2026-09-07

The index was built concurrently and swapped in, and `contacts` was vacuumed back to
100% all-visible. Re-measured with EXPLAIN (ANALYZE, BUFFERS), capped variants run FIRST
so they got no warm-cache advantage:

| query | plan | heap fetches | buffers | time |
| --- | --- | --- | --- | --- |
| counts, 6mo cutoff, no cap | Parallel Index **Only** Scan | **0** | 84,331 | 13.0s |
| counts, 6mo cutoff, **+ cap 2x** | Parallel Index **Only** Scan | **0** | 84,356 | 21.5s |
| claim chunk of 600, no cap | Index **Only** Scan | **0** | 15,306 | 0.34s |
| claim chunk of 600, **+ cap 2x** | Index **Only** Scan | **0** | 15,306 | 2.5s |

**The cap is now free.** Capped and uncapped touch the same buffers to within 0.03% and
take zero heap fetches. The wall-time gaps are cache, not the predicate — re-running the
counts pair in the opposite order flips which one is slower (18.1s uncapped vs 13.3s
capped), and the claim's 0.34s vs 2.5s is one run hitting cache and the other reading
15,056 pages from disk.

Two things worth carrying forward:

- **The vacuum mattered more than the index.** Restoring the visibility map from 72.6% to
  100% took the claim chunk from 7.2s to 0.34s — heap fetches went 56,331 → 0. That is a
  20x win on *every* assign, capped or not. The index is what keeps the plan index-only
  once a cap is present; the VM is what makes an index-only plan actually cheap. Both are
  needed and they fix different halves.
- **`INDEX_CLEANUP OFF` cannot finish this job.** It moved the VM 72.6% → 81.5% in 48s and
  then stopped, reporting `index scan bypassed: 74556 pages (18.48%) have 101758 dead item
  identifiers` — a page holding dead item identifiers cannot be marked all-visible until
  the indexes are cleaned. The full pass (heap + 12 indexes, ~4 GB) is what reached 100%.
  At 81.5% the counts query still refused an index-only plan, correctly: forcing it with
  `enable_seqscan = off` ran past 10 minutes.

Add a `VACUUM (ANALYZE) contacts` to the deploy, after the index swap.

## No other steps

No migration beyond the index, no backfill, no recompute, no config. Rolling back is the
`downgrade()` in 078 (restores the 072 payload); the app code tolerates either index —
only the plan changes.
