"""066_add_webinar_booking_attribution

Webinar-Studio-owned per-booking → webinar attribution, so per-webinar sales
outcomes survive GHL overwriting its single opportunity per contact. One row per
first-call ghl_appointment (appointment_id PK): the webinar that drove the booked
call, snapshotted call outcome / lead_quality / won, an attribution_source, and a
locked_at that freezes the attribution once trusted (capture-forward). The sales
funnel reads this instead of the collapsed, latest-only opportunity.

Additive (new table only) → forward-compatible with the code currently on prod.

Revision ID: 066
Revises: 065
"""
from alembic import op


revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webinar_booking_attribution (
            appointment_id     TEXT PRIMARY KEY,
            ghl_contact_id     TEXT,
            contact_id         UUID,
            webinar_id         UUID,
            attribution_source TEXT,
            booked_at          TIMESTAMPTZ,
            call_at            TIMESTAMPTZ,
            call_status        TEXT,
            lead_quality       TEXT,
            won                BOOLEAN,
            disqualified       BOOLEAN,
            locked_at          TIMESTAMPTZ,
            synced_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wba_webinar ON webinar_booking_attribution (webinar_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wba_contact ON webinar_booking_attribution (ghl_contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wba_app_contact ON webinar_booking_attribution (contact_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webinar_booking_attribution")
