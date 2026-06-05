"""Outreach campaign planning models: OutreachBucket, BucketCopy, OutreachSender, Webinar, WebinarListAssignment, CopyUsageLog."""

from db.models._common import (
    Base, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, JSONB, Mapped, Optional, String, Text, UUID, UniqueConstraint,
    datetime, func, gen_uuid, mapped_column, relationship,
)


class OutreachBucket(Base):
    __tablename__ = "outreach_buckets"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    industry: Mapped[Optional[str]] = mapped_column(Text)
    total_contacts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    remaining_contacts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    countries: Mapped[Optional[dict]] = mapped_column(JSONB, server_default="[]")
    emp_range: Mapped[Optional[str]] = mapped_column(Text)
    source_file: Mapped[Optional[str]] = mapped_column(Text)
    merged_into_bucket_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False), ForeignKey("outreach_buckets.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    copies: Mapped[list["BucketCopy"]] = relationship(back_populates="bucket", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_outreach_buckets_user_name"),
        Index("ix_outreach_buckets_user_id", "user_id"),
    )


class BucketCopy(Base):
    __tablename__ = "bucket_copies"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    bucket_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("outreach_buckets.id", ondelete="CASCADE"))
    upload_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("upload_history.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    copy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    variant_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    primary_picked_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ai_feedback: Mapped[Optional[str]] = mapped_column(Text)
    generation_batch_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    bucket: Mapped[Optional["OutreachBucket"]] = relationship(back_populates="copies")

    __table_args__ = (
        CheckConstraint("copy_type IN ('title', 'description')", name="ck_bucket_copies_type"),
        Index("ix_bucket_copies_bucket_type", "bucket_id", "copy_type"),
        Index("ix_bucket_copies_upload_id", "upload_id"),
        Index("ix_bucket_copies_user_id", "user_id"),
        Index("ix_bucket_copies_batch", "generation_batch_id"),
    )


class OutreachSender(Base):
    __tablename__ = "outreach_senders"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    total_accounts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    send_per_account: Mapped[int] = mapped_column(Integer, nullable=False, server_default="50")
    days_per_webinar: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    color: Mapped[Optional[str]] = mapped_column(String(20))
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_outreach_senders_user_name"),
        Index("ix_outreach_senders_user_id", "user_id"),
    )


class Webinar(Base):
    __tablename__ = "webinars"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free-text label that distinguishes A/B variants of the same number
    # (e.g. "Account A", "WG-Skarpe"). NULL means "single, non-variant
    # webinar"; the partial unique index allows at most one such row per
    # (user_id, number). When NOT NULL, (user_id, number, variant_label)
    # is unique.
    variant_label: Mapped[Optional[str]] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="planning")
    broadcast_id: Mapped[Optional[str]] = mapped_column(Text)
    # Which WebinarGeek credential to use for this variant's broadcast.
    # NULL → fall back to the credential named 'default' in
    # connector_credentials (preserves pre-multi-credential behavior).
    webinargeek_credential_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("connector_credentials.id", ondelete="SET NULL"),
    )
    # One-shot stamp: set once the scheduler has auto-synced this webinar's
    # WebinarGeek broadcast subscribers (fires 2h after the broadcast's start
    # time). NULL → not yet auto-synced. See services/wg_sync.py.
    broadcast_auto_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Optional link to the PREVIOUS webinar whose WebinarGeek broadcast supplies
    # this webinar's Nonjoiners (that broadcast's registrants who did NOT watch
    # live). NULL → fall back to the GHL-based nonjoiner computation.
    nonjoiner_source_webinar_id: Mapped[Optional[str]] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("webinars.id", ondelete="SET NULL"),
    )
    main_title: Mapped[Optional[str]] = mapped_column(Text)
    registration_link: Mapped[Optional[str]] = mapped_column(Text)
    unsubscribe_link: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    assignments: Mapped[list["WebinarListAssignment"]] = relationship(back_populates="webinar", lazy="selectin")

    # The two unique partial indexes below are managed in migration 034 via
    # raw SQL because Alembic/SQLAlchemy don't have a clean cross-DB way to
    # express partial-unique-on-NULL semantics. They are documented here
    # but not declared in __table_args__:
    #   uq_webinars_user_number_no_variant: UNIQUE (user_id, number)
    #     WHERE variant_label IS NULL
    #   uq_webinars_user_number_variant:    UNIQUE (user_id, number, variant_label)
    #     WHERE variant_label IS NOT NULL
    __table_args__ = (
        CheckConstraint("status IN ('planning', 'sent', 'archived')", name="ck_webinars_status"),
        Index("ix_webinars_user_id", "user_id"),
        Index("ix_webinars_status", "status"),
        Index("ix_webinars_wg_credential", "webinargeek_credential_id"),
        Index("ix_webinars_nonjoiner_source", "nonjoiner_source_webinar_id"),
    )


