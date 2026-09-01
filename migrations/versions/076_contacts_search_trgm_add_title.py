"""076_contacts_search_trgm_add_title

Widen ix_contacts_search_trgm to cover `title`, so the Contacts directory search
finds people by job title ("CEO", "head of engineering") alongside email, name,
company, bucket and lead list.

Why title and nothing else: at ~15 chars average it adds ~12% to the indexed
text (the index is 869 MB at migration 075's six fields), and it is the one
remaining HIGH-CARDINALITY field people actually type as a substring. The other
"data points" a user might filter on — country (9 chars), industry (28), sector,
seniority, employee range — are low-cardinality vocabularies where a substring
match is a poor interface and an expensive one: adding country+industry alone
would have grown the index past 1.2 GB while returning capped 10k-match blobs.
Those are exposed as structured filters on GET /outreach/contacts instead, each
one riding an existing index (see the module docstring in
api/routers/outreach/contacts.py).

The query in api/routers/outreach/contacts.py must use this exact expression
(same coalesce/concatenation shape) or the planner will not match the index.
That module reads the live index definition once at first search and falls back
to the migration-075 field list when this migration has not been applied, so a
backend deployed ahead of the index degrades to "title isn't searchable yet"
rather than sequentially scanning 6.9 GB per keystroke.

Deploy note (prod): the in-migration statements below are for dev databases. On
prod, build out of band and off-peak, then stamp — a plain CREATE INDEX takes a
SHARE lock on contacts for the whole multi-minute build, blocking imports and
claims. Build the new index under a temporary name first so the old one keeps
serving searches, then swap:

    SET statement_timeout = 0;
    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_search_trgm_v2
    ON contacts USING gin ((
        coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' ||
        coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' ||
        coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '') || ' ' ||
        coalesce(title, '')
    ) gin_trgm_ops);

    DROP INDEX CONCURRENTLY IF EXISTS ix_contacts_search_trgm;
    ALTER INDEX ix_contacts_search_trgm_v2 RENAME TO ix_contacts_search_trgm;

Peak disk during the build is both indexes at once (~1.9 GB). Restart the API
after the rename so the cached field list picks up `title`.

Revision ID: 076
Revises: 075
"""
from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_contacts_search_trgm"

# Keep in sync with SEARCH_FIELDS_076 in api/routers/outreach/contacts.py.
EXPR_076 = """
    coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' ||
    coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' ||
    coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '') || ' ' ||
    coalesce(title, '')
"""

# Migration 075's expression, restored on downgrade.
EXPR_075 = """
    coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' ||
    coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' ||
    coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '')
"""


def _create(expr: str) -> str:
    return f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON contacts USING gin (({expr}) gin_trgm_ops)"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(_create(EXPR_076))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(_create(EXPR_075))
