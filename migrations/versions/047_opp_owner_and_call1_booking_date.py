"""047_opp_owner_and_call1_booking_date

Add opportunity owner (Sales Rep) + Call 1 booking date to ghl_opportunity so
the Bookings drill-down can show them:
- call1_booking_date (TIMESTAMPTZ) — GHL opp "Call 1: Date of Booking"
- assigned_to_id (TEXT)            — GHL opp `assignedTo` user id
- owner_name (TEXT)                — resolved Sales Rep name (denormalized)

Nullable; populated on the next GHL opportunity sync.

Revision ID: 047
Revises: 046
"""
from alembic import op


revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


COLUMNS = [
    ("call1_booking_date", "TIMESTAMPTZ"),
    ("assigned_to_id", "TEXT"),
    ("owner_name", "TEXT"),
]


def upgrade() -> None:
    for name, typ in COLUMNS:
        op.execute(f"ALTER TABLE ghl_opportunity ADD COLUMN IF NOT EXISTS {name} {typ}")


def downgrade() -> None:
    for name, _ in COLUMNS:
        op.execute(f"ALTER TABLE ghl_opportunity DROP COLUMN IF EXISTS {name}")
