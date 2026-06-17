"""050_add_calendar_upload_column_map

Persist a user-edited CSV column mapping (target field -> CSV header name) on
the upload row so a mis-named header can be mapped manually in the modal. Stored
on webinar_calendar_uploads so the background import (and orphan-resume after a
restart) use the same mapping instead of falling back to header auto-detection.

Revision ID: 050
Revises: 049
"""
from alembic import op


revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE webinar_calendar_uploads ADD COLUMN IF NOT EXISTS column_map JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE webinar_calendar_uploads DROP COLUMN IF EXISTS column_map")
