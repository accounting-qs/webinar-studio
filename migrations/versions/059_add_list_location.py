"""059_add_list_location

List-level location captured at CSV import (a country or a region like Europe/USA).
Stored on every contact of the import as a deliberate, high-confidence signal that
is kept separate from the per-contact `country` (which comes from the CSV and is
often blank). The assign filter uses it as a second-level match when `country` is
empty. Also stored on upload_history so an orphaned import can resume with it.

- contacts.list_location       TEXT
- upload_history.list_location TEXT

Revision ID: 059
Revises: 058
"""
from alembic import op


revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE contacts ADD COLUMN IF NOT EXISTS list_location TEXT")
    op.execute("ALTER TABLE upload_history ADD COLUMN IF NOT EXISTS list_location TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE upload_history DROP COLUMN IF EXISTS list_location")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS list_location")
