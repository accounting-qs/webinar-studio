"""072_contacts_claim_covering_index

Covering index for the contact-claim scan behind POST /outreach/webinars/{id}/assign.

The claim picks candidates out of a bucket under the operator's filters
(country / list_location / employee headcount) and the reuse gate
(assigned_membership_count, last_invited_at). Before this index the best match
was ix_contacts_good_avail — (user_id, bucket_id) INCLUDE (country,
list_location, employee_count) WHERE NOT is_blocklisted AND
assigned_membership_count = 0 AND last_invited_at IS NULL — and the planner
could only use it as a *plain* Index Scan, because the scan has to return
contacts.id and id is not in that index.

That distinction is the whole problem. A plain Index Scan cannot evaluate an
INCLUDE column: INCLUDE payloads are readable only by an Index Only Scan. So
every row had to be fetched from the 3.1 GB heap *before* the country filter
could reject it. On 2026-08-19 a 42-country Europe filter rejected ~3 of every
4 rows examined, so claiming 4,773 contacts touched ~16.7k random heap pages.
Prod's disk sustains only ~250-350 random reads/s, which put that single
statement at 62-102s measured idle — past the 120s statement_timeout once it
also competed with live traffic. The assign 500'd and rolled back.

Adding id to the payload is what makes the candidate scan index-only, so the
filters are evaluated against index tuples and rejected rows cost no heap read
at all. The claim then locks only the candidates it actually wants, by id
(webinars.py `_claim_pool`, phase 2) — turning "heap reads proportional to rows
EXAMINED" into "heap reads proportional to rows CLAIMED".

Shape notes:
  - last_invited_at is a KEY column, not INCLUDE: the reuse gate needs it as a
    range boundary (fresh-only is `IS NULL`, reuse is `< cutoff`), which only a
    key column can serve.
  - The predicate deliberately drops `last_invited_at IS NULL` from
    ix_contacts_good_avail's, so one index serves fresh-only, reuse-only and
    mixed-reuse claims instead of only the fresh pool.
  - user_id leads because every claim query scopes by it. Without that leading
    column Postgres can only apply bucket_id as a non-boundary qual and reads
    the whole index rather than the bucket's slice (measured 67s → 33s on the
    pre-claim count alone).

Applied to prod with CREATE INDEX CONCURRENTLY, out of band and off-peak (see
068/069's operational note — the Render start command runs `alembic upgrade
head`, so a migration that blocks on locks crash-loops the service). upgrade()
below is the non-concurrent equivalent for fresh environments and is idempotent
via IF NOT EXISTS, so it is a no-op on prod once the concurrent build lands.

Follow-up, deliberately NOT done here: ix_contacts_good_avail (177 MB) and
ix_contacts_claimable (44 MB) both become subsets of this index once the plans
are confirmed in production, and dropping them would also cut per-row write
amplification on the claim UPDATE (contacts currently carries ~9 indexes, at
~3ms per non-HOT row write). Leaving them means a bad plan can fall back rather
than table-scan; drop them in a separate change after the plans are verified.

Revision ID: 072
Revises: 071
"""
from alembic import op

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_contacts_claim_cover"

CREATE_SQL = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME}
ON contacts (user_id, bucket_id, last_invited_at)
INCLUDE (id, country, list_location, employee_count)
WHERE NOT is_blocklisted AND assigned_membership_count = 0
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
