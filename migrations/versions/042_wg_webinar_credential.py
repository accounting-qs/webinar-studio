"""042_wg_webinar_credential

Track which WebinarGeek account each cached broadcast was synced from.
Adds `webinargeek_webinars.credential_id` (FK → connector_credentials.id,
ON DELETE SET NULL). Populated by the refresh endpoint on every sync;
existing rows stay NULL until the next refresh re-stamps them.

Revision ID: 042
Revises: 041
"""
from alembic import op


revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE webinargeek_webinars
        ADD COLUMN IF NOT EXISTS credential_id UUID
            REFERENCES connector_credentials(id) ON DELETE SET NULL
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wg_webinars_credential ON webinargeek_webinars (credential_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_wg_webinars_credential")
    op.execute("ALTER TABLE webinargeek_webinars DROP COLUMN IF EXISTS credential_id")
