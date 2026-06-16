"""049_add_sop_videos

SOP walkthrough videos shown on the SOP page — title, subtitle, and a Loom
share URL, all editable in-app. Seeds the two fixed entries (Promotion
planning, Statistics). Idempotent: only seeds when the table is empty.

Revision ID: 049
Revises: 048
"""
from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS sop_videos (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL DEFAULT '',
            loom_url TEXT NOT NULL DEFAULT '',
            display_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Seed the two fixed walkthroughs only when the table is empty so reruns
    # never clobber operator edits.
    op.execute("""
        INSERT INTO sop_videos (title, subtitle, loom_url, display_order)
        SELECT * FROM (VALUES
            ('Promotion planning', 'Covering the promotion part',
             'https://www.loom.com/share/317e5c0dff5a4ed19dfa566465e3ba07', 0),
            ('Statistics', 'For the statistics part and the links',
             'https://www.loom.com/share/d491fa4db14445e58becc6c48ea83c53', 1)
        ) AS seed(title, subtitle, loom_url, display_order)
        WHERE NOT EXISTS (SELECT 1 FROM sop_videos)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sop_videos")
