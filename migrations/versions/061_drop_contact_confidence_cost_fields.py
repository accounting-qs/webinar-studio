"""061_drop_contact_confidence_cost_fields

Drop four unused enrichment columns from contacts: confidence, reasoning, cost,
status. These were only ever populated by the CSV import mapping and are read
nowhere in the app (stats, list assignment, segment quality, copy generation all
ignore them; contacts lifecycle uses outreach_status, and cost tracking lives in
api_cost_log). Removing them from the import path alongside this migration.

Dropping a column is metadata-only in PostgreSQL — fast, no table rewrite, safe
under the 120s statement_timeout.

Revision ID: 061
Revises: 060
"""
from alembic import op


revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE contacts "
        "DROP COLUMN IF EXISTS confidence, "
        "DROP COLUMN IF EXISTS reasoning, "
        "DROP COLUMN IF EXISTS cost, "
        "DROP COLUMN IF EXISTS status"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE contacts "
        "ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION, "
        "ADD COLUMN IF NOT EXISTS reasoning TEXT, "
        "ADD COLUMN IF NOT EXISTS cost DOUBLE PRECISION, "
        "ADD COLUMN IF NOT EXISTS status TEXT"
    )
