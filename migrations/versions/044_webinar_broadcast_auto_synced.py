"""044_webinar_broadcast_auto_synced

Adds `webinars.broadcast_auto_synced_at` — a one-shot timestamp marking that the
scheduler has already auto-synced this planned webinar's WebinarGeek broadcast
subscribers. The scheduler fires once, 2 hours after the broadcast's start time
(webinargeek_webinars.starts_at). NULL means "not yet auto-synced". The partial
index backs the scheduler's due-scan.

Revision ID: 044
Revises: 043
"""
from alembic import op


revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE webinars ADD COLUMN IF NOT EXISTS broadcast_auto_synced_at TIMESTAMPTZ"
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_webinars_broadcast_autosync_due
        ON webinars (broadcast_id)
        WHERE broadcast_id IS NOT NULL AND broadcast_auto_synced_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_webinars_broadcast_autosync_due")
    op.execute("ALTER TABLE webinars DROP COLUMN IF EXISTS broadcast_auto_synced_at")
