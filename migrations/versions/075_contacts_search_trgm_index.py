"""075_contacts_search_trgm_index

GIN pg_trgm expression index powering the Contacts directory search
(GET /outreach/contacts?search=...).

The search is a substring match (ILIKE '%term%') across the identity fields a
user actually types — email, first/last name, company website, bucket and lead
list — concatenated into one expression. Without an index that is a full heap
scan of contacts (~5.6M rows, ~3.1 GB heap) per search: 10-30s+ of sequential
IO that evicts the working set from 512 MB of shared_buffers — the exact
cache-eviction failure mode migrations 072-074 exist to prevent. A trigram GIN
lookup instead touches only the posting lists for the query's trigrams plus the
matching heap rows, and the API layer additionally caps every search at 10,001
matches so a broad term ("gmail") can't fan out unbounded.

The query in api/routers/outreach/contacts.py must use this exact expression
(same coalesce/concatenation shape) or the planner will not match the index.

pg_trgm is already installed (migration 038). Trigram extraction lower-cases
internally, so ILIKE is served case-insensitively without lower() in the
expression.

Write cost: one more index to maintain on contacts (back up to 12). GIN
fastupdate batches insertions through the pending list, and none of the claim
path's UPDATE columns feed this expression — but non-HOT updates still touch
every index, so imports and the claim loop each pay a small additional per-row
cost. Accepted for making 5.6M contacts searchable at all.

Deploy note (prod): the in-migration CREATE below is for dev databases. On
prod, build it out of band and off-peak, then stamp — a plain CREATE INDEX
takes SHARE lock on contacts for the whole multi-minute build, blocking
imports and claims:

    SET statement_timeout = 0;
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_search_trgm
    ON contacts USING gin ((
        coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' ||
        coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' ||
        coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '')
    ) gin_trgm_ops);

Revision ID: 075
Revises: 074
"""
from alembic import op

revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_contacts_search_trgm"

# Keep in sync with SEARCH_EXPR in api/routers/outreach/contacts.py.
CREATE_SQL = f"""
CREATE INDEX IF NOT EXISTS {INDEX_NAME}
ON contacts USING gin ((
    coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' ||
    coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' ||
    coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '')
) gin_trgm_ops)
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(CREATE_SQL)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
