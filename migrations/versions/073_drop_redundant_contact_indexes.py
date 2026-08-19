"""073_drop_redundant_contact_indexes

Drop five redundant indexes on `contacts`. This is a scale fix, not a tidy-up.

`contacts` carried 16 indexes totalling ~3.3 GB against a 3.1 GB heap, on an
instance with 512 MB of shared_buffers. Two consequences, both of which showed
up as production incidents:

  1. Nothing stays cached. Working sets are evicted immediately, so scans pay
     random reads against a disk that sustains only ~250-350 of them per second.
     That IO ceiling is what pushed the assign claim past the 120s
     statement_timeout on 2026-08-19 (see migration 072).
  2. Every non-HOT UPDATE to a contact rewrites EVERY index on the table. The
     claim loop's UPDATE changes assigned_membership_count, assignment_id,
     assigned_date and outreach_status — all indexed, so HOT is impossible and
     the write cost is paid in full, ~3ms/row. Dropping 5 of 16 indexes cuts
     that per-row index-maintenance work by roughly a third, on the claim, on
     imports, and on blocklist re-stamping alike.

Each drop below is redundant by a structural argument, not by a usage guess —
idx_scan counts (5 months of stats, since 2026-03-30) are quoted only as
corroboration.

REDUNDANT BY THE BTREE PREFIX RULE — a query that can use an index on (a) can
always use an index on (a, b); only the leading column governs what is
searchable. Each of these is a strict single-column prefix of a surviving index,
and all three are full (non-partial) indexes whose supersets are also full:

  ix_contacts_bucket_id   (bucket_id)  112 MB, 76 scans
      ⊂ ix_contacts_outreach_status  (bucket_id, outreach_status)
  ix_contacts_upload_id   (upload_id)  134 MB, 99 scans
      ⊂ ix_contacts_upload_status_bucket (upload_id, outreach_status, bucket_id)
  ix_contacts_user_id     (user_id)     70 MB, 34 scans
      ⊂ uq_contacts_user_email       (user_id, email)

  Foreign keys stay indexed, which is the trap to check before dropping any of
  these: contacts has FKs on bucket_id, upload_id, user_id and assignment_id,
  and after this migration every one is still the LEADING column of a surviving
  index (the first three above, plus ix_contacts_assignment_id). So ON DELETE
  SET NULL / CASCADE maintenance keeps using an index instead of seq-scanning
  contacts per referenced row.

SUPERSEDED BY ix_contacts_claim_cover (migration 072) — same partial predicate
or broader, same leading keys, strictly larger payload:

  ix_contacts_claimable   (bucket_id, last_invited_at)
                          WHERE NOT is_blocklisted AND assigned_membership_count = 0
                          44 MB, 126 scans
      Identical predicate; claim_cover keys (user_id, bucket_id, last_invited_at).
      Safe ONLY because every consumer scopes user_id — verified: the assign
      claim (webinars.py, fixed in this same change) and the eligible counts
      (buckets.py, `Contact.user_id == LLOYD_USER_ID`). A consumer that filtered
      bucket_id WITHOUT user_id would fall off the leading column and scan the
      whole index, which is exactly the bug 072 was written to fix.

UNUSED — no code path constructs this predicate:

  ix_contacts_bucket_unassigned (bucket_id, assignment_id) 129 MB, 7 scans in
      5 months. `Contact.assignment_id` is only ever compared with == or IN
      (webinars.py, releases.py), never IS NULL, and never alongside bucket_id;
      those equality lookups are served by ix_contacts_assignment_id. The two
      `assignment_id IS NULL` sites in the codebase are on
      webinar_contact_memberships, a different table.

Total reclaimed: ~489 MB, and contacts drops from 16 indexes to 11.

DELIBERATELY KEPT:
  ix_contacts_good_avail (177 MB) — also a subset of claim_cover, but it backs
      the hot per-bucket eligible-counts aggregate and is 3.3x SMALLER than
      claim_cover (177 MB vs 582 MB), so an index-only scan over it touches far
      fewer pages. Dropping it would trade a rarely-run claim for a frequently-run
      count. Revisit only with a measured plan comparison.
  ix_contacts_bucket_filters (594 MB) — NOT superseded: its predicate is only
      `NOT is_blocklisted`, with no assigned_membership_count = 0, so it covers
      contacts that claim_cover omits entirely. Backs the unfiltered per-bucket
      totals.

Applied to prod with DROP INDEX CONCURRENTLY, out of band (see 068/069's
operational note — the Render start command runs `alembic upgrade head`, so a
migration that blocks on locks crash-loops the service). upgrade() below is the
non-concurrent equivalent for fresh environments and is idempotent via
IF EXISTS, so it is a no-op on prod once the concurrent drops land.

Revision ID: 073
Revises: 072
"""
from alembic import op

revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None

DROPPED = [
    "ix_contacts_bucket_id",
    "ix_contacts_upload_id",
    "ix_contacts_user_id",
    "ix_contacts_claimable",
    "ix_contacts_bucket_unassigned",
]

# Recreation SQL for downgrade(), matching the definitions these indexes had
# when they were dropped.
RECREATE = {
    "ix_contacts_bucket_id": "CREATE INDEX IF NOT EXISTS ix_contacts_bucket_id ON contacts (bucket_id)",
    "ix_contacts_upload_id": "CREATE INDEX IF NOT EXISTS ix_contacts_upload_id ON contacts (upload_id)",
    "ix_contacts_user_id": "CREATE INDEX IF NOT EXISTS ix_contacts_user_id ON contacts (user_id)",
    "ix_contacts_claimable": (
        "CREATE INDEX IF NOT EXISTS ix_contacts_claimable ON contacts (bucket_id, last_invited_at) "
        "WHERE NOT is_blocklisted AND assigned_membership_count = 0"
    ),
    "ix_contacts_bucket_unassigned": (
        "CREATE INDEX IF NOT EXISTS ix_contacts_bucket_unassigned ON contacts (bucket_id, assignment_id)"
    ),
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in DROPPED:
        op.execute(f"DROP INDEX IF EXISTS {name}")


def downgrade() -> None:
    for name in DROPPED:
        op.execute(RECREATE[name])
