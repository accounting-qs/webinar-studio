"""
Pydantic schemas for the outreach API.
Shared across all outreach sub-routers.
"""

import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── Buckets ────────────────────────────────────────────────────────────────

class BucketCreate(BaseModel):
    name: str
    industry: str | None = None
    total_contacts: int = 0
    remaining_contacts: int | None = None
    countries: list[str] = []
    emp_range: str | None = None
    source_file: str | None = None

class BucketUpdate(BaseModel):
    name: str | None = None
    industry: str | None = None
    total_contacts: int | None = None
    remaining_contacts: int | None = None
    countries: list[str] | None = None
    emp_range: str | None = None


# ── Copies ─────────────────────────────────────────────────────────────────

class CopyGenerateRequest(BaseModel):
    copy_type: str = Field(..., pattern="^(title|description|both)$")
    variant_count: int = Field(3, ge=1, le=10)

class CopyCreate(BaseModel):
    copy_type: str = Field(..., pattern="^(title|description)$")
    text: str = ""

class CopyUpdate(BaseModel):
    text: str | None = None
    is_primary: bool | None = None

class CopyRegenerateRequest(BaseModel):
    feedback: str


class CopyBulkGenerateRequest(BaseModel):
    bucket_ids: list[str]
    copy_type: str = Field(..., pattern="^(title|description|both)$")
    variant_count: int = Field(3, ge=1, le=10)


class BucketMergeRequest(BaseModel):
    keeper_bucket_id: str
    source_bucket_ids: list[str]


# ── Senders ────────────────────────────────────────────────────────────────

class SenderCreate(BaseModel):
    name: str
    total_accounts: int = 0
    send_per_account: int = 50
    days_per_webinar: int = 5
    color: str | None = None

class SenderUpdate(BaseModel):
    name: str | None = None
    total_accounts: int | None = None
    send_per_account: int | None = None
    days_per_webinar: int | None = None
    color: str | None = None
    is_active: bool | None = None


# ── Webinars ───────────────────────────────────────────────────────────────

class WebinarCreate(BaseModel):
    number: int
    date: datetime.date
    # A/B variants of the same `number` distinguish themselves with a
    # free-text label (e.g. "Account A"). NULL means the webinar is the
    # sole entry for this number; only one such row is allowed per number.
    variant_label: str | None = None
    # Per-variant WebinarGeek credential. NULL → fall back to the row in
    # connector_credentials with name='default'.
    webinargeek_credential_id: str | None = None
    # Optional WebinarGeek broadcast to link at creation time. NULL → none
    # selected yet (operator can pick one later in the Edit modal).
    broadcast_id: str | None = None

class WebinarUpdate(BaseModel):
    number: int | None = None
    date: Optional[datetime.date] = None
    status: str | None = None
    broadcast_id: str | None = None
    main_title: str | None = None
    registration_link: str | None = None
    unsubscribe_link: str | None = None
    variant_label: str | None = None
    webinargeek_credential_id: str | None = None
    nonjoiner_source_webinar_id: str | None = None


# ── Assignments ────────────────────────────────────────────────────────────

class AssignRequest(BaseModel):
    bucket_id: str | None = None
    upload_id: str | None = None
    sender_id: str
    volume: int
    accounts_used: int = 0
    send_per_account: int | None = None
    days: int | None = None
    countries_override: str | None = None
    emp_range_override: str | None = None

class AssignmentUpdate(BaseModel):
    title_copy_id: str | None = None
    desc_copy_id: str | None = None
    accounts_used: int | None = None
    volume: int | None = None
    remaining: int | None = None
    list_url: str | None = None
    list_name: str | None = None
    gcal_invited: int | None = None
    is_setup: bool | None = None


# ── Uploads ────────────────────────────────────────────────────────────────

class UploadFileResponse(BaseModel):
    id: str
    file_name: str
    storage_path: str
    total_rows: int
    headers: list[str]
    preview_rows: list[list[str]]

class ImportStartCreate(BaseModel):
    field_mappings: dict[str, str]  # CSV header -> system field
    duplicate_mode: str = "ignore"  # "ignore" | "overwrite"
    upload_mode: str = "bucket"  # "bucket" | "custom_list"
    custom_list_name: str | None = None


# ── Custom Fields ──────────────────────────────────────────────────────────

class CustomFieldCreate(BaseModel):
    field_name: str
    field_type: str = "text"  # text, number, date, boolean


# ── Brain ─────────────────────────────────────────────────────────────────

class PrincipleCreate(BaseModel):
    principle_text: str
    knowledge_type: str = "copy_general"
    category: str | None = None

class PrincipleUpdate(BaseModel):
    principle_text: str | None = None
    category: str | None = None
    is_active: bool | None = None

class CaseStudyCreate(BaseModel):
    title: str
    client_name: str | None = None
    industry: str | None = None
    tags: list[str] = []
    content: str
    source_url: str | None = None
    structured: dict | None = None

class CaseStudyUpdate(BaseModel):
    title: str | None = None
    client_name: str | None = None
    industry: str | None = None
    tags: list[str] | None = None
    content: str | None = None
    is_active: bool | None = None
    source_url: str | None = None
    structured: dict | None = None

class CaseStudyImportRequest(BaseModel):
    url: str
    notes: str | None = None

class BrainContentUpdate(BaseModel):
    brain_content: str
