"""069_ghl_contact_email_covering_index

Covering index for the ghl_contact side of the per-webinar statistics join.

The statistics queries join contacts to ghl_contact on email and then read four
small columns off the match. Before this index that join was a nested loop over
`ix_ghl_contact_lower_email` — 255 million index probes across a 127-day window,
each followed by a *random* heap fetch into a 2.7 GB heap. It was the single
largest source of disk reads on the instance (3.5 TB for the top query alone,
13% of all query time).

The heap fetch is almost pure waste: a ghl_contact row averages 932 bytes in
the heap and raw_custom_fields alone averages 1,069 bytes/row (1.5 GB of TOAST),
so every random fetch pulls an 8 KB page that is mostly JSONB the statistics
never read, to get four narrow columns. INCLUDE-ing those columns lets the join
run as an index-only scan and skip the heap entirely.

Indexed on plain `email`, not `lower(email)`: every email in the database is
already lowercase — verified exhaustively, not sampled (0 mixed-case across
contacts 4,515,253 / ghl_contact 3,006,079 / webinar_calendar_invites 3,583,344
/ webinargeek_subscribers 38,663) — and both ingest paths force `.lower()`
(api/routers/outreach/uploads.py, services/ghl_sync.py). Dropping LOWER() from
the join predicate is what lets the planner pick a hash join here at all.

The four INCLUDE columns are small (booked_call_webinar_series and the two
dates are fixed-width; calendar_webinar_series_history averages 14 bytes), so
the index stays far cheaper than the heap pages it avoids.

Applied to prod with CREATE INDEX CONCURRENTLY (see 068's operational note —
the Render start command runs `alembic upgrade head`, so a migration that can
block on locks crash-loops the service). CONCURRENTLY cannot run inside
alembic's transaction; upgrade() below is the non-concurrent equivalent for
fresh environments and is idempotent via IF NOT EXISTS.

Follow-up, deliberately NOT done here: once this index is confirmed in use,
ix_ghl_contact_lower_email (169 MB) and ix_ghl_contact_email (175 MB) become
redundant. Leaving them in place means a bad plan can fall back rather than
table-scan, and they can be dropped in a separate change after the plans are
verified in production.

Revision ID: 069
Revises: 068
"""
from alembic import op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_ghl_contact_email_stats"

CREATE_SQL = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON ghl_contact (email)
INCLUDE (booked_call_webinar_series,
         webinar_registration_in_form_date,
         cold_calendar_unsubscribe_date,
         calendar_webinar_series_history)
"""

# The statistics joins now compare email columns directly (g.email = c.email)
# instead of LOWER() on both sides. That makes "every stored email is already
# lowercase" load-bearing for correctness rather than merely true today: a
# single mixed-case row would silently drop matches and under-report metrics,
# with no error to notice.
#
# NOT VALID is deliberate — it skips the full-table scan (and the ACCESS
# EXCLUSIVE lock that comes with it) while still enforcing the rule on every
# INSERT and UPDATE from here on. Existing rows were verified exhaustively at
# the time of writing, so there is nothing to catch retroactively. To upgrade
# to a fully-validated constraint later, off-peak:
#     ALTER TABLE <t> VALIDATE CONSTRAINT ck_<t>_email_lowercase;
_LOWERCASE_EMAIL_TABLES = ["contacts", "ghl_contact", "webinar_calendar_invites",
                           "webinargeek_subscribers"]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(CREATE_SQL)

    # Postgres has no ADD CONSTRAINT IF NOT EXISTS, and this must be re-runnable:
    # prod took the index and part of these constraints out-of-band (lock
    # contention on the hot tables meant they landed one at a time), so a plain
    # ADD would raise "already exists" — and because the Render start command is
    # `alembic upgrade head && uvicorn ...`, that error would crash-loop the
    # service rather than just failing a migration.
    for table in _LOWERCASE_EMAIL_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ck_{table}_email_lowercase'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT ck_{table}_email_lowercase
                        CHECK (email = lower(email)) NOT VALID;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    for table in _LOWERCASE_EMAIL_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_email_lowercase")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
