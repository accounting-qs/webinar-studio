"""051_index_contact_fk_columns

Index the two foreign-key columns that reference contacts.id with
ON DELETE SET NULL: webinar_calendar_invites.matched_contact_id and
contact_release_log.contact_id. Neither was indexed, so deleting a contact
forced Postgres to sequentially scan both tables (~1.8M / ~260k rows) once per
deleted row to null the references — a single 7.6k-row cleanup ran for minutes
and tripped statement_timeout. With these indexes the SET NULL lookups become
index probes and contact deletes are fast again.

Revision ID: 051
Revises: 050
"""
from alembic import op


revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_wci_matched_contact_id "
        "ON webinar_calendar_invites (matched_contact_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_release_log_contact "
        "ON contact_release_log (contact_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_release_log_contact")
    op.execute("DROP INDEX IF EXISTS ix_wci_matched_contact_id")
