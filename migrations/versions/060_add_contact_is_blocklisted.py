"""060_add_contact_is_blocklisted

Denormalize blocklist membership onto contacts as a boolean flag so read paths
(per-bucket / per-assignment blocklist counts, and the `not blocklisted` claim
filters) stop scanning the blocklist for every contact on every page load.

Schema only — fast and lock-light:
  - contacts.is_blocklisted  BOOLEAN NOT NULL DEFAULT false
      Adding a column with a constant default is metadata-only in PG 11+, so no
      table rewrite.
  - two PARTIAL indexes keyed WHERE is_blocklisted. The column starts all-false,
      so these indexes are empty at creation → instant, negligible lock.

Deliberately NOT here (run scripts/backfill_contact_blocklist.py after deploy):
  - ix_contacts_lower_email — an index over the whole table; built CONCURRENTLY
      by the script to avoid a long write lock.
  - the data backfill that flips the flag for existing blocklisted contacts;
      chunked with per-batch commits to respect the 120s statement_timeout.

Revision ID: 060
Revises: 059
"""
from alembic import op


revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE contacts "
        "ADD COLUMN IF NOT EXISTS is_blocklisted BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_contacts_bl_assignment "
        "ON contacts (assignment_id, outreach_status) WHERE is_blocklisted"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_contacts_bl_bucket "
        "ON contacts (bucket_id, outreach_status) WHERE is_blocklisted"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_contacts_bl_bucket")
    op.execute("DROP INDEX IF EXISTS ix_contacts_bl_assignment")
    op.execute("DROP INDEX IF EXISTS ix_contacts_lower_email")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS is_blocklisted")
