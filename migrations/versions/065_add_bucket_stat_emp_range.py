"""065_add_bucket_stat_emp_range

Add a user-set employee-count range (stat_emp_min / stat_emp_max, literal
headcount) to outreach_buckets. Defined on the Statistics → Segments dashboard;
drives the per-segment drill-down and the data-driven "suggested range". NULL =
undefined; either bound may be NULL for an open range. Distinct from the existing
`emp_range` free-text column (the bucket's source range, load-bearing for copy
generation). Additive + metadata-only (nullable INT adds are instant in PG11+),
so it is forward-compatible with the code currently on prod.

Revision ID: 065
Revises: 064
"""
from alembic import op


revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE outreach_buckets ADD COLUMN IF NOT EXISTS stat_emp_min INTEGER")
    op.execute("ALTER TABLE outreach_buckets ADD COLUMN IF NOT EXISTS stat_emp_max INTEGER")
    # NULL bounds pass the CHECK (comparisons with NULL are UNKNOWN, not FALSE), so
    # open/undefined ranges are allowed. Drop-then-add keeps the migration re-runnable.
    op.execute("ALTER TABLE outreach_buckets DROP CONSTRAINT IF EXISTS ck_outreach_buckets_stat_emp_range")
    op.execute(
        "ALTER TABLE outreach_buckets ADD CONSTRAINT ck_outreach_buckets_stat_emp_range "
        "CHECK (stat_emp_min IS NULL OR stat_emp_max IS NULL OR stat_emp_min <= stat_emp_max)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE outreach_buckets DROP CONSTRAINT IF EXISTS ck_outreach_buckets_stat_emp_range")
    op.execute("ALTER TABLE outreach_buckets DROP COLUMN IF EXISTS stat_emp_max")
    op.execute("ALTER TABLE outreach_buckets DROP COLUMN IF EXISTS stat_emp_min")
