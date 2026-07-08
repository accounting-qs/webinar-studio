"""054_ghl_appointments

Calendar-derived 1st/2nd call tracking. Replaces the unreliable GHL opportunity
custom fields (which under-counted first calls by ~80% after a calendar swap)
with the real calendar appointments booked on the contact.

- ghl_calendar    — cached {calendar_id: name} + name-based classification
                    (first | followup | exclude) + optional funnel_tag.
- ghl_appointment — one row per GHL calendar appointment (from
                    GET /contacts/{id}/appointments); calendar_class denormalized
                    so a deleted calendar can't strand the row.
- ghl_opportunity — add call2_appointment_date/status + call1_source provenance
                    ('calendar' | 'custom_field'). The three existing call1_*
                    columns are repopulated from calendar-derived truth.

Revision ID: 054
Revises: 053
"""
from alembic import op


revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


OPP_COLUMNS = [
    ("call2_appointment_date", "TIMESTAMPTZ"),
    ("call2_appointment_status", "TEXT"),
    ("call1_source", "TEXT"),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ghl_calendar (
            calendar_id TEXT PRIMARY KEY,
            name TEXT,
            calendar_class TEXT,
            funnel_tag TEXT,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS ghl_appointment (
            appointment_id TEXT PRIMARY KEY,
            ghl_contact_id TEXT,
            calendar_id TEXT,
            calendar_class TEXT,
            start_time TIMESTAMPTZ,
            status TEXT,
            booked_at TIMESTAMPTZ,
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            raw JSONB,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_ghl_appt_contact ON ghl_appointment (ghl_contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ghl_appt_class ON ghl_appointment (calendar_class)")

    for name, typ in OPP_COLUMNS:
        op.execute(f"ALTER TABLE ghl_opportunity ADD COLUMN IF NOT EXISTS {name} {typ}")


def downgrade() -> None:
    for name, _ in OPP_COLUMNS:
        op.execute(f"ALTER TABLE ghl_opportunity DROP COLUMN IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS ghl_appointment")
    op.execute("DROP TABLE IF EXISTS ghl_calendar")
