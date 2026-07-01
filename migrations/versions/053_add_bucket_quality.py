"""053_add_bucket_quality

Add a nullable quality label (good/medium/bad) to outreach_buckets. It is set
on the Statistics → Segments dashboard and surfaced read-only on the Planning
page's bucket picker (dropdown + Available-sources table). NULL = unmarked.

Revision ID: 053
Revises: 052
"""
from alembic import op


revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outreach_buckets ADD COLUMN IF NOT EXISTS quality VARCHAR(20)")
    # NULL passes the CHECK (NULL IN (...) is UNKNOWN, not FALSE), so unmarked
    # buckets are allowed. Drop-then-add keeps the migration re-runnable.
    op.execute("ALTER TABLE outreach_buckets DROP CONSTRAINT IF EXISTS ck_outreach_buckets_quality")
    op.execute(
        "ALTER TABLE outreach_buckets ADD CONSTRAINT ck_outreach_buckets_quality "
        "CHECK (quality IN ('good', 'medium', 'bad'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE outreach_buckets DROP CONSTRAINT IF EXISTS ck_outreach_buckets_quality")
    op.execute("ALTER TABLE outreach_buckets DROP COLUMN IF EXISTS quality")
