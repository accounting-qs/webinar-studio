"""043_rename_characteristic_to_sub_identity

Rename contacts.characteristic → contacts.sub_identity to match the
new terminology used in the CSV import UI.

Revision ID: 043
Revises: 042
"""
from alembic import op


revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE contacts RENAME COLUMN characteristic TO sub_identity")


def downgrade() -> None:
    op.execute("ALTER TABLE contacts RENAME COLUMN sub_identity TO characteristic")
