"""077_email_domain_provider

Cache of email domain -> mailbox provider, resolved from MX records.

The audience is ~98% company domains, so the domain name alone says nothing
about which mailbox actually receives the calendar invite. The MX host does:
aspmx.l.google.com means Google Workspace, mail.protection.outlook.com means
Microsoft 365. Resolution is slow (one DNS round trip per domain) but the
answer is stable, so it is cached here once per domain and reused by every
webinar afterwards.

Revision ID: 077
Revises: 076
"""
from alembic import op

revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_domain_provider (
            domain      TEXT PRIMARY KEY,
            -- Canonical provider label ("Google Workspace", "Microsoft 365",
            -- "No mail server", "Other", …). Never NULL: an unresolvable
            -- domain is a finding, not a gap, so it gets a label of its own.
            provider    TEXT NOT NULL,
            -- Lowest-preference MX host the label was derived from, kept so a
            -- reclassification can re-run over the cache without new lookups.
            mx_host     TEXT,
            -- 'ok' | 'nxdomain' | 'no_mx' | 'timeout' | 'error'
            status      TEXT NOT NULL DEFAULT 'ok',
            resolved_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Provider rollups group by provider; the tail is long so this index earns
    # its keep on the aggregate queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_edp_provider ON email_domain_provider (provider)"
    )
    # Lets the resolver find rows worth retrying (timeouts/errors) cheaply.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_edp_status ON email_domain_provider (status) "
        "WHERE status <> 'ok'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS email_domain_provider")
