"""Connector models: API credentials and the shared webinar-platform data cache.

webinar_broadcasts / webinar_registrants are provider-neutral: WebinarGeek and
Zoom both write into them, discriminated by the `provider` column. Everything
downstream joins on (LOWER(email), webinars.broadcast_id) and does not care
which platform produced a row.
"""

from db.models._common import (
    Base, Boolean, DateTime, ForeignKey, Index, Integer, JSONB,
    Mapped, Optional, String, Text, UniqueConstraint, UUID,
    datetime, func, gen_uuid, mapped_column,
)


class ConnectorCredential(Base):
    __tablename__ = "connector_credentials"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Names a single credential within a provider. 'default' is the row
    # used when no specific selection is made (matches pre-multi-credential
    # behavior). Variants (e.g. WebinarGeek A/B accounts) get their own
    # named row and reference it via Webinar.webinargeek_credential_id.
    name: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Used by providers that need a second piece of identity alongside the
    # API key (e.g. GHL location id). Null for single-secret providers.
    location_id: Mapped[Optional[str]] = mapped_column(Text)
    # GHL-only: pipeline used for opportunity streaming. Null elsewhere.
    pipeline_id: Mapped[Optional[str]] = mapped_column(Text)
    # Zoom-only: the non-secret half of Server-to-Server OAuth. The client
    # secret lives in api_key so masking/deletion stay provider-agnostic.
    client_id: Mapped[Optional[str]] = mapped_column(Text)
    account_id: Mapped[Optional[str]] = mapped_column(Text)
    # Skarpe-only: the MCP endpoint this key belongs to. Skarpe keys are bound
    # to one host (staging vs production backend), so the URL travels with the
    # credential. Null for providers with a hardcoded base URL.
    base_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_connector_credentials_provider_name"),
    )


class WebinarBroadcast(Base):
    __tablename__ = "webinar_broadcasts"

    broadcast_id: Mapped[str] = mapped_column(Text, primary_key=True)
    webinar_id: Mapped[Optional[str]] = mapped_column(String(64))
    # Which platform produced this row: 'webinargeek' | 'zoom'. Load-bearing —
    # the WebinarGeek picker, its sync-all count and run_sync_all all filter on
    # it so they never pick up a Zoom broadcast (and never hand one to the
    # WebinarGeek API key).
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="webinargeek")
    # Zoom only. The per-instance UUID the participants report is keyed by.
    # NULL until the webinar has actually aired — it does not exist before
    # then, which is why broadcast_id cannot carry it. Resolved once, at sync
    # time, and never re-resolved.
    platform_instance_id: Mapped[Optional[str]] = mapped_column(Text)
    # Zoom only. Parsed out of broadcast_id so SQL never has to string-split.
    occurrence_id: Mapped[Optional[str]] = mapped_column(Text)
    # Which provider credential most recently surfaced this broadcast.
    # Stamped during refresh; rows synced before migration 042 stay NULL
    # until the next refresh.
    credential_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("connector_credentials.id", ondelete="SET NULL"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    internal_title: Mapped[Optional[str]] = mapped_column(Text)
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    subscriptions_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    live_viewers_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    replay_viewers_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    has_ended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    raw: Mapped[Optional[dict]] = mapped_column(JSONB)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class WebinarRegistrant(Base):
    __tablename__ = "webinar_registrants"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    broadcast_id: Mapped[str] = mapped_column(
        Text, ForeignKey("webinar_broadcasts.broadcast_id", ondelete="CASCADE"), nullable=False,
    )
    # Redundant with the FK, but the ~20 raw-SQL sites join on broadcast_id
    # alone and provenance is otherwise invisible when reading rows directly.
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="webinargeek")
    subscriber_id: Mapped[Optional[str]] = mapped_column(String(64))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    first_name: Mapped[Optional[str]] = mapped_column(Text)
    last_name: Mapped[Optional[str]] = mapped_column(Text)
    company: Mapped[Optional[str]] = mapped_column(Text)
    job_title: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(Text)
    city: Mapped[Optional[str]] = mapped_column(Text)
    country: Mapped[Optional[str]] = mapped_column(Text)
    timezone: Mapped[Optional[str]] = mapped_column(Text)
    registration_source: Mapped[Optional[str]] = mapped_column(Text)
    subscribed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    unsubscribed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    unsubscribe_source: Mapped[Optional[str]] = mapped_column(Text)
    watched_live: Mapped[Optional[bool]] = mapped_column(Boolean)
    watched_replay: Mapped[Optional[bool]] = mapped_column(Boolean)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    minutes_viewing: Mapped[Optional[int]] = mapped_column(Integer)
    viewing_country: Mapped[Optional[str]] = mapped_column(Text)
    viewing_device: Mapped[Optional[str]] = mapped_column(Text)
    watch_link: Mapped[Optional[str]] = mapped_column(Text)
    raw: Mapped[Optional[dict]] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("broadcast_id", "email", name="uq_webinar_registrants_broadcast_email"),
        Index("ix_webinar_registrants_broadcast", "broadcast_id"),
        Index("ix_webinar_registrants_email", "email"),
    )
