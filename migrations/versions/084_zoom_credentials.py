"""084_zoom_credentials

Zoom Server-to-Server OAuth needs three values, not one: Account ID, Client ID
and Client Secret. connector_credentials had a single api_key slot plus two
GHL-specific columns (location_id, pipeline_id).

The client secret goes in api_key -- it is the secret, so _mask() and the
generic DELETE keep working unchanged -- and the two non-secret identifiers get
their own named columns. Overloading location_id for account_id was the
alternative and would have been actively misleading; location_id/pipeline_id
already set the precedent that provider-specific columns live on this table,
which has fewer than ten rows.

Nullable because every other provider leaves them empty.

No access-token column on purpose: the token lives 3600s with no refresh token,
so it is cached in memory in integrations/zoom_client.py. Persisting it would
mean writing a bearer token into a plaintext table every 55 minutes and reading
it back on every call, for nothing.

Revision ID: 084
Revises: 083
"""
from alembic import op


revision = "084"
down_revision = "083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS client_id TEXT")
    op.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS account_id")
    op.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS client_id")
