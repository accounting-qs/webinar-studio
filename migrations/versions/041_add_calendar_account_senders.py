"""041_add_calendar_account_senders

Maps each (webinar_id, calendar_account) pair to a Sender from
outreach_senders so the Account Health view can group accounts by who
sends from them.

Two writes paths:
- Optional sender_id on a calendar upload (Pattern A): when the CSV
  import finishes, every distinct calendar_account in that upload gets
  a row in calendar_account_senders pointing to that sender.
- Bulk-paste modal (Pattern B): user picks webinar + sender, pastes
  newline-separated accounts; each (webinar, account) is upserted to
  that sender.

Re-saving a (webinar, account) pair overwrites the previous sender —
the last write wins (matches Pattern B's "I'm fixing the assignment"
intent).

Revision ID: 041
Revises: 040
"""
from alembic import op


revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pattern A: remember which sender was selected at upload time.
    op.execute("""
        ALTER TABLE webinar_calendar_uploads
        ADD COLUMN IF NOT EXISTS sender_id UUID
            REFERENCES outreach_senders(id) ON DELETE SET NULL
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wcu_sender ON webinar_calendar_uploads (sender_id)"
    )

    # Resolved (webinar, account) → sender mapping that drives Account
    # Health's Sender column and the per-sender sub-tabs.
    op.execute("""
        CREATE TABLE IF NOT EXISTS calendar_account_senders (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            webinar_id UUID NOT NULL REFERENCES webinars(id) ON DELETE CASCADE,
            calendar_account TEXT NOT NULL,
            sender_id UUID NOT NULL REFERENCES outreach_senders(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_cas_webinar_account UNIQUE (webinar_id, calendar_account)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_cas_user ON calendar_account_senders (user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cas_webinar ON calendar_account_senders (webinar_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cas_sender ON calendar_account_senders (sender_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cas_account ON calendar_account_senders (calendar_account)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calendar_account_senders")
    op.execute("DROP INDEX IF EXISTS ix_wcu_sender")
    op.execute("ALTER TABLE webinar_calendar_uploads DROP COLUMN IF EXISTS sender_id")