class WebinarListAssignment(Base):
    __tablename__ = "webinar_list_assignments"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    bucket_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("outreach_buckets.id", ondelete="SET NULL"))
    sender_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("outreach_senders.id", ondelete="SET NULL"))
    description: Mapped[Optional[str]] = mapped_column(Text)
    list_url: Mapped[Optional[str]] = mapped_column(Text)
    volume: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    remaining: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    gcal_invited: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    accounts_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    send_per_account: Mapped[Optional[int]] = mapped_column(Integer)
    days: Mapped[Optional[int]] = mapped_column(Integer)
    title_copy_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("bucket_copies.id", ondelete="SET NULL"))
    desc_copy_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("bucket_copies.id", ondelete="SET NULL"))
    countries_override: Mapped[Optional[str]] = mapped_column(Text)
    emp_range_override: Mapped[Optional[str]] = mapped_column(Text)
    is_nonjoiners: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_no_list_data: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    is_setup: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    source_upload_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("upload_history.id", ondelete="SET NULL"))
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="bucket")
    list_name: Mapped[Optional[str]] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    webinar: Mapped["Webinar"] = relationship(back_populates="assignments")
    bucket: Mapped[Optional["OutreachBucket"]] = relationship()
    sender: Mapped[Optional["OutreachSender"]] = relationship()
    title_copy: Mapped[Optional["BucketCopy"]] = relationship(foreign_keys=[title_copy_id])
    desc_copy: Mapped[Optional["BucketCopy"]] = relationship(foreign_keys=[desc_copy_id])

    __table_args__ = (
        Index("ix_wla_webinar_id", "webinar_id"),
        Index("ix_wla_bucket_id", "bucket_id"),
        Index("ix_wla_sender_id", "sender_id"),
        Index("ix_wla_webinar_sender", "webinar_id", "sender_id"),
        Index("ix_wla_user_id", "user_id"),
        Index("ix_wla_source_upload_id", "source_upload_id"),
    )


class CopyUsageLog(Base):
    __tablename__ = "copy_usage_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    bucket_copy_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("bucket_copies.id", ondelete="CASCADE"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinar_list_assignments.id", ondelete="CASCADE"), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_copy_usage_log_copy", "bucket_copy_id"),
        Index("ix_copy_usage_log_assignment", "assignment_id"),
    )


class BucketCopyGenerationJob(Base):
    """Tracks async copy-generation work so it survives browser navigation.

    One job row per (bucket, copy_type). Frontend polls status instead of
    awaiting a long HTTP call.
    """
    __tablename__ = "bucket_copy_generation_jobs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    bucket_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("outreach_buckets.id", ondelete="CASCADE"), nullable=False)
    copy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    variant_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("copy_type IN ('title', 'description')", name="ck_copy_gen_jobs_type"),
        CheckConstraint("status IN ('pending', 'generating', 'done', 'failed')", name="ck_copy_gen_jobs_status"),
        Index("ix_copy_gen_jobs_user", "user_id"),
        Index("ix_copy_gen_jobs_bucket_type", "bucket_id", "copy_type"),
        Index("ix_copy_gen_jobs_status", "status"),
    )


class ContactReleaseLog(Base):
    """Audit row for one contact released back to the bucket pool after a webinar.

    A single uploaded CSV creates many rows sharing one `release_batch_id` so
    a future "undo this release" feature can revert an entire batch at once.
    `released_by` is nullable until the auth layer lands.
    """
    __tablename__ = "contact_release_log"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    release_batch_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    released_by: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="SET NULL"))
    contact_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("contacts.id", ondelete="SET NULL"))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    prior_status: Mapped[str] = mapped_column(String(20), nullable=False)
    prior_assignment_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))
    prior_bucket_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False))
    prior_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("prior_status IN ('assigned', 'used')", name="ck_release_log_prior_status"),
        Index("ix_release_log_user", "user_id"),
        Index("ix_release_log_webinar", "webinar_id"),
        Index("ix_release_log_batch", "release_batch_id"),
        Index("ix_release_log_email", "user_id", "email"),
    )


class WebinarListExportJob(Base):
    """Tracks async CSV export of a webinar's assigned-lists contacts.

    One row per export run. The CSV is stored inline on the row so the job
    survives browser navigation: the frontend polls status, then downloads.
    """
    __tablename__ = "webinar_list_export_jobs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    webinar_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("webinars.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    contact_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    csv_content: Mapped[Optional[str]] = mapped_column(Text, deferred=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'ready', 'failed')", name="ck_wle_jobs_status"),
        Index("ix_wle_jobs_user", "user_id"),
        Index("ix_wle_jobs_webinar", "webinar_id"),
        Index("ix_wle_jobs_status", "status"),
    )
