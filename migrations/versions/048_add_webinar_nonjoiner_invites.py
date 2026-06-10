"""048_add_webinar_nonjoiner_invites

Per-webinar Non-joiners CSV ingestion (email + calendar response Yes/Maybe).

- webinar_calendar_uploads gains a `kind` column ('calendar' | 'nonjoiner')
  so the existing upload pipeline (presign/confirm/import/status/delete) is
  reused; the kind routes parsing + destination table.
- webinar_nonjoiner_invites: one row per uploaded Non-joiners record. Upsert
  key (webinar_id, email): re-uploading the same email for the same webinar
  updates the row; the same email across different webinars yields independent
  rows. Kept separate from webinar_calendar_invites so a Non-joiners upload
  never triggers the normal calendar-CSV mode for planned lists / No-List-Data.

Revision ID: 048
Revises: 047
"""
from alembic import op


revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE webinar_calendar_uploads "
        "ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'calendar'"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS webinar_nonjoiner_invites (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            upload_id UUID NOT NULL REFERENCES webinar_calendar_uploads(id) ON DELETE CASCADE,
            webinar_id UUID NOT NULL REFERENCES webinars(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            calendar_invite_response TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_wnji_webinar_email UNIQUE (webinar_id, email)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_wnji_webinar_email ON webinar_nonjoiner_invites (webinar_id, email)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wnji_webinar_response ON webinar_nonjoiner_invites (webinar_id, calendar_invite_response)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wnji_upload ON webinar_nonjoiner_invites (upload_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webinar_nonjoiner_invites")
    op.execute("ALTER TABLE webinar_calendar_uploads DROP COLUMN IF EXISTS kind")
