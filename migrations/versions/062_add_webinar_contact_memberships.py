"""062_add_webinar_contact_memberships

Restructure invitation state from the single denormalized slot on `contacts`
(assignment_id / outreach_status / assigned_date / used_at — one webinar at a
time) into a many-to-many membership table so a contact can be invited to
multiple webinars over time, each tracked independently.

Schema only — fast and lock-light:
  - webinar_contact_memberships: one row per (contact, webinar-list). Created
      empty, so all its indexes build instantly.
  - contacts.{assigned_membership_count, times_invited} INT NOT NULL DEFAULT 0
      and contacts.last_invited_at TIMESTAMPTZ — denormalized caches for the
      reuse filter / dashboards. Constant-default column adds are metadata-only
      in PG 11+, so no table rewrite.

Deliberately NOT here (run scripts/backfill_reusable_contacts.py after deploy):
  - backfilling membership rows from the current live contacts slot;
  - recomputing the three cache columns from those memberships;
  - ix_contacts_claimable — a partial index over the whole contacts table, built
      CONCURRENTLY by the script to avoid a long write lock.

The legacy single-slot columns (assignment_id / outreach_status / assigned_date /
used_at) are intentionally LEFT IN PLACE here; they are dropped in a later
migration after the membership model has fully taken over the read/write paths.

Revision ID: 062
Revises: 061
"""
from alembic import op


revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webinar_contact_memberships (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            contact_id     UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
            webinar_id     UUID NOT NULL REFERENCES webinars(id) ON DELETE CASCADE,
            assignment_id  UUID REFERENCES webinar_list_assignments(id) ON DELETE SET NULL,
            bucket_id      UUID REFERENCES outreach_buckets(id) ON DELETE SET NULL,
            status         VARCHAR(20) NOT NULL DEFAULT 'assigned',
            assigned_date  DATE,
            used_at        TIMESTAMPTZ,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_wcm_status CHECK (status IN ('assigned', 'used')),
            CONSTRAINT uq_wcm_webinar_contact UNIQUE (webinar_id, contact_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wcm_assignment ON webinar_contact_memberships (assignment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wcm_contact ON webinar_contact_memberships (contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wcm_webinar_status ON webinar_contact_memberships (webinar_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wcm_bucket ON webinar_contact_memberships (bucket_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wcm_user ON webinar_contact_memberships (user_id)")

    op.execute(
        "ALTER TABLE contacts "
        "ADD COLUMN IF NOT EXISTS assigned_membership_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE contacts "
        "ADD COLUMN IF NOT EXISTS times_invited INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE contacts "
        "ADD COLUMN IF NOT EXISTS last_invited_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_contacts_claimable")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS last_invited_at")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS times_invited")
    op.execute("ALTER TABLE contacts DROP COLUMN IF EXISTS assigned_membership_count")
    op.execute("DROP TABLE IF EXISTS webinar_contact_memberships")
