"""052_add_statistics_snapshot

Persisted per-webinar statistics snapshots. Each row stores the fully-computed
statistics payload for one webinar-variant + source, so the dashboard reads it
back instantly instead of recomputing the ~30s contacts↔ghl_contact join on
every page load. Rebuilt by services.statistics_snapshot.recompute() after any
source data change (GHL sync, WebinarGeek sync, calendar upload, webinar edit)
or a manual trigger.

Revision ID: 052
Revises: 051
"""
from alembic import op


revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS statistics_snapshot (
            source          VARCHAR(20)  NOT NULL,
            webinar_id      TEXT         NOT NULL,
            webinar_number  INTEGER,
            variant_label   TEXT,
            payload         JSONB        NOT NULL,
            computed_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (source, webinar_id)
        )
        """
    )
    # Recompute status / "last updated" read MAX(computed_at) scoped by source.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_statistics_snapshot_source_computed "
        "ON statistics_snapshot (source, computed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_statistics_snapshot_source_computed")
    op.execute("DROP TABLE IF EXISTS statistics_snapshot")
