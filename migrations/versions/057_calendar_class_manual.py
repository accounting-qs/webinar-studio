"""057_calendar_class_manual

User-editable calendar classification.

- ghl_calendar.class_is_manual BOOLEAN — set when the user overrides
  calendar_class from the Calendars tab. Sync refreshes calendar_class from
  the name-based auto-classification only while this is FALSE; a manual
  class is never clobbered.

Revision ID: 057
Revises: 056
"""
from alembic import op


revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ghl_calendar ADD COLUMN IF NOT EXISTS "
        "class_is_manual BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ghl_calendar DROP COLUMN IF EXISTS class_is_manual")
