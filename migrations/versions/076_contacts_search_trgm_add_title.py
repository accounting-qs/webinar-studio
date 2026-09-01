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
to the migration-075 field list when the widened index does not exist yet, so a
backend deployed ahead of the index degrades to "title isn't searchable yet"
rather than sequentially scanning 6.9 GB per keystroke.

THIS MIGRATION NEVER TOUCHES AN EXISTING INDEX. An earlier revision dropped and
recreated it inline; on 2026-09-01 an `alembic upgrade head` run against prod
reached that DROP and was stopped only by the 5s lock_timeout — had it acquired
the lock it would have destroyed the live 869 MB search index and then held a
SHARE lock on contacts for a multi-minute plain CREATE. So now:

  - index missing entirely (fresh/dev database) -> create the widened (title)
    version directly, plus pg_trgm;
  - index already present (prod, any expression version) -> no-op. The widen
    happens ONLY via the out-of-band build-then-swap below, off-peak; the API
    self-detects which expression is live, so the swap can happen any time
    after this revision is stamped.

Out-of-band widen (prod), when title search is wanted:

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
from sqlalchemy import text as sa_text

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


def _index_exists() -> bool:
    return bool(op.get_bind().execute(sa_text(
        "SELECT 1 FROM pg_indexes WHERE tablename = 'contacts' AND indexname = :n"
    ), {"n": INDEX_NAME}).scalar())


def upgrade() -> None:
    if _index_exists():
        # Prod path: leave the live index alone; widen out of band (docstring).
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON contacts USING gin (({EXPR_076}) gin_trgm_ops)"
    )


def downgrade() -> None:
    # Never drop a live index from inside the chain; the API works with either
    # expression version, so downgrading the version row is enough.
    pass
