"""070_split_raw_custom_fields

Moves ghl_contact.raw_custom_fields and ghl_opportunity.raw_custom_fields into
1:1 side tables so the hot tables stop carrying them.

Why: raw_custom_fields is written by every sync and read by nothing — no SELECT
in the backend, no full-entity ORM load, no frontend reference. It is the single
biggest contributor to ghl_contact's width: measured on prod, it averages 1,092
bytes/row detoasted, and only 15.5% of rows exceed the 2 KB TOAST threshold, so
the large majority sits *in-line* and inflates the heap that every statistics
scan has to read. ghl_contact is 2,673 MB of heap (932 bytes/row) plus 1,513 MB
of TOAST.

Every per-webinar statistics query joins ghl_contact on email, so that width is
paid on the hot path, repeatedly, on an instance whose cache is far smaller than
its working set. Splitting it out shrinks the pages those scans touch without
losing any data.

The data is kept, not dropped: the side tables hold it verbatim, keyed by the
same id, so anything that ever needs the custom fields can join to it.

No foreign key to the parent tables. It would be the natural modelling choice,
but the sync upserts these rows in very large batches and a per-row FK check on
that path costs more than the referential guarantee is worth here; the id is
sourced from the parent row in the same transaction. ON DELETE CASCADE is
likewise skipped for the same reason — orphan rows are harmless (they are
write-only payloads) and a periodic cleanup is cheaper than per-row enforcement.

Applied to prod out-of-band during a temporary XL resize, since the backfill
copies ~3 GB and the column drop needs a table rewrite to actually reclaim the
space (DROP COLUMN alone is metadata-only). upgrade() below is the equivalent
for other environments and is written to be re-runnable.

Revision ID: 070
Revises: 069
"""
from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


_SPLITS = [
    ("ghl_contact", "ghl_contact_custom_fields", "ghl_contact_id"),
    ("ghl_opportunity", "ghl_opportunity_custom_fields", "ghl_opportunity_id"),
]


def upgrade() -> None:
    for parent, side, key in _SPLITS:
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {side} (
                {key}            TEXT PRIMARY KEY,
                raw_custom_fields JSONB,
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # Idempotent backfill: safe to re-run, and a no-op once the parent
        # column is gone (guarded below so re-running after the drop works).
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = '{parent}' AND column_name = 'raw_custom_fields'
                ) THEN
                    EXECUTE 'INSERT INTO {side} ({key}, raw_custom_fields)
                             SELECT {key}, raw_custom_fields FROM {parent}
                             WHERE raw_custom_fields IS NOT NULL
                             ON CONFLICT ({key}) DO NOTHING';
                    EXECUTE 'ALTER TABLE {parent} DROP COLUMN raw_custom_fields';
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    for parent, side, key in _SPLITS:
        op.execute(f"ALTER TABLE {parent} ADD COLUMN IF NOT EXISTS raw_custom_fields JSONB")
        op.execute(
            f"""
            UPDATE {parent} p SET raw_custom_fields = s.raw_custom_fields
            FROM {side} s WHERE s.{key} = p.{key}
            """
        )
        op.execute(f"DROP TABLE IF EXISTS {side}")
