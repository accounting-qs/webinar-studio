"""063_weekly_report_settings

Weekly webinar report emailed via Resend.

New singleton table report_settings (id=1, seeded on first read):
- enabled       BOOLEAN     — toggle for the weekly report job
- day_of_week   VARCHAR(3)  — mon..sun (default wed)
- hour_local    INTEGER     — hour of day (0-23)
- minute_local  INTEGER     — minute (0-59)
- timezone      TEXT        — IANA timezone for the send time
- recipients    JSONB       — list of recipient email addresses
- from_address  TEXT        — Resend from address (verified domain)
- last_sent_at  TIMESTAMPTZ — last successful send (also double-send guard)
- last_error    TEXT        — last failure message, cleared on success

Revision ID: 063
Revises: 061
"""
from alembic import op


revision = "063"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS report_settings (
            id            INTEGER PRIMARY KEY,
            enabled       BOOLEAN NOT NULL DEFAULT FALSE,
            day_of_week   VARCHAR(3) NOT NULL DEFAULT 'wed',
            hour_local    INTEGER NOT NULL DEFAULT 14,
            minute_local  INTEGER NOT NULL DEFAULT 0,
            timezone      TEXT NOT NULL DEFAULT 'America/Chicago',
            recipients    JSONB NOT NULL DEFAULT '["geri@quantum-scaling.com"]'::jsonb,
            from_address  TEXT NOT NULL DEFAULT 'reports@qs-institutes.com',
            last_sent_at  TIMESTAMPTZ,
            last_error    TEXT,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS report_settings")
