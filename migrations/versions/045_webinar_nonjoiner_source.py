"""045_webinar_nonjoiner_source

Adds webinars.nonjoiner_source_webinar_id — an optional self-reference to the
PREVIOUS webinar whose WebinarGeek broadcast supplies this webinar's Nonjoiners
(registrants of that broadcast who did NOT watch live). NULL → fall back to the
GHL-based nonjoiner computation.

Revision ID: 045
Revises: 044
"""
from alembic import op


revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE webinars
        ADD COLUMN IF NOT EXISTS nonjoiner_source_webinar_id UUID
            REFERENCES webinars(id) ON DELETE SET NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webinars_nonjoiner_source "
        "ON webinars (nonjoiner_source_webinar_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_webinars_nonjoiner_source")
    op.execute("ALTER TABLE webinars DROP COLUMN IF EXISTS nonjoiner_source_webinar_id")
