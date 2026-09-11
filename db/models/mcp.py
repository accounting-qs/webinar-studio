"""MCP connector tokens — one row per external agent allowed to call POST /mcp."""

from sqlalchemy import text as sa_text

from db.models._common import (
    Base, Boolean, DateTime, Index, Integer, Mapped, Optional, Text, UUID,
    datetime, func, gen_uuid, mapped_column,
)


class McpConnector(Base):
    __tablename__ = "mcp_connectors"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    # Free text the admin types when generating the token. The server cannot
    # know what is on the far end, so this is the only label there is.
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 hex of the token. The plaintext is shown once at generation and
    # never persisted — no endpoint can return it.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # Second gate on the confirm-gated tools (report generation, recompute,
    # report email). Off by default — a fresh connector is read-only.
    allow_writes: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_mcp_connectors_enabled", "enabled", postgresql_where=sa_text("enabled")),
    )
