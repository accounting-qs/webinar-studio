"""Per-webinar "Added to Calendar" CSV ingestion models.

WebinarCalendarUpload — one row per CSV upload (status/progress tracker).
WebinarCalendarInvite — one row per CSV record. Upsert key is
    (webinar_id, email): re-uploading the same email for the same webinar
    updates the row; the same email across different webinars yields
    independent rows. matched_assignment_id NULL = "No List Data".
CalendarAccountSender — maps (webinar_id, calendar_account) → outreach
    sender, used by the Account Health view.
"""

from db.models._common import (
    Base, Boolean, CheckConstraint, DateTime, ForeignKey, Index,
    Integer, Mapped, Optional, String, Text, UUID, UniqueConstraint,
    datetime, func, gen_uuid, mapped_column,
)


class WebinarCalendarUpload(Base):
    __tablename__ = "webinar_calendar_uploads"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    # Optional sender chosen at upload time (Pattern A); after the import
    # completes, every distinct calendar_account in this CSV gets a row in
    # calendar_account_senders pointing here.
    sender_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("outreach_senders.id", ondelete="SET NULL")
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[Optional[str]] = mapped_column(Text)
    has_responses: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # 'calendar' (normal Added-to-Calendar CSV → webinar_calendar_invites) or
    # 'nonjoiner' (Yes/Maybe responses for the auto-derived Nonjoiners cohort →
    # webinar_nonjoiner_invites). Routes parsing + destination table.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="calendar")

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    unmatched_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'uploading', 'processing', 'paused', 'complete', 'failed', 'cancelled')",
            name="ck_wcu_status",
        ),
        Index("ix_wcu_user", "user_id"),
        Index("ix_wcu_webinar", "webinar_id"),
        Index("ix_wcu_created", "created_at"),
    )


class WebinarCalendarInvite(Base):
    __tablename__ = "webinar_calendar_invites"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    upload_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinar_calendar_uploads.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)

    email: Mapped[str] = mapped_column(Text, nullable=False)
    calendar_invited_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    calendar_account: Mapped[Optional[str]] = mapped_column(Text)
    calendar_account_prefix: Mapped[Optional[str]] = mapped_column(Text)
    calendar_webinar_series: Mapped[Optional[int]] = mapped_column(Integer)
    calendar_invite_response: Mapped[Optional[str]] = mapped_column(Text)

    matched_assignment_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("webinar_list_assignments.id", ondelete="SET NULL")
    )
    matched_contact_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("contacts.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("webinar_id", "email", name="uq_wci_webinar_email"),
        Index("ix_wci_webinar_email", "webinar_id", "email"),
        Index("ix_wci_webinar_assignment", "webinar_id", "matched_assignment_id"),
        Index("ix_wci_webinar_account", "webinar_id", "calendar_account"),
        Index("ix_wci_webinar_response", "webinar_id", "calendar_invite_response"),
        Index("ix_wci_upload", "upload_id"),
    )


class WebinarNonjoinerInvite(Base):
    """One row per uploaded Non-joiners CSV record: email + calendar response.

    Separate from webinar_calendar_invites so a Non-joiners upload never feeds
    the normal calendar-CSV mode (planned-list / No-List-Data Yes/Maybe). The
    Statistics Nonjoiners row uses these rows only to LABEL Yes/Maybe on the
    auto-derived nonjoiner cohort. Upsert key (webinar_id, email): re-uploading
    the same email for the same webinar updates the row; the same email across
    different webinars yields independent rows.
    """

    __tablename__ = "webinar_nonjoiner_invites"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    upload_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinar_calendar_uploads.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)

    email: Mapped[str] = mapped_column(Text, nullable=False)
    calendar_invite_response: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("webinar_id", "email", name="uq_wnji_webinar_email"),
        Index("ix_wnji_webinar_email", "webinar_id", "email"),
        Index("ix_wnji_webinar_response", "webinar_id", "calendar_invite_response"),
        Index("ix_wnji_upload", "upload_id"),
    )


class CalendarAccountSender(Base):
    """Resolved (webinar_id, calendar_account) → outreach sender mapping.

    Written by both the CSV upload flow (Pattern A, on import completion)
    and the bulk-paste modal on Account Health (Pattern B). Re-saving a
    pair overwrites the previous sender — last write wins.
    """

    __tablename__ = "calendar_account_senders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    webinar_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False
    )
    calendar_account: Mapped[str] = mapped_column(Text, nullable=False)
    sender_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("outreach_senders.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("webinar_id", "calendar_account", name="uq_cas_webinar_account"),
        Index("ix_cas_user", "user_id"),
        Index("ix_cas_webinar", "webinar_id"),
        Index("ix_cas_sender", "sender_id"),
        Index("ix_cas_account", "calendar_account"),
    )
