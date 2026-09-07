"""078_contacts_claim_cover_times_invited

Add `times_invited` to ix_contacts_claim_cover's INCLUDE payload, for the
Planning assign panel's "Invited less than X" cap.

The cap is `contacts.times_invited < X`, applied to the same two scans as every
other assign filter: the per-bucket eligible counts (GET /outreach/buckets/eligible)
and the claim's candidate scan (POST /outreach/webinars/{id}/assign, `_claim_pool`
phase 1). BOTH are Index Only Scans over ix_contacts_claim_cover — and that is
only true while EVERY column they filter on lives in the index.

`times_invited` did not. An INCLUDE payload is readable only by an Index Only
Scan, so a predicate on a column outside it forces the planner off that plan
entirely and onto one that reads the 3.1 GB heap. That is the exact failure mode
migration 072 was written to fix.

Measured with EXPLAIN (ANALYZE, BUFFERS) on prod 2026-09-04:

  Eligible counts — whole-table GROUP BY, 6-month reuse cutoff, no other filter:
    no cap  -> Parallel Index Only Scan    6.5s   354,720 buffers ( 81,205 read)
    + cap   -> Parallel Seq Scan          22.2s   403,497 buffers (375,072 read)

  Claim phase 1 — one 600-row chunk out of the 1.19M-contact "General" bucket,
  6-month cutoff + 3-country filter:
    no cap  -> Index Only Scan             7.2s    50,795 buffers ( 27,719 read)
    + cap   -> Parallel Bitmap Heap Scan  12.6s    43,831 buffers ( 43,331 read)

The control that isolates the cause: re-run the counts query with an always-true
predicate on `employee_count` — same shape, same 100% selectivity, the same
416,311x2 rows out and 2,328,234 filtered away — but on a column that IS in the
payload. It holds the Parallel Index Only Scan at 6.5s, identical to the uncapped
baseline. The payload decides, not the predicate.

Under prod's 120s statement cap that 3.4x / 1.75x is most of the safety margin:
the uncapped counts request was already seen hitting the cap through the app on a
cold cache the same day.

Four bytes per index tuple buys both scans back: the cap is evaluated against
index tuples, rejected rows cost no heap read, and the plans stay index-only.

VERIFIED on prod 2026-09-07 after this index was built and `contacts` was vacuumed
back to 100% all-visible. Capped and uncapped now run the SAME plan, touch the same
buffers to within 0.03%, and take ZERO heap fetches:

  counts, 6mo cutoff:  no cap 84,331 buffers / 0 fetches | + cap 84,356 / 0
  claim chunk of 600:  no cap 15,306 buffers / 0 fetches | + cap 15,306 / 0

The cap is free. Note the visibility map is the other half of this and matters more:
restoring it 72.6% -> 100% took the claim chunk from 7.2s to 0.34s (56,331 heap
fetches -> 0), on every assign, capped or not. The index keeps the plan index-only
when a cap is present; vacuum is what makes an index-only plan cheap.

Shape notes:
  - INCLUDE, not a key column: the cap is never a scan boundary — the leading
    keys (user_id, bucket_id) and the reuse range (last_invited_at) already
    position the scan, and times_invited is only ever a filter on the rows it
    returns.
  - No new index. Widening the existing payload keeps `contacts` at 12 indexes;
    an additional one would add per-row write amplification to every claim,
    import and blocklist re-stamp (migration 073 exists precisely because that
    cost got out of hand).
  - Write amplification is unchanged in practice: rows whose times_invited moves
    are rows being marked used, which also bump assigned_membership_count and so
    cross this index's partial predicate anyway.

DEPLOY NOTE — build this CONCURRENTLY on prod, out of band and off-peak, before
the app code that sends `max_invited` goes live:

    SET lock_timeout = 0;       -- see below: a short one BREAKS a concurrent build
    SET statement_timeout = 0;
    CREATE INDEX CONCURRENTLY ix_contacts_claim_cover_v2
      ON contacts (user_id, bucket_id, last_invited_at)
      INCLUDE (id, country, list_location, employee_count, times_invited)
      WHERE NOT is_blocklisted AND assigned_membership_count = 0;
    DROP INDEX CONCURRENTLY ix_contacts_claim_cover;
    ALTER INDEX ix_contacts_claim_cover_v2 RENAME TO ix_contacts_claim_cover;

`lock_timeout = 0` is not optional and is the opposite of the `SET LOCAL lock_timeout =
'5s'` used in upgrade() below. CREATE INDEX CONCURRENTLY finishes by waiting out every
already-open transaction, and that wait is a lock on each one's virtual xid — so
lock_timeout cancels it and leaves an INVALID index behind. On the first prod attempt
(2026-09-07) a 30s lock_timeout killed the build at 116s behind an unrelated 3.5-minute
query, leaving a 622 MB invalid _v2 to drop concurrently before retrying. Waiting costs
the app nothing: CIC holds only ShareUpdateExclusiveLock (blocks DDL/VACUUM, not
reads/writes). After any failure, check pg_index.indisvalid and drop the leftover.

(Postgres cannot add an INCLUDE column in place, so it is build-new/drop-old.
Budget ~700 MB of extra disk for the overlap; the old index keeps serving reads
throughout.) upgrade() below is the non-concurrent equivalent for fresh
environments — see 068/069's operational note: the Render start command runs
`alembic upgrade head`, so a migration that blocks on locks crash-loops the
service. It is idempotent, so it is a no-op on prod once the concurrent build
above has landed and been renamed into place.

Revision ID: 078
Revises: 077
"""
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_contacts_claim_cover"

CREATE_SQL = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME}
ON contacts (user_id, bucket_id, last_invited_at)
INCLUDE (id, country, list_location, employee_count, times_invited)
WHERE NOT is_blocklisted AND assigned_membership_count = 0
"""

# The 072 definition, for downgrade().
CREATE_SQL_V072 = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME}
ON contacts (user_id, bucket_id, last_invited_at)
INCLUDE (id, country, list_location, employee_count)
WHERE NOT is_blocklisted AND assigned_membership_count = 0
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # A pre-existing index under this name is the 072 payload (no times_invited)
    # unless the concurrent build above already replaced it — checking the
    # definition is what makes this a no-op in the latter case instead of a
    # silent skip in the former.
    current = op.get_bind().exec_driver_sql(
        "SELECT indexdef FROM pg_indexes "
        f"WHERE tablename = 'contacts' AND indexname = '{INDEX_NAME}'"
    ).scalar()
    if current is not None and "times_invited" in current:
        return
    if current is not None:
        op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(CREATE_SQL_V072)
