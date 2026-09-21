"""083_provider_discriminator

Marks which webinar platform produced each cached broadcast and each synced
registrant, ahead of the Zoom integration itself. Existing rows default to
'webinargeek', so this deploy is a no-op for current behaviour and can be
verified as such before any Zoom code ships.

`provider` on webinar_broadcasts is load-bearing at exactly three call sites
(the WebinarGeek broadcast picker, its sync-all count, and run_sync_all) —
without it those would pick up Zoom broadcasts and, in the last case, try to
sync them with a WebinarGeek API key.

On webinar_registrants it is strictly redundant (derivable through the FK) but
worth the one TEXT column: the ~20 raw-SQL sites join on broadcast_id alone,
and provenance is otherwise invisible in psql.

Also widened here, both binary-coercible and therefore rewrite-free:

- webinar_*.broadcast_id was VARCHAR(64) while webinars.broadcast_id is TEXT.
  An asymmetry with a ceiling, and Zoom ids are longer than WebinarGeek's.
  Parent before child so the FK stays valid.
- ghl_sync_run.sync_type was VARCHAR(32). WebinarGeek's "wg:<id>" fits; Zoom's
  "zoom:<webinar_id>:<occurrence_id>" does not, and the overflow would kill the
  sync at INSERT with an error pointing nowhere near the cause.

Revision ID: 083
Revises: 082
"""
from alembic import op


revision = "083"
down_revision = "082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Metadata-only on PG 11+: no table rewrite despite the NOT NULL DEFAULT.
    op.execute(
        "ALTER TABLE webinar_broadcasts "
        "ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'webinargeek'"
    )
    op.execute(
        "ALTER TABLE webinar_registrants "
        "ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'webinargeek'"
    )

    # Zoom-specific identity. NULL for WebinarGeek rows, and NULL for a Zoom
    # webinar until it has actually aired — the instance UUID does not exist
    # before then, which is why it cannot live in broadcast_id.
    op.execute("ALTER TABLE webinar_broadcasts ADD COLUMN IF NOT EXISTS platform_instance_id TEXT")
    op.execute("ALTER TABLE webinar_broadcasts ADD COLUMN IF NOT EXISTS occurrence_id TEXT")

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webinar_broadcasts_provider "
        "ON webinar_broadcasts (provider)"
    )

    op.execute("ALTER TABLE webinar_broadcasts  ALTER COLUMN broadcast_id TYPE TEXT")
    op.execute("ALTER TABLE webinar_registrants ALTER COLUMN broadcast_id TYPE TEXT")

    op.execute("ALTER TABLE ghl_sync_run ALTER COLUMN sync_type TYPE TEXT")


def downgrade() -> None:
    # Deliberately does not narrow the types back: any Zoom-era row would fail
    # the truncation, and a downgrade should not be able to lose data.
    op.execute("DROP INDEX IF EXISTS ix_webinar_broadcasts_provider")
    op.execute("ALTER TABLE webinar_broadcasts DROP COLUMN IF EXISTS occurrence_id")
    op.execute("ALTER TABLE webinar_broadcasts DROP COLUMN IF EXISTS platform_instance_id")
    op.execute("ALTER TABLE webinar_registrants DROP COLUMN IF EXISTS provider")
    op.execute("ALTER TABLE webinar_broadcasts DROP COLUMN IF EXISTS provider")
