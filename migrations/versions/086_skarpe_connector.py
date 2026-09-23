"""086_skarpe_connector

Skarpe (calendar-invitation campaign tool) joins the connectors. Two schema
pieces:

1. connector_credentials.base_url — Skarpe has a staging and a production
   backend, and each API key only works against its own host, so the endpoint
   is part of the credential, not a constant. Nullable: every other provider
   keeps its hardcoded base URL. location_id/pipeline_id/client_id already set
   the precedent that provider-specific columns live on this small table.

2. skarpe_campaigns — one row per (Skarpe workspace credential, assigned list)
   draft campaign created from the Planning page. A dedicated table rather
   than columns on webinar_list_assignments because one list can legitimately
   have campaigns in several workspaces (staging trial + production push),
   and because the linkage carries its own lifecycle (push progress, policy
   confirmation, status/error) that the future "mark contacts used when the
   campaign starts" phase will query by credential. external_ref mirrors
   Skarpe's idempotency key (we send the assignment id), so re-running a
   partially failed batch converges on the same campaigns.

Revision ID: 086
Revises: 085
"""
from alembic import op


revision = "086"
down_revision = "085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS base_url TEXT")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skarpe_campaigns (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            assignment_id UUID NOT NULL REFERENCES webinar_list_assignments(id) ON DELETE CASCADE,
            credential_id UUID REFERENCES connector_credentials(id) ON DELETE SET NULL,
            skarpe_campaign_id TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            app_url TEXT,
            title TEXT,
            webinar_number INTEGER,
            accounts_attached INTEGER NOT NULL DEFAULT 0,
            contacts_total INTEGER NOT NULL DEFAULT 0,
            contacts_pushed INTEGER NOT NULL DEFAULT 0,
            policy_version TEXT,
            policy_hash TEXT,
            policy_confirmed_by TEXT,
            policy_confirmed_at TIMESTAMPTZ,
            status TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_skarpe_campaigns_status CHECK (status IN
                ('pending', 'draft_created', 'accounts_attached',
                 'pushing_contacts', 'completed', 'failed')),
            CONSTRAINT uq_skarpe_campaigns_credential_assignment
                UNIQUE (credential_id, assignment_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_skarpe_campaigns_assignment ON skarpe_campaigns (assignment_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_skarpe_campaigns_credential ON skarpe_campaigns (credential_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skarpe_campaigns")
    op.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS base_url")
