"""056_daily_sales_sync_schedule

Daily scheduled "Sales + Calls" sync (all opportunities + appointments).

- ghl_sync_settings.daily_sales_enabled     BOOLEAN — toggle for the daily job
- ghl_sync_settings.daily_sales_hour_local  INTEGER — hour of day (0-23)
- ghl_sync_settings.daily_sales_timezone    TEXT    — IANA timezone for the hour

Revision ID: 056
Revises: 055
"""
from alembic import op


revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ghl_sync_settings ADD COLUMN IF NOT EXISTS daily_sales_enabled BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE ghl_sync_settings ADD COLUMN IF NOT EXISTS daily_sales_hour_local INTEGER NOT NULL DEFAULT 6")
    op.execute("ALTER TABLE ghl_sync_settings ADD COLUMN IF NOT EXISTS daily_sales_timezone TEXT NOT NULL DEFAULT 'America/Chicago'")


def downgrade() -> None:
    op.execute("ALTER TABLE ghl_sync_settings DROP COLUMN IF EXISTS daily_sales_timezone")
    op.execute("ALTER TABLE ghl_sync_settings DROP COLUMN IF EXISTS daily_sales_hour_local")
    op.execute("ALTER TABLE ghl_sync_settings DROP COLUMN IF EXISTS daily_sales_enabled")
