"""082_drop_rename_compat_views

Drops the `webinargeek_subscribers` / `webinargeek_webinars` compatibility
views created by 081. They existed only to keep the previous release's
instance serving reads during the 30-90s rolling-deploy overlap; once that
process is gone they are dead weight, and leaving them would let a stale
reference keep working silently instead of failing loudly.

Deploy this separately from 081 — same day is fine, but not in the same
release, or the views are gone before the old process has drained and the
overlap outage 081 was written to prevent happens anyway.

Revision ID: 082
Revises: 081
"""
from alembic import op


revision = "082"
down_revision = "081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS webinargeek_subscribers")
    op.execute("DROP VIEW IF EXISTS webinargeek_webinars")


def downgrade() -> None:
    op.execute("CREATE OR REPLACE VIEW webinargeek_subscribers AS SELECT * FROM webinar_registrants")
    op.execute("CREATE OR REPLACE VIEW webinargeek_webinars AS SELECT * FROM webinar_broadcasts")
