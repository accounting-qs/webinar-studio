"""071_add_webinar_report_request

Durable queue for per-webinar report generation. Generation is a 2-4 minute
in-process background task; a deploy, OOM restart or crash mid-run silently
killed the request and the report never appeared (the button looked broken).

Each Generate click (or scheduler prep) now upserts a request row first. The
worker deletes the row when the report lands; a scheduler sweep retries any
row that is still present and not currently running, so requests survive
restarts. attempts caps runaway retries on genuinely failing webinars.

Additive (new table only) -> forward-compatible with the code currently on prod.

Revision ID: 071
Revises: 070
"""
from alembic import op

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS webinar_report_request (
            webinar_id    TEXT PRIMARY KEY,
            requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS webinar_report_request")
