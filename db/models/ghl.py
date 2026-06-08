"""GoHighLevel sync models: contacts, opportunities, sync runs, settings."""

from sqlalchemy import Numeric
from db.models._common import (
    Base, Boolean, Date, DateTime, Index, Integer, JSONB,
    Mapped, Optional, String, Text, UUID,
    datetime, func, gen_uuid, mapped_column,
)


class GHLContact(Base):
    __tablename__ = "ghl_contact"

    ghl_contact_id: Mapped[str] = mapped_column(Text, primary_key=True)
    email: Mapped[Optional[str]] = mapped_column(Text)
    date_added: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Free-text fields parsed at read time for per-webinar counts
    calendar_invite_response_history: Mapped[Optional[str]] = mapped_column(Text)
    calendar_webinar_series_history: Mapped[Optional[str]] = mapped_column(Text)
    calendar_webinar_series_non_joiners: Mapped[Optional[str]] = mapped_column(Text)

    is_booked_call: Mapped[Optional[str]] = mapped_column(Text)
    booked_call_webinar_series: Mapped[Optional[int]] = mapped_column(Integer)
    webinar_registration_in_form_date: Mapped[Optional[datetime]] = mapped_column(Date)
    cold_calendar_unsubscribe_date: Mapped[Optional[datetime]] = mapped_column(Date)
    has_sms_click_tag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # --- Fallback / auxiliary fields (migration 026) ---
    calendar_invite_response_prefix: Mapped[Optional[str]] = mapped_column(Text)
    calendar_invite_response_prefix_non_joiners: Mapped[Optional[str]] = mapped_column(Text)
    webinar_registration_number: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_webinar_series_latest: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_webinar_series_registered_total_count: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_webinar_series_attended_total_count: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_time_in_session_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_viewing_time_in_minutes_total: Mapped[Optional[int]] = mapped_column(Integer)
    zoom_attended: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_source: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_medium: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_name: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_content: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_term: Mapped[Optional[str]] = mapped_column(Text)
    book_campaign_id: Mapped[Optional[str]] = mapped_column(Text)
    registration_campaign_source: Mapped[Optional[str]] = mapped_column(Text)
    registration_campaign_medium: Mapped[Optional[str]] = mapped_column(Text)
    registration_campaign_name: Mapped[Optional[str]] = mapped_column(Text)

    tags: Mapped[Optional[list]] = mapped_column(JSONB)
    raw_custom_fields: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at_ghl: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at_ghl: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_ghl_contact_email", "email"),
        Index("ix_ghl_contact_booked_series", "booked_call_webinar_series"),
        Index("ix_ghl_contact_self_reg_date", "webinar_registration_in_form_date"),
        Index("ix_ghl_contact_unsub_date", "cold_calendar_unsubscribe_date"),
    )


class GHLOpportunity(Base):
    __tablename__ = "ghl_opportunity"

    ghl_opportunity_id: Mapped[str] = mapped_column(Text, primary_key=True)
    ghl_contact_id: Mapped[Optional[str]] = mapped_column(Text)
    pipeline_stage_id: Mapped[Optional[str]] = mapped_column(Text)
    monetary_value: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))

    call1_appointment_status: Mapped[Optional[str]] = mapped_column(Text)
    call1_appointment_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    call1_booking_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Opportunity owner (Sales Rep): GHL `assignedTo` user id + resolved name
    assigned_to_id: Mapped[Optional[str]] = mapped_column(Text)
    owner_name: Mapped[Optional[str]] = mapped_column(Text)

    webinar_source_number: Mapped[Optional[int]] = mapped_column(Integer)
    lead_quality: Mapped[Optional[str]] = mapped_column(Text)
    projected_deal_size_option: Mapped[Optional[str]] = mapped_column(Text)
    projected_deal_size_value: Mapped[Optional[int]] = mapped_column(Integer)

    raw_custom_fields: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at_ghl: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at_ghl: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_ghl_opp_webinar", "webinar_source_number"),
        Index("ix_ghl_opp_contact", "ghl_contact_id"),
        Index("ix_ghl_opp_stage", "pipeline_stage_id"),
        Index("ix_ghl_opp_lead_quality", "lead_quality"),
    )


class GHLSyncRun(Base):
    __tablename__ = "ghl_sync_run"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    sync_type: Mapped[str] = mapped_column(String(32), nullable=False)  # 'full' | 'incremental'
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, server_default="scheduled")  # 'scheduled' | 'manual'
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    contacts_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    opportunities_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    expected_total: Mapped[Optional[int]] = mapped_column(Integer)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_details: Mapped[Optional[list]] = mapped_column(JSONB)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_ghl_sync_run_started", started_at.desc()),
        Index("ix_ghl_sync_run_status", "status"),
    )


class GHLWebinarStats(Base):
    __tablename__ = "ghl_webinar_stats"

    webinar_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    gcal_invited_count: Mapped[Optional[int]] = mapped_column(Integer)
    nj_count: Mapped[Optional[int]] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GHLSyncSettings(Base):
    __tablename__ = "ghl_sync_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1 (singleton)
    incremental_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    incremental_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    weekly_full_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    weekly_full_day_of_week: Mapped[str] = mapped_column(String(3), nullable=False, server_default="wed")
    weekly_full_hour_local: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4")
    weekly_full_timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="America/Chicago")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
