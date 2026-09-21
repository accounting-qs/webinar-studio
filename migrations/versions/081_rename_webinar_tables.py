"""081_rename_webinar_tables

Zoom is being added as a second webinar platform alongside WebinarGeek, and
both write registrations + attendance into the same two tables — the whole
statistics layer joins them on (LOWER(email), webinars.broadcast_id) and is
otherwise provider-agnostic. Keeping the WebinarGeek names once Zoom rows live
in them would make every future reader mistrust the data.

    webinargeek_webinars    -> webinar_broadcasts
    webinargeek_subscribers -> webinar_registrants

This is catalog-only: no rows move, no table is rewritten, ACCESS EXCLUSIVE is
held for microseconds. Renaming a table does NOT rename its indexes or its
constraints, so those are renamed explicitly here — otherwise `ix_wg_subs_*`
and `ck_webinargeek_subscribers_*` would sit on tables that no longer carry
those names, which is exactly the confusion this migration removes.

Constraint/index names are looked up defensively rather than hardcoded: the
UNIQUE (broadcast_id, email) in migration 020 was declared inline, so its real
name is the Postgres-generated `webinargeek_subscribers_broadcast_id_email_key`
and not the `uq_wg_subs_broadcast_email` the ORM model declares. Same reason
migration 069 uses DO blocks — prod has had DDL applied out-of-band, and a
migration that raises crash-loops the service (start command is
`alembic upgrade head && uvicorn ...`).

The compatibility VIEWs are for the rolling deploy only. Render boots the new
instance (which runs this migration) while the OLD instance still serves
traffic for another 30-90s; without them every query on that old process would
hit `relation "webinargeek_subscribers" does not exist` and Statistics,
Contacts, Reports and Blocklist would all 500 for the overlap. Reads pass
through; writes do not (auto-updatable views reject ON CONFLICT DO UPDATE), so
a WebinarGeek sync caught mid-flight fails and retries on the next scheduler
tick. Migration 082 drops the views once the old process is gone.

Revision ID: 081
Revises: 080
"""
from alembic import op


revision = "081"
down_revision = "080"
branch_labels = None
depends_on = None


def _rename_constraint(table: str, old: str, new: str) -> str:
    """ALTER TABLE ... RENAME CONSTRAINT has no IF EXISTS; emulate it."""
    return f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}' AND conrelid = '{table}'::regclass
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{new}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
            END IF;
        END $$;
    """


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.execute("ALTER TABLE IF EXISTS webinargeek_webinars RENAME TO webinar_broadcasts")
    op.execute("ALTER TABLE IF EXISTS webinargeek_subscribers RENAME TO webinar_registrants")

    op.execute("ALTER INDEX IF EXISTS ix_wg_subs_broadcast RENAME TO ix_webinar_registrants_broadcast")
    op.execute("ALTER INDEX IF EXISTS ix_wg_subs_email RENAME TO ix_webinar_registrants_email")
    op.execute("ALTER INDEX IF EXISTS ix_wg_subscribers_lower_email RENAME TO ix_webinar_registrants_lower_email")
    op.execute("ALTER INDEX IF EXISTS ix_wg_webinars_credential RENAME TO ix_webinar_broadcasts_credential")

    # Primary keys, the inline UNIQUE from 020, the FK from 020 and the
    # lowercase CHECK from 069. Both the generated and the ORM-declared spelling
    # are attempted, since only one of each pair actually exists.
    op.execute(_rename_constraint(
        "webinar_broadcasts", "webinargeek_webinars_pkey", "webinar_broadcasts_pkey"))
    op.execute(_rename_constraint(
        "webinar_registrants", "webinargeek_subscribers_pkey", "webinar_registrants_pkey"))
    op.execute(_rename_constraint(
        "webinar_registrants", "webinargeek_subscribers_broadcast_id_email_key",
        "uq_webinar_registrants_broadcast_email"))
    op.execute(_rename_constraint(
        "webinar_registrants", "uq_wg_subs_broadcast_email",
        "uq_webinar_registrants_broadcast_email"))
    op.execute(_rename_constraint(
        "webinar_registrants", "webinargeek_subscribers_broadcast_id_fkey",
        "webinar_registrants_broadcast_id_fkey"))
    op.execute(_rename_constraint(
        "webinar_registrants", "ck_webinargeek_subscribers_email_lowercase",
        "ck_webinar_registrants_email_lowercase"))

    # Rolling-deploy read compatibility. Dropped in 082.
    op.execute("CREATE OR REPLACE VIEW webinargeek_subscribers AS SELECT * FROM webinar_registrants")
    op.execute("CREATE OR REPLACE VIEW webinargeek_webinars AS SELECT * FROM webinar_broadcasts")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS webinargeek_subscribers")
    op.execute("DROP VIEW IF EXISTS webinargeek_webinars")

    op.execute(_rename_constraint(
        "webinar_registrants", "ck_webinar_registrants_email_lowercase",
        "ck_webinargeek_subscribers_email_lowercase"))
    op.execute(_rename_constraint(
        "webinar_registrants", "webinar_registrants_broadcast_id_fkey",
        "webinargeek_subscribers_broadcast_id_fkey"))
    op.execute(_rename_constraint(
        "webinar_registrants", "uq_webinar_registrants_broadcast_email",
        "webinargeek_subscribers_broadcast_id_email_key"))
    op.execute(_rename_constraint(
        "webinar_registrants", "webinar_registrants_pkey", "webinargeek_subscribers_pkey"))
    op.execute(_rename_constraint(
        "webinar_broadcasts", "webinar_broadcasts_pkey", "webinargeek_webinars_pkey"))

    op.execute("ALTER INDEX IF EXISTS ix_webinar_broadcasts_credential RENAME TO ix_wg_webinars_credential")
    op.execute("ALTER INDEX IF EXISTS ix_webinar_registrants_lower_email RENAME TO ix_wg_subscribers_lower_email")
    op.execute("ALTER INDEX IF EXISTS ix_webinar_registrants_email RENAME TO ix_wg_subs_email")
    op.execute("ALTER INDEX IF EXISTS ix_webinar_registrants_broadcast RENAME TO ix_wg_subs_broadcast")

    op.execute("ALTER TABLE IF EXISTS webinar_registrants RENAME TO webinargeek_subscribers")
    op.execute("ALTER TABLE IF EXISTS webinar_broadcasts RENAME TO webinargeek_webinars")
