"""085_bucket_copy_internal_name

Short user-set label on a copy variant ("Nov angle", "ROI hook") so
description variants can be told apart at a glance in the copy generator.
Free text, nullable — absent means unnamed.

Revision ID: 085
Revises: 084
"""
from alembic import op


revision = "085"
down_revision = "084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE bucket_copies ADD COLUMN IF NOT EXISTS internal_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE bucket_copies DROP COLUMN IF EXISTS internal_name")
