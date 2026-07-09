"""055_calendar_source

Curated calendar->source mapping for the Bookings drill-down.

- ghl_calendar.source_label      TEXT — curated source label for a booking
                                  calendar. Seeded from funnel_tag on sync;
                                  user edits are preserved (never clobbered).
- ghl_opportunity.call1_calendar_id TEXT — the calendar the derived 1st call
                                  was booked on. Drives the calendar-derived
                                  "Booking source" breakdown.

Revision ID: 055
Revises: 054
"""
from alembic import op


revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ghl_calendar ADD COLUMN IF NOT EXISTS source_label TEXT")
    op.execute("ALTER TABLE ghl_opportunity ADD COLUMN IF NOT EXISTS call1_calendar_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE ghl_opportunity DROP COLUMN IF EXISTS call1_calendar_id")
    op.execute("ALTER TABLE ghl_calendar DROP COLUMN IF EXISTS source_label")
