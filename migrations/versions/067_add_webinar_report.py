"""067_add_webinar_report

Per-webinar report artifacts. Each row is a frozen, fully-computed report for
one webinar variant: the heavy numbers payload (scorecard vs all-webinar and
last-4-week baselines, per-dimension funnels for industry / geography /
employee size / segments, bookings deep-dive, non-joiner package, caveats)
plus the AI-generated insights (Claude) that render at the bottom of the
report page and inside the weekly email.

Reports are generated in the background (mirroring statistics_snapshot's
recompute pattern) by services.webinar_report: automatically 15 minutes before
the weekly email send, on first view from the Statistics page "Report" button,
or via the manual Regenerate action. The page/email only ever read this table —
they never recompute.

Additive (new table only) -> forward-compatible with the code currently on prod.

Revision ID: 067
Revises: 066
"""
from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webinar_report (
            webinar_id      TEXT PRIMARY KEY,
            webinar_number  INTEGER,
            variant_label   TEXT,
            payload         JSONB NOT NULL,
            insights        JSONB,
            insights_model  TEXT,
            ai_error        TEXT,
            generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            generation_ms   INTEGER
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_webinar_report_number"
        " ON webinar_report (webinar_number)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webinar_report")
