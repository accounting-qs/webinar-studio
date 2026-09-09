"""079_calendar_upload_counts_pending

A Non-joiners upload's Matched/No-List-Data numbers are not per-row matching —
they are a set intersection with the derived non-joiner group, resolved once
after the rows are in (services/nonjoiners.py). That query is expensive, and
when it failed (E156: it hit the 600s statement cap while three big calendar
imports were still running) the worker swallowed the error and left both
counters at 0 — indistinguishable from "nothing matched".

counts_pending marks that state: the rows imported fine, the count did not
resolve. The UI renders "—" plus a Recount action instead of a false 0.

Revision ID: 079
Revises: 078
"""
from alembic import op


revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE webinar_calendar_uploads "
        "ADD COLUMN IF NOT EXISTS counts_pending BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE webinar_calendar_uploads DROP COLUMN IF EXISTS counts_pending")
