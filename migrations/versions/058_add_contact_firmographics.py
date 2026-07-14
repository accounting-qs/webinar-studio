"""058_add_contact_firmographics

Store additional person/company firmographics from lead CSVs so we can
A/B test outreach by location and segment on richer data.

Adds to contacts:
- title                   TEXT     — person job title
- seniority               TEXT     — person seniority (e.g. Manager, Director)
- employee_count          INTEGER  — raw headcount (when the source gives a number
                                     rather than a range); on import the raw count is
                                     also bucketed into employee_range so that stays
                                     the single unified size filter key
- company_founded_year    TEXT     — kept as text (raw CSV value)
- company_total_funding   TEXT     — kept as text (raw CSV value)
- company_annual_revenue  TEXT     — kept as text (raw CSV value)
- company_country         TEXT     — company HQ country

Revision ID: 058
Revises: 057
"""
from alembic import op


revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("title", "TEXT"),
    ("seniority", "TEXT"),
    ("employee_count", "INTEGER"),
    ("company_founded_year", "TEXT"),
    ("company_total_funding", "TEXT"),
    ("company_annual_revenue", "TEXT"),
    ("company_country", "TEXT"),
)


def upgrade() -> None:
    for name, coltype in _COLUMNS:
        op.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {name} {coltype}")


def downgrade() -> None:
    for name, _ in _COLUMNS:
        op.execute(f"ALTER TABLE contacts DROP COLUMN IF EXISTS {name}")
