"""Upload and contact models: UploadHistory, ContactCustomField, Contact."""

from sqlalchemy import text

from db.models._common import (
    Base, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index,
    Integer, JSONB, Mapped, Optional, String, Text, UUID, UniqueConstraint,
    datetime, func, gen_uuid, mapped_column, relationship,
)


class UploadHistory(Base):
    __tablename__ = "upload_history"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[Optional[str]] = mapped_column(Text)
    field_mappings: Mapped[Optional[dict]] = mapped_column(JSONB)
    duplicate_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ignore")
    upload_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="bucket")
    custom_list_name: Mapped[Optional[str]] = mapped_column(Text)
    # List-level location (country or region) applied to every contact of this import.
    list_location: Mapped[Optional[str]] = mapped_column(Text)

    total_contacts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_buckets: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    bucket_summary: Mapped[Optional[dict]] = mapped_column(JSONB)

    # Progress tracking
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    overwritten_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'uploading', 'processing', 'complete', 'failed', 'cancelled', 'paused')", name="ck_upload_history_status"),
        Index("ix_upload_history_user_id", "user_id"),
    )


class ContactCustomField(Base):
    __tablename__ = "contact_custom_fields"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="text")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "field_name", name="uq_custom_fields_user_name"),
        CheckConstraint("field_type IN ('text', 'number', 'date', 'boolean')", name="ck_custom_fields_type"),
        Index("ix_custom_fields_user_id", "user_id"),
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    upload_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("upload_history.id", ondelete="SET NULL"))
    bucket_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("outreach_buckets.id", ondelete="SET NULL"))
    assignment_id: Mapped[Optional[str]] = mapped_column(UUID(as_uuid=False), ForeignKey("webinar_list_assignments.id", ondelete="SET NULL"))
    outreach_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="available")
    # Denormalized blocklist membership: true when this contact's email is on the
    # user's blocklist. Maintained at write time (import + every blocklist add/
    # remove) so read paths never scan the blocklist per row. Kept in sync by
    # `mark_contacts_blocklisted` in api/routers/outreach/_helpers.py.
    is_blocklisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    assigned_date: Mapped[Optional[datetime]] = mapped_column(Date)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Reusable-contacts caches, denormalized from webinar_contact_memberships and
    # maintained at write time (like is_blocklisted). Let the reuse filter and the
    # dashboards read per-contact invite history without scanning memberships.
    #   assigned_membership_count — # current 'assigned' memberships (any webinar) = in-flight guard
    #   times_invited             — # current 'used' memberships = the dashboard metric
    #   last_invited_at           — MAX(used_at) over current 'used' memberships; NULL = never invited
    assigned_membership_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    times_invited: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_invited_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Core identity
    contact_id: Mapped[Optional[str]] = mapped_column(Text)
    first_name: Mapped[Optional[str]] = mapped_column(Text)
    last_name: Mapped[Optional[str]] = mapped_column(Text)
    email: Mapped[Optional[str]] = mapped_column(Text)
    company_website: Mapped[Optional[str]] = mapped_column(Text)

    # Enrichment
    bucket_name: Mapped[Optional[str]] = mapped_column(Text)
    classification: Mapped[Optional[str]] = mapped_column(Text)

    # Source metadata
    lead_list_name: Mapped[Optional[str]] = mapped_column(Text)
    segment_name: Mapped[Optional[str]] = mapped_column(Text)
    created_date: Mapped[Optional[str]] = mapped_column(Text)
    industry: Mapped[Optional[str]] = mapped_column(Text)
    employee_range: Mapped[Optional[str]] = mapped_column(Text)
    employee_count: Mapped[Optional[int]] = mapped_column(Integer)
    country: Mapped[Optional[str]] = mapped_column(Text)
    database_provider: Mapped[Optional[str]] = mapped_column(Text)
    scraper: Mapped[Optional[str]] = mapped_column(Text)
    enrichment_classification: Mapped[Optional[str]] = mapped_column(Text)
    primary_identity: Mapped[Optional[str]] = mapped_column(Text)
    sub_identity: Mapped[Optional[str]] = mapped_column(Text)
    sector: Mapped[Optional[str]] = mapped_column(Text)

    # Person / company firmographics
    title: Mapped[Optional[str]] = mapped_column(Text)
    seniority: Mapped[Optional[str]] = mapped_column(Text)
    company_founded_year: Mapped[Optional[str]] = mapped_column(Text)
    company_total_funding: Mapped[Optional[str]] = mapped_column(Text)
    company_annual_revenue: Mapped[Optional[str]] = mapped_column(Text)
    company_country: Mapped[Optional[str]] = mapped_column(Text)

    # List-level location chosen at import (a country or a region like "Europe").
    # Kept separate from `country`; used as a fallback when country is blank.
    list_location: Mapped[Optional[str]] = mapped_column(Text)

    # Custom fields stored as JSONB
    custom_data: Mapped[Optional[dict]] = mapped_column(JSONB, server_default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    bucket = relationship("OutreachBucket")
    upload = relationship("UploadHistory")
    assignment = relationship("WebinarListAssignment")

    __table_args__ = (
        UniqueConstraint("user_id", "email", name="uq_contacts_user_email"),
        CheckConstraint("outreach_status IN ('available', 'assigned', 'used')", name="ck_contacts_outreach_status"),
        Index("ix_contacts_user_id", "user_id"),
        Index("ix_contacts_bucket_id", "bucket_id"),
        Index("ix_contacts_upload_id", "upload_id"),
        # No ix_contacts_email here: it was btree(user_id, email) — identical to
        # the uq_contacts_user_email constraint above, which already serves those
        # lookups. Dropped in migration 068.
        Index("ix_contacts_assignment_id", "assignment_id"),
        Index("ix_contacts_bucket_unassigned", "bucket_id", "assignment_id"),
        Index("ix_contacts_outreach_status", "bucket_id", "outreach_status"),
        Index("ix_contacts_upload_status_bucket", "upload_id", "outreach_status", "bucket_id"),
        # Partial indexes over only the (few) blocklisted rows so the per-bucket /
        # per-assignment blocklist counts touch just those, not every contact.
        Index("ix_contacts_bl_assignment", "assignment_id", "outreach_status",
              postgresql_where=text("is_blocklisted")),
        Index("ix_contacts_bl_bucket", "bucket_id", "outreach_status",
              postgresql_where=text("is_blocklisted")),
        # Case-insensitive email lookup — powers flag maintenance (stamping
        # contacts when an email enters/leaves the blocklist) and the backfill.
        Index("ix_contacts_lower_email", "user_id", func.lower(text("email"))),
    )
