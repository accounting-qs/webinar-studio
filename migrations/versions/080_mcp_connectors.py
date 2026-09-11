"""080_mcp_connectors

Bearer tokens for the remote MCP server mounted at POST /mcp.

One row per external agent ("connector"), not a singleton — so one bot can be
revoked without knocking out the others, and last_used_at gives an honest
signal about which tokens are actually live.

- name          TEXT        — free text the admin types; the server has no way
                              to know what is on the far end of a token
- token_hash    TEXT UNIQUE — sha256 hex of the token. The plaintext is shown
                              exactly once, at generation, and never stored:
                              if any endpoint could return it, the bearer check
                              would be pointless
- token_prefix  TEXT        — first few chars, for telling rows apart in the UI
- enabled       BOOLEAN     — revoke without deleting the audit trail
- allow_writes  BOOLEAN     — per-connector permission. Read tools are always
                              allowed; the confirm-gated tools that spend money
                              or queue heavy work need this ON as a second gate
- last_used_at  TIMESTAMPTZ — stamped (throttled) on every authenticated call
- call_count    INTEGER     — cumulative authenticated calls

Revision ID: 080
Revises: 079
"""
from alembic import op


revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_connectors (
            id            UUID PRIMARY KEY,
            name          TEXT NOT NULL,
            token_hash    TEXT NOT NULL UNIQUE,
            token_prefix  TEXT NOT NULL DEFAULT '',
            enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            allow_writes  BOOLEAN NOT NULL DEFAULT FALSE,
            last_used_at  TIMESTAMPTZ,
            call_count    INTEGER NOT NULL DEFAULT 0,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Auth looks up every enabled row per call; the table is tiny but the
    # partial index keeps that a single index scan regardless.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_mcp_connectors_enabled "
        "ON mcp_connectors (enabled) WHERE enabled"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_mcp_connectors_enabled")
    op.execute("DROP TABLE IF EXISTS mcp_connectors")
