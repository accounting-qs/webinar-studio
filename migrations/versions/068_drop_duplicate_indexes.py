"""068_drop_duplicate_indexes

Reclaims ~790 MB of duplicate and unused index on the four largest tables.

Prod (SMALL: 2 GB RAM) runs a 13.5 GB database through a 512 MB shared_buffers,
so the heap cache hit rate sits at ~61% — nearly every heap read goes to disk,
which is what pins Disk IO at ~99% during sync bursts. Index that is never read
still costs on every write and still competes for that cache, so dropping it is
a direct win on a cache-starved instance.

Measured on prod before writing this (pg_index grouping + pg_stat_user_indexes):

  ix_contacts_email                   379 MB  378,242 scans  exact duplicate of
                                                             uq_contacts_user_email
                                                             — both btree(user_id, email)
  ix_wci_webinar_email                290 MB        0 scans  exact duplicate of
                                                             uq_wci_webinar_email
  ix_ghl_contact_series_history_trgm   69 MB        0 scans  unused
  ix_wcm_bucket                        25 MB        0 scans  unused
  ix_wcm_user                          25 MB        4 scans  effectively unused

The two duplicates are safe to drop despite ix_contacts_email's scan count: the
surviving unique index has identical columns in identical order (verified via
pg_indexes), so the planner simply switches to it. uq_contacts_user_email
already serves 4,990,209 scans against the same table.

Plain DROP INDEX rather than DROP INDEX CONCURRENTLY: alembic runs migrations
inside a transaction (see migrations/env.py) and CONCURRENTLY cannot run in one.
A non-concurrent drop is near-instantaneous once it holds the lock — the only
real risk is queueing behind a long-running statistics query, so each drop is
guarded by a short lock_timeout. If that fires, the migration aborts cleanly
with nothing dropped; re-run it when the instance is quiet.

Revision ID: 068
Revises: 067
"""
from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


# (index, table) — table only matters for the downgrade rebuild.
_DUPLICATE_INDEXES = [
    ("ix_contacts_email", "contacts", "(user_id, email)"),
    ("ix_wci_webinar_email", "webinar_calendar_invites", "(webinar_id, email)"),
    ("ix_wcm_bucket", "webinar_contact_memberships", "(bucket_id)"),
    ("ix_wcm_user", "webinar_contact_memberships", "(user_id)"),
]


def upgrade() -> None:
    # Never wait more than 5s for the ACCESS EXCLUSIVE lock. Better to abort and
    # retry off-peak than to stall every reader behind a 29s statistics query.
    op.execute("SET LOCAL lock_timeout = '5s'")

    for index_name, _table, _cols in _DUPLICATE_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    # Trigram index over ghl_contact.calendar_webinar_series_history (added in
    # 038). The `~*` probe it was built for is still issued, but its dominant
    # consumer evaluates the regex inside a FILTER over an already-joined row
    # set (ghl_statistics_source.py:1667) — a shape no index can serve. Hence 0
    # scans across a 127-day window, while its three siblings on this table are
    # actively used (3,640 / 2,139 / 319 scans), so this is not a stats artifact.
    op.execute("DROP INDEX IF EXISTS ix_ghl_contact_series_history_trgm")


def downgrade() -> None:
    for index_name, table, cols in _DUPLICATE_INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} {cols}")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ghl_contact_series_history_trgm "
        "ON ghl_contact USING gin (calendar_webinar_series_history gin_trgm_ops)"
    )
