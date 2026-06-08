"""046_book_campaign_content_term_id

Add the remaining "Book - Campaign *" UTM fields on ghl_contact so the
Bookings drill-down can show them alongside Source / Medium / Name:
- book_campaign_content (TEXT)  — GHL "Book - Campaign Content" (utm_content)
- book_campaign_term (TEXT)     — GHL "Book - Campaign Term"    (utm_term)
- book_campaign_id (TEXT)       — GHL "Book - Campaign ID"

Nullable; populated on the next GHL contact sync.

Revision ID: 046
Revises: 045
"""
from alembic import op


revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


COLUMNS = [
    ("book_campaign_content", "TEXT"),
    ("book_campaign_term", "TEXT"),
    ("book_campaign_id", "TEXT"),
]


def upgrade() -> None:
    for name, typ in COLUMNS:
        op.execute(f"ALTER TABLE ghl_contact ADD COLUMN IF NOT EXISTS {name} {typ}")


def downgrade() -> None:
    for name, _ in COLUMNS:
        op.execute(f"ALTER TABLE ghl_contact DROP COLUMN IF EXISTS {name}")
