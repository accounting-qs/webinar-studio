"""Outreach sub-router: Webinars + Assignments CRUD + Account tracking."""
import asyncio
import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, undefer

from api.auth import require_auth
from api.routers.outreach._helpers import (
    LLOYD_USER_ID, _blocklist_email_subquery, assignment_dict,
    compute_blocklist_counts_per_assignment, copy_dict, webinar_dict,
)
from api.schemas import WebinarCreate, WebinarUpdate, AssignRequest, AssignmentUpdate
from db.models import (
    OutreachBucket, OutreachSender, Webinar, WebinarListAssignment, CopyUsageLog,
    Contact, WebinarListExportJob,
)
from db.session import AsyncSessionLocal, get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Keep references so detached background tasks aren't garbage-collected
_active_export_tasks: dict[str, asyncio.Task] = {}


# ═══════════════════════════════════════════════════════════════════════════
# WEBINARS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/webinars")
async def list_webinars(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(Webinar).where(Webinar.user_id == LLOYD_USER_ID)
        .options(selectinload(Webinar.assignments))
        .order_by(Webinar.number.desc())
    )
    webinars = result.scalars().all()
    return {"webinars": [webinar_dict(w) for w in webinars]}


@router.post("/webinars", status_code=201)
async def create_webinar(
    body: WebinarCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    # Conflict rules mirror the partial unique indexes:
    #   1. At most one row per (user_id, number) with NULL variant_label
    #   2. (user_id, number, variant_label) must be unique when label is set
    variant_label = (body.variant_label or "").strip() or None
    existing = await db.execute(
        select(Webinar).where(
            Webinar.user_id == LLOYD_USER_ID,
            Webinar.number == body.number,
        )
    )
    siblings = list(existing.scalars().all())
    if variant_label is None:
        if siblings:
            # Either there's already a non-variant row, or there are
            # variant rows. In both cases we'd violate uniqueness — operator
            # must give this one a label too.
            raise HTTPException(
                409,
                f"Webinar number {body.number} already exists. "
                "Provide a variant_label to add a parallel variant.",
            )
    else:
        # Cannot mix labeled and unlabeled rows for the same number — if
        # an unlabeled row exists, the operator should label it first.
        if any(s.variant_label is None for s in siblings):
            raise HTTPException(
                409,
                f"Webinar number {body.number} already exists without a variant label. "
                "Label the existing webinar before adding a variant.",
            )
        if any(s.variant_label == variant_label for s in siblings):
            raise HTTPException(
                409,
                f"Webinar number {body.number} variant '{variant_label}' already exists.",
            )

    # Default registration_link and unsubscribe_link from the latest webinar
    latest_result = await db.execute(
        select(Webinar).where(Webinar.user_id == LLOYD_USER_ID)
        .order_by(Webinar.number.desc())
        .limit(1)
    )
    latest = latest_result.scalar_one_or_none()

    webinar = Webinar(
        user_id=LLOYD_USER_ID,
        number=body.number,
        variant_label=variant_label,
        webinargeek_credential_id=body.webinargeek_credential_id,
        date=body.date,
        status="planning",
        registration_link=latest.registration_link if latest else None,
        unsubscribe_link=latest.unsubscribe_link if latest else None,
    )
    db.add(webinar)
    await db.flush()
    await db.refresh(webinar)
    return webinar_dict(webinar)


@router.put("/webinars/{webinar_id}")
async def update_webinar(
    webinar_id: str,
    body: WebinarUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(Webinar).where(Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID)
    )
    webinar = result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")
    fields = body.model_dump(exclude_unset=True)
    for field, val in fields.items():
        setattr(webinar, field, val)
    await db.flush()
    # Drop the per-webinar stats cache when an edit changes anything the
    # Statistics page derives (nonjoiner source, broadcast, identity/date) so
    # the dashboard reflects it immediately instead of after the 10-min TTL.
    if {"nonjoiner_source_webinar_id", "broadcast_id", "number", "variant_label", "date"} & fields.keys():
        from services.statistics import invalidate_stats_cache
        invalidate_stats_cache()
    return webinar_dict(webinar)


@router.delete("/webinars/{webinar_id}")
async def delete_webinar(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(Webinar).where(Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID)
        .options(selectinload(Webinar.assignments))
    )
    webinar = result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")

    # Release 'assigned' contacts back to 'available' for all assignments
    assignment_ids = [a.id for a in (webinar.assignments or [])]
    total_released = 0
    if assignment_ids:
        release_result = await db.execute(
            update(Contact)
            .where(Contact.assignment_id.in_(assignment_ids), Contact.outreach_status == "assigned")
            .values(assignment_id=None, outreach_status="available", assigned_date=None)
        )
        total_released = release_result.rowcount

        # Clear assignment_id on 'used' contacts (keep status as 'used')
        await db.execute(
            update(Contact)
            .where(Contact.assignment_id.in_(assignment_ids), Contact.outreach_status == "used")
            .values(assignment_id=None)
        )

    # Restore bucket remaining counts
    if total_released > 0:
        # Group released by bucket
        for a in (webinar.assignments or []):
            if a.bucket_id:
                b_result = await db.execute(
                    select(sa_func.count()).where(
                        Contact.bucket_id == a.bucket_id,
                        Contact.outreach_status == "available",
                    )
                )
                actual_available = b_result.scalar() or 0
                await db.execute(
                    update(OutreachBucket)
                    .where(OutreachBucket.id == a.bucket_id)
                    .values(remaining_contacts=actual_available)
                )

    # Delete copy usage logs for all assignments
    if assignment_ids:
        await db.execute(
            delete(CopyUsageLog).where(CopyUsageLog.assignment_id.in_(assignment_ids))
        )

    # Delete webinar (CASCADE will delete assignments)
    await db.delete(webinar)
    await db.flush()

    return {"deleted": True, "released": total_released}


# ═══════════════════════════════════════════════════════════════════════════
# WEBINAR ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/webinars/{webinar_id}/lists")
async def get_webinar_lists(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(WebinarListAssignment).where(
            WebinarListAssignment.webinar_id == webinar_id,
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
        .options(
            selectinload(WebinarListAssignment.bucket),
            selectinload(WebinarListAssignment.sender),
            selectinload(WebinarListAssignment.title_copy),
            selectinload(WebinarListAssignment.desc_copy),
        )
        .order_by(WebinarListAssignment.display_order, WebinarListAssignment.created_at)
    )
    assignments = result.scalars().all()
    bl_counts = await compute_blocklist_counts_per_assignment(
        db, [a.id for a in assignments]
    )
    return {
        "assignments": [
            assignment_dict(a, blocklist_counts=bl_counts.get(a.id)) for a in assignments
        ]
    }


@router.post("/webinars/{webinar_id}/assign", status_code=201)
async def assign_bucket(
    webinar_id: str,
    body: AssignRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    # Validate: exactly one source must be provided
    if not body.bucket_id and not body.upload_id:
        raise HTTPException(400, "Either bucket_id or upload_id must be provided")
    if body.bucket_id and body.upload_id:
        raise HTTPException(400, "Cannot provide both bucket_id and upload_id")

    is_custom_list = body.upload_id is not None

    # Validate webinar
    w_result = await db.execute(
        select(Webinar).where(Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID)
    )
    webinar = w_result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")

    # Validate sender
    s_result = await db.execute(
        select(OutreachSender).where(OutreachSender.id == body.sender_id, OutreachSender.user_id == LLOYD_USER_ID)
    )
    sender = s_result.scalar_one_or_none()
    if not sender:
        raise HTTPException(404, "Sender not found")

    bucket = None
    title_copy = None
    desc_copy = None
    upload = None

    blocklist_sq = _blocklist_email_subquery()
    not_blocklisted = sa_func.lower(Contact.email).notin_(blocklist_sq)

    if is_custom_list:
        # Validate custom list upload
        from db.models import UploadHistory
        u_result = await db.execute(
            select(UploadHistory).where(
                UploadHistory.id == body.upload_id,
                UploadHistory.user_id == LLOYD_USER_ID,
                UploadHistory.upload_mode == "custom_list",
                UploadHistory.status == "complete",
            )
        )
        upload = u_result.scalar_one_or_none()
        if not upload:
            raise HTTPException(404, "Custom list not found or not complete")

        # Count available (non-blocklisted) contacts from this upload
        available_count_result = await db.execute(
            select(sa_func.count()).where(
                Contact.upload_id == body.upload_id,
                Contact.bucket_id.is_(None),
                Contact.outreach_status == "available",
                not_blocklisted,
            )
        )
        available_count = available_count_result.scalar() or 0
        desc_str = upload.custom_list_name or upload.file_name

        # Copies for this custom list (by upload_id). Prefer primary; fall
        # back to the lowest variant_index so every assignment has a default
        # selection the user can override via the Variations modal.
        from db.models import BucketCopy
        copies_result = await db.execute(
            select(BucketCopy).where(
                BucketCopy.upload_id == body.upload_id,
                BucketCopy.deleted_at.is_(None),
            )
        )
        all_copies = list(copies_result.scalars())
        for copy_type in ("title", "description"):
            candidates = [c for c in all_copies if c.copy_type == copy_type]
            if not candidates:
                continue
            chosen = next((c for c in candidates if c.is_primary), None) \
                or min(candidates, key=lambda c: (c.variant_index, c.created_at))
            if copy_type == "title":
                title_copy = chosen
            else:
                desc_copy = chosen
    else:
        # Validate bucket
        b_result = await db.execute(
            select(OutreachBucket).where(OutreachBucket.id == body.bucket_id, OutreachBucket.user_id == LLOYD_USER_ID)
            .options(selectinload(OutreachBucket.copies))
        )
        bucket = b_result.scalar_one_or_none()
        if not bucket:
            raise HTTPException(404, "Bucket not found")

        # Count available (non-blocklisted) contacts in this bucket
        available_count_result = await db.execute(
            select(sa_func.count()).where(
                Contact.bucket_id == body.bucket_id,
                Contact.outreach_status == "available",
                not_blocklisted,
            )
        )
        available_count = available_count_result.scalar() or 0

        # Prefer the primary copy; fall back to any non-deleted copy
        # (lowest variant_index) so every assignment has a default selection
        # the user can override via the Variations modal's "Pick for list".
        def _pick_copy(copy_type: str):
            candidates = [c for c in (bucket.copies or []) if c.copy_type == copy_type and not c.deleted_at]
            if not candidates:
                return None
            primary = next((c for c in candidates if c.is_primary), None)
            if primary:
                return primary
            return min(candidates, key=lambda c: (c.variant_index, c.created_at))
        title_copy = _pick_copy("title")
        desc_copy = _pick_copy("description")

        countries = body.countries_override or ", ".join(bucket.countries or [])
        emp = body.emp_range_override or bucket.emp_range or ""
        desc_str = f"{bucket.name}, {emp} emp, {countries}"

    if available_count < body.volume:
        raise HTTPException(
            400,
            f"Volume {body.volume} exceeds available contacts ({available_count})",
        )

    # Get next display order
    max_order_result = await db.execute(
        select(sa_func.max(WebinarListAssignment.display_order)).where(
            WebinarListAssignment.webinar_id == webinar_id
        )
    )
    next_order = (max_order_result.scalar() or 0) + 1

    # Create assignment
    assignment = WebinarListAssignment(
        user_id=LLOYD_USER_ID,
        webinar_id=webinar_id,
        bucket_id=body.bucket_id if not is_custom_list else None,
        sender_id=body.sender_id,
        description=desc_str,
        volume=body.volume,
        remaining=body.volume,
        accounts_used=body.accounts_used,
        send_per_account=body.send_per_account,
        days=body.days,
        title_copy_id=title_copy.id if title_copy else None,
        desc_copy_id=desc_copy.id if desc_copy else None,
        countries_override=body.countries_override,
        emp_range_override=body.emp_range_override,
        source_type="custom_list" if is_custom_list else "bucket",
        source_upload_id=body.upload_id if is_custom_list else None,
        list_name=(upload.custom_list_name or upload.file_name) if is_custom_list else None,
        display_order=next_order,
    )
    db.add(assignment)
    await db.flush()  # get assignment.id

    # Claim available contacts — excluding blocklisted
    if is_custom_list:
        claim_subq = (
            select(Contact.id)
            .where(
                Contact.upload_id == body.upload_id,
                Contact.bucket_id.is_(None),
                Contact.outreach_status == "available",
                not_blocklisted,
            )
            .limit(body.volume)
        )
    else:
        claim_subq = (
            select(Contact.id)
            .where(
                Contact.bucket_id == body.bucket_id,
                Contact.outreach_status == "available",
                not_blocklisted,
            )
            .limit(body.volume)
        )

    # Re-check `outreach_status == 'available'` on the outer UPDATE so PostgreSQL's
    # EvalPlanQual re-evaluates against the row's current state under READ COMMITTED.
    # Without this predicate, a concurrent assign request (double-click race) would
    # silently overwrite the first request's assignment_id on the same rows once it
    # acquired the row locks — leaving the first assignment with volume>0 but zero
    # contacts attached. See https://www.postgresql.org/docs/current/transaction-iso.html
    claim_result = await db.execute(
        update(Contact)
        .where(
            Contact.id.in_(claim_subq),
            Contact.outreach_status == "available",
        )
        .values(
            assignment_id=assignment.id,
            outreach_status="assigned",
            assigned_date=webinar.date,
        )
    )
    claimed = claim_result.rowcount

    # Reconcile assignment.volume / .remaining with what was actually claimed.
    # The pre-claim available_count check (line ~328) can race against a
    # concurrent assignment, leaving fewer rows for this UPDATE. Without this
    # reconciliation the assignment carries a phantom volume that the
    # Planning table and contacts page have no way to reconcile.
    if claimed == 0 and body.volume > 0:
        await db.delete(assignment)
        await db.flush()
        source = "list" if is_custom_list else "bucket"
        raise HTTPException(
            409,
            f"No available contacts left in this {source} — they were claimed by another assignment.",
        )
    if claimed != body.volume:
        assignment.volume = claimed
        assignment.remaining = claimed
        await db.flush()

    # Update bucket remaining counter (only for bucket assignments)
    if bucket:
        bucket.remaining_contacts = max(0, available_count - claimed)

    # Log copy usage
    if title_copy:
        db.add(CopyUsageLog(bucket_copy_id=title_copy.id, assignment_id=assignment.id))
    if desc_copy:
        db.add(CopyUsageLog(bucket_copy_id=desc_copy.id, assignment_id=assignment.id))

    await db.flush()

    # Reload with relationships
    await db.refresh(assignment)
    reload_result = await db.execute(
        select(WebinarListAssignment).where(WebinarListAssignment.id == assignment.id)
        .options(
            selectinload(WebinarListAssignment.bucket),
            selectinload(WebinarListAssignment.sender),
            selectinload(WebinarListAssignment.title_copy),
            selectinload(WebinarListAssignment.desc_copy),
        )
    )
    assignment = reload_result.scalar_one()
    bl = await compute_blocklist_counts_per_assignment(db, [assignment.id])
    resp = assignment_dict(assignment, blocklist_counts=bl.get(assignment.id))
    resp["bucket_remaining"] = bucket.remaining_contacts if bucket else None
    return resp


@router.put("/assignments/{assignment_id}")
async def update_assignment(
    assignment_id: str,
    body: AssignmentUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(WebinarListAssignment).where(
            WebinarListAssignment.id == assignment_id,
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(assignment, field, val)
    await db.flush()

    # Reload with relationships so assignment_dict can serialize them
    result = await db.execute(
        select(WebinarListAssignment).where(WebinarListAssignment.id == assignment_id)
        .options(
            selectinload(WebinarListAssignment.bucket),
            selectinload(WebinarListAssignment.sender),
            selectinload(WebinarListAssignment.title_copy),
            selectinload(WebinarListAssignment.desc_copy),
        )
    )
    assignment = result.scalar_one()
    bl = await compute_blocklist_counts_per_assignment(db, [assignment.id])
    return assignment_dict(assignment, blocklist_counts=bl.get(assignment.id))


@router.delete("/assignments/{assignment_id}")
async def delete_assignment(
    assignment_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(WebinarListAssignment).where(
            WebinarListAssignment.id == assignment_id,
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found")

    # Release 'assigned' contacts back to 'available' (not 'used' — those stay used)
    release_result = await db.execute(
        update(Contact)
        .where(Contact.assignment_id == assignment_id, Contact.outreach_status == "assigned")
        .values(assignment_id=None, outreach_status="available", assigned_date=None)
    )
    released = release_result.rowcount

    # Clear assignment_id on 'used' contacts too (assignment is being deleted)
    # but keep their outreach_status as 'used'
    await db.execute(
        update(Contact)
        .where(Contact.assignment_id == assignment_id, Contact.outreach_status == "used")
        .values(assignment_id=None)
    )

    # Restore bucket remaining — only the released (previously assigned, not yet used) ones
    bucket_id = assignment.bucket_id
    bucket_remaining = None
    if bucket_id and released > 0:
        b_result = await db.execute(select(OutreachBucket).where(OutreachBucket.id == bucket_id))
        bucket = b_result.scalar_one_or_none()
        if bucket:
            bucket.remaining_contacts += released
            bucket_remaining = bucket.remaining_contacts

    # Delete usage logs + assignment in parallel deletes
    await db.execute(
        delete(CopyUsageLog).where(CopyUsageLog.assignment_id == assignment_id)
    )

    await db.delete(assignment)
    await db.flush()

    return {
        "released": released,
        "bucket_id": bucket_id,
        "bucket_remaining": bucket_remaining,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNT TRACKING
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/webinars/{webinar_id}/accounts")
async def get_webinar_accounts(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    senders_result = await db.execute(
        select(OutreachSender).where(
            OutreachSender.user_id == LLOYD_USER_ID,
            OutreachSender.is_active.is_(True),
        )
    )
    senders = senders_result.scalars().all()

    usage_result = await db.execute(
        select(
            WebinarListAssignment.sender_id,
            sa_func.coalesce(sa_func.sum(WebinarListAssignment.accounts_used), 0).label("used"),
        ).where(
            WebinarListAssignment.webinar_id == webinar_id,
        ).group_by(WebinarListAssignment.sender_id)
    )
    usage_map = {row.sender_id: row.used for row in usage_result}

    return {
        "senders": [
            {
                "sender_id": s.id,
                "sender_name": s.name,
                "total_accounts": s.total_accounts,
                "accounts_used": usage_map.get(s.id, 0),
                "accounts_available": s.total_accounts - usage_map.get(s.id, 0),
            }
            for s in senders
        ]
    }


# ═══════════════════════════════════════════════════════════════════════════
# ASSIGNMENT CONTACTS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/assignments/{assignment_id}/contacts")
async def get_assignment_contacts(
    assignment_id: str,
    status: str = Query("assigned", regex="^(assigned|used|all)$"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    # Verify assignment belongs to user
    asgn_result = await db.execute(
        select(WebinarListAssignment)
        .where(WebinarListAssignment.id == assignment_id, WebinarListAssignment.user_id == LLOYD_USER_ID)
        .options(selectinload(WebinarListAssignment.bucket), selectinload(WebinarListAssignment.webinar))
    )
    assignment = asgn_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found")

    not_blocklisted = sa_func.lower(Contact.email).notin_(_blocklist_email_subquery())

    q = select(Contact).where(
        Contact.assignment_id == assignment_id,
        Contact.user_id == LLOYD_USER_ID,
        not_blocklisted,
    )
    if status != "all":
        q = q.where(Contact.outreach_status == status)
    q = q.order_by(Contact.first_name, Contact.email)

    result = await db.execute(q)
    contacts = result.scalars().all()

    # Count by status for the filter badges — excludes blocklisted
    count_result = await db.execute(
        select(Contact.outreach_status, sa_func.count()).where(
            Contact.assignment_id == assignment_id,
            Contact.user_id == LLOYD_USER_ID,
            not_blocklisted,
        ).group_by(Contact.outreach_status)
    )
    counts = {row[0]: row[1] for row in count_result}

    bl_counts = await compute_blocklist_counts_per_assignment(db, [assignment_id])
    blocklisted_total = bl_counts.get(assignment_id, {}).get("total", 0)

    # Header total mirrors the All tab — assigned + used rows actually
    # attached to this assignment, post-blocklist. The query above already
    # filters by assignment_id and excludes blocklisted contacts.
    attached_total = counts.get("assigned", 0) + counts.get("used", 0)

    return {
        "assignment": {
            "id": assignment.id,
            "bucket_name": assignment.bucket.name if assignment.bucket else None,
            "list_name": assignment.list_name,
            "webinar_number": assignment.webinar.number if assignment.webinar else None,
            "webinar_date": assignment.webinar.date.isoformat() if assignment.webinar and assignment.webinar.date else None,
            "volume": attached_total,
            "volume_raw": assignment.volume,
            "blocklisted_total": blocklisted_total,
        },
        "contacts": [
            {
                "id": c.id,
                "email": c.email,
                "first_name": c.first_name,
                "outreach_status": c.outreach_status,
                "used_at": c.used_at.isoformat() if c.used_at else None,
            }
            for c in contacts
        ],
        "counts": {
            "assigned": counts.get("assigned", 0),
            "used": counts.get("used", 0),
            "total": sum(counts.values()),
        },
    }


class MarkUsedRequest(BaseModel):
    contact_ids: list[str]


@router.put("/assignments/{assignment_id}/contacts/mark-used")
async def mark_contacts_used(
    assignment_id: str,
    body: MarkUsedRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    # Verify assignment belongs to user
    asgn_result = await db.execute(
        select(WebinarListAssignment).where(
            WebinarListAssignment.id == assignment_id,
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
    )
    assignment = asgn_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(404, "Assignment not found")

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Contact)
        .where(
            Contact.id.in_(body.contact_ids),
            Contact.assignment_id == assignment_id,
            Contact.user_id == LLOYD_USER_ID,
            Contact.outreach_status == "assigned",
        )
        .values(outreach_status="used", used_at=now)
    )
    marked = result.rowcount or 0
    if marked:
        assignment.remaining = max(0, (assignment.remaining or 0) - marked)
    await db.flush()

    return {"marked": marked, "remaining": assignment.remaining}


# ═══════════════════════════════════════════════════════════════════════════
# ASSIGNMENT GROUP CONTACTS (bulk view across multiple assignments)
# ═══════════════════════════════════════════════════════════════════════════

# Cap to avoid pathological requests; bucket groups in practice are 2–10 lists.
_GROUP_CONTACTS_MAX_IDS = 50


# Page bound. Groups can have tens of thousands of contacts; paginate the UI
# fetch to keep the page interactive. CSV export below has no page limit.
_GROUP_CONTACTS_DEFAULT_LIMIT = 1000
_GROUP_CONTACTS_MAX_LIMIT = 5000


def _parse_group_ids(ids: str) -> list[str]:
    parsed = [s for s in (x.strip() for x in ids.split(",")) if s]
    if not parsed:
        raise HTTPException(400, "ids is required")
    seen: set[str] = set()
    parsed = [a for a in parsed if not (a in seen or seen.add(a))]
    if len(parsed) > _GROUP_CONTACTS_MAX_IDS:
        raise HTTPException(400, f"Too many ids (max {_GROUP_CONTACTS_MAX_IDS})")
    return parsed


@router.get("/assignment-groups/contacts")
async def get_group_contacts(
    ids: str = Query(..., description="Comma-separated assignment IDs"),
    status: str = Query("assigned", regex="^(assigned|used|all)$"),
    limit: int = Query(_GROUP_CONTACTS_DEFAULT_LIMIT, ge=1, le=_GROUP_CONTACTS_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    assignment_ids = _parse_group_ids(ids)

    asgn_result = await db.execute(
        select(WebinarListAssignment)
        .where(
            WebinarListAssignment.id.in_(assignment_ids),
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
        .options(selectinload(WebinarListAssignment.bucket), selectinload(WebinarListAssignment.webinar))
    )
    assignments = asgn_result.scalars().all()
    if len(assignments) != len(assignment_ids):
        raise HTTPException(404, "One or more assignments not found")

    not_blocklisted = sa_func.lower(Contact.email).notin_(_blocklist_email_subquery())

    q = (
        select(Contact)
        .where(
            Contact.assignment_id.in_(assignment_ids),
            Contact.user_id == LLOYD_USER_ID,
            not_blocklisted,
        )
    )
    if status != "all":
        q = q.where(Contact.outreach_status == status)
    q = q.order_by(Contact.first_name, Contact.email).limit(limit).offset(offset)

    result = await db.execute(q)
    contacts = result.scalars().all()

    count_result = await db.execute(
        select(Contact.outreach_status, sa_func.count())
        .where(
            Contact.assignment_id.in_(assignment_ids),
            Contact.user_id == LLOYD_USER_ID,
            not_blocklisted,
        )
        .group_by(Contact.outreach_status)
    )
    counts = {row[0]: row[1] for row in count_result}

    bl_counts = await compute_blocklist_counts_per_assignment(db, assignment_ids)
    blocklisted_total = sum((v.get("total", 0) for v in bl_counts.values()), 0)

    bucket_names = {a.bucket.name for a in assignments if a.bucket and a.bucket.name}
    webinar_numbers = {a.webinar.number for a in assignments if a.webinar}
    webinar_dates = {a.webinar.date.isoformat() for a in assignments if a.webinar and a.webinar.date}

    attached_total = counts.get("assigned", 0) + counts.get("used", 0)
    filtered_total = (
        sum(counts.values()) if status == "all" else counts.get(status, 0)
    )

    return {
        "group": {
            "assignment_ids": assignment_ids,
            "bucket_name": next(iter(bucket_names)) if len(bucket_names) == 1 else None,
            "webinar_number": next(iter(webinar_numbers)) if len(webinar_numbers) == 1 else None,
            "webinar_date": next(iter(webinar_dates)) if len(webinar_dates) == 1 else None,
            "list_count": len(assignments),
            "volume": attached_total,
            "volume_raw": sum((a.volume or 0) for a in assignments),
            "blocklisted_total": blocklisted_total,
        },
        "contacts": [
            {
                "id": c.id,
                "email": c.email,
                "first_name": c.first_name,
                "outreach_status": c.outreach_status,
                "used_at": c.used_at.isoformat() if c.used_at else None,
            }
            for c in contacts
        ],
        "counts": {
            "assigned": counts.get("assigned", 0),
            "used": counts.get("used", 0),
            "total": sum(counts.values()),
        },
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(contacts),
            "filtered_total": filtered_total,
        },
    }


@router.get("/assignment-groups/contacts.csv")
async def stream_group_contacts_csv(
    ids: str = Query(..., description="Comma-separated assignment IDs"),
    status: str = Query("assigned", regex="^(assigned|used|all)$"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    assignment_ids = _parse_group_ids(ids)

    # Verify ownership and gather list names for the CSV's "list_name" column.
    asgn_result = await db.execute(
        select(WebinarListAssignment)
        .where(
            WebinarListAssignment.id.in_(assignment_ids),
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
        .options(selectinload(WebinarListAssignment.bucket), selectinload(WebinarListAssignment.webinar))
    )
    assignments = asgn_result.scalars().all()
    if len(assignments) != len(assignment_ids):
        raise HTTPException(404, "One or more assignments not found")

    def _list_name(a: WebinarListAssignment) -> str:
        if a.list_name:
            return a.list_name
        bucket = a.bucket.name if a.bucket else ""
        w = f"W{a.webinar.number}" if a.webinar else ""
        return " — ".join(p for p in (w, bucket) if p) or a.id

    list_name_by_aid = {a.id: _list_name(a) for a in assignments}

    not_blocklisted = sa_func.lower(Contact.email).notin_(_blocklist_email_subquery())

    async def row_iter():
        # CSV header.
        header_buf = io.StringIO()
        csv.writer(header_buf).writerow(["email", "first_name", "list_name", "outreach_status", "used_at"])
        yield header_buf.getvalue()

        q = (
            select(
                Contact.email,
                Contact.first_name,
                Contact.assignment_id,
                Contact.outreach_status,
                Contact.used_at,
            )
            .where(
                Contact.assignment_id.in_(assignment_ids),
                Contact.user_id == LLOYD_USER_ID,
                not_blocklisted,
            )
        )
        if status != "all":
            q = q.where(Contact.outreach_status == status)
        q = q.order_by(Contact.first_name, Contact.email).execution_options(yield_per=2000)

        buf = io.StringIO()
        writer = csv.writer(buf)
        stream = await db.stream(q)
        async for email, first_name, aid, oreach_status, used_at in stream:
            writer.writerow([
                email or "",
                first_name or "",
                list_name_by_aid.get(aid, ""),
                oreach_status or "",
                used_at.isoformat() if used_at else "",
            ])
            # Flush in ~64KB chunks so bytes leave the server steadily and
            # the platform proxy doesn't think the connection is idle.
            if buf.tell() > 64 * 1024:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        if buf.tell() > 0:
            yield buf.getvalue()

    filename = f"group_{len(assignment_ids)}_lists_{status}.csv"
    return StreamingResponse(
        row_iter(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class GroupMarkUsedRequest(BaseModel):
    contact_ids: list[str]


@router.put("/assignment-groups/contacts/mark-used")
async def mark_group_contacts_used(
    body: GroupMarkUsedRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if not body.contact_ids:
        return {"marked": 0, "by_assignment": {}}

    now = datetime.now(timezone.utc)

    # Find the contacts (filtered by user) and their source assignments before
    # the update so we know which assignment counters to decrement.
    src_result = await db.execute(
        select(Contact.id, Contact.assignment_id)
        .where(
            Contact.id.in_(body.contact_ids),
            Contact.user_id == LLOYD_USER_ID,
            Contact.outreach_status == "assigned",
        )
    )
    rows = src_result.all()
    target_ids = [r.id for r in rows]
    if not target_ids:
        return {"marked": 0, "by_assignment": {}}

    per_assignment: dict[str, int] = {}
    for r in rows:
        per_assignment[r.assignment_id] = per_assignment.get(r.assignment_id, 0) + 1

    update_result = await db.execute(
        update(Contact)
        .where(Contact.id.in_(target_ids))
        .values(outreach_status="used", used_at=now)
    )
    marked = update_result.rowcount or 0

    # Decrement each affected assignment's remaining counter atomically in the
    # same transaction. Bound at zero to mirror the single-assignment endpoint.
    if per_assignment:
        affected = await db.execute(
            select(WebinarListAssignment).where(
                WebinarListAssignment.id.in_(list(per_assignment.keys())),
                WebinarListAssignment.user_id == LLOYD_USER_ID,
            )
        )
        for asgn in affected.scalars().all():
            dec = per_assignment.get(asgn.id, 0)
            if dec:
                asgn.remaining = max(0, (asgn.remaining or 0) - dec)
    await db.flush()

    return {"marked": marked, "by_assignment": per_assignment}


# ═══════════════════════════════════════════════════════════════════════════
# WEBINAR LIST EXPORT (background CSV of assigned contacts + list names)
# ═══════════════════════════════════════════════════════════════════════════

def _build_list_name_for_assignment(a: WebinarListAssignment) -> str:
    """Compose the per-assignment list name used in the CSV.

    Format: "{description} - {sender} - {title version} - {description version}"
    Missing parts are omitted so we don't emit dangling separators.
    """
    parts: list[str] = []
    if a.description:
        parts.append(a.description)
    if a.sender and a.sender.name:
        parts.append(a.sender.name)
    if a.title_copy is not None:
        parts.append(f"V{a.title_copy.variant_index + 1}")
    if a.desc_copy is not None:
        parts.append(f"V{a.desc_copy.variant_index + 1}")
    return " - ".join(parts)


async def _run_webinar_list_export_job(job_id: str) -> None:
    """Build the CSV for a webinar's assigned-lists and save it on the job row."""
    async with AsyncSessionLocal() as db:
        try:
            job_result = await db.execute(
                select(WebinarListExportJob).where(WebinarListExportJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()
            if not job:
                logger.warning("Webinar list export job %s not found", job_id)
                return

            job.status = "processing"
            job.started_at = datetime.now(timezone.utc)
            job.error_message = None
            await db.commit()

            asgn_result = await db.execute(
                select(WebinarListAssignment)
                .where(
                    WebinarListAssignment.webinar_id == job.webinar_id,
                    WebinarListAssignment.user_id == job.user_id,
                    WebinarListAssignment.is_nonjoiners.is_(False),
                    WebinarListAssignment.is_no_list_data.is_(False),
                )
                .options(
                    selectinload(WebinarListAssignment.sender),
                    selectinload(WebinarListAssignment.title_copy),
                    selectinload(WebinarListAssignment.desc_copy),
                )
            )
            assignments = asgn_result.scalars().all()
            list_name_by_aid = {a.id: _build_list_name_for_assignment(a) for a in assignments}

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Email", "List name"])

            total = 0
            if list_name_by_aid:
                stream = await db.stream(
                    select(Contact.email, Contact.assignment_id)
                    .where(
                        Contact.assignment_id.in_(list(list_name_by_aid.keys())),
                        Contact.user_id == job.user_id,
                        Contact.outreach_status.in_(("assigned", "used")),
                        Contact.email.is_not(None),
                    )
                    .order_by(Contact.assignment_id, Contact.email)
                    .execution_options(yield_per=5000)
                )
                async for email, aid in stream:
                    if not email:
                        continue
                    writer.writerow([email, list_name_by_aid.get(aid, "")])
                    total += 1

            job.csv_content = buf.getvalue()
            job.contact_count = total
            job.status = "ready"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
        except Exception as exc:
            logger.exception("Webinar list export job %s failed", job_id)
            try:
                await db.rollback()
                fail_result = await db.execute(
                    select(WebinarListExportJob).where(WebinarListExportJob.id == job_id)
                )
                job = fail_result.scalar_one_or_none()
                if job:
                    job.status = "failed"
                    job.error_message = str(exc)[:500]
                    job.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                logger.exception("Failed to mark export job %s as failed", job_id)
        finally:
            _active_export_tasks.pop(job_id, None)


def _spawn_webinar_list_export_job(job_id: str) -> None:
    task = asyncio.create_task(_run_webinar_list_export_job(job_id))
    _active_export_tasks[job_id] = task


def _export_job_dict(job: WebinarListExportJob) -> dict:
    return {
        "id": job.id,
        "webinar_id": job.webinar_id,
        "status": job.status,
        "contact_count": job.contact_count,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.post("/webinars/{webinar_id}/export-lists", status_code=202)
async def start_webinar_list_export(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Kick off a background CSV export of a webinar's assigned-list contacts.

    If an in-flight job already exists, returns it instead of creating a new one.
    """
    w_result = await db.execute(
        select(Webinar).where(Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID)
    )
    webinar = w_result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")

    existing_result = await db.execute(
        select(WebinarListExportJob)
        .where(
            WebinarListExportJob.webinar_id == webinar_id,
            WebinarListExportJob.user_id == LLOYD_USER_ID,
            WebinarListExportJob.status.in_(("pending", "processing")),
        )
        .order_by(WebinarListExportJob.created_at.desc())
        .limit(1)
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        return _export_job_dict(existing)

    job = WebinarListExportJob(
        user_id=LLOYD_USER_ID,
        webinar_id=webinar_id,
        status="pending",
    )
    db.add(job)
    await db.flush()
    await db.commit()

    _spawn_webinar_list_export_job(job.id)
    return _export_job_dict(job)


@router.get("/webinars/{webinar_id}/export-lists/latest")
async def get_latest_webinar_list_export(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Return the most recent export job for a webinar (or null if none)."""
    result = await db.execute(
        select(WebinarListExportJob)
        .where(
            WebinarListExportJob.webinar_id == webinar_id,
            WebinarListExportJob.user_id == LLOYD_USER_ID,
        )
        .order_by(WebinarListExportJob.created_at.desc())
        .limit(1)
    )
    job = result.scalar_one_or_none()
    return {"job": _export_job_dict(job) if job else None}


@router.get("/webinars/export-lists/active")
async def list_active_webinar_list_exports(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Latest export job per webinar — used by Planning page to restore state."""
    result = await db.execute(
        select(WebinarListExportJob)
        .where(WebinarListExportJob.user_id == LLOYD_USER_ID)
        .order_by(WebinarListExportJob.created_at.desc())
    )
    rows = result.scalars().all()
    latest: dict[str, WebinarListExportJob] = {}
    for j in rows:
        if j.webinar_id not in latest:
            latest[j.webinar_id] = j
    return {"jobs": [_export_job_dict(j) for j in latest.values()]}


@router.get("/webinars/{webinar_id}/export-lists/{job_id}/download")
async def download_webinar_list_export(
    webinar_id: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Stream the CSV for a completed export job."""
    result = await db.execute(
        select(WebinarListExportJob, Webinar.number)
        .join(Webinar, Webinar.id == WebinarListExportJob.webinar_id)
        .where(
            WebinarListExportJob.id == job_id,
            WebinarListExportJob.webinar_id == webinar_id,
            WebinarListExportJob.user_id == LLOYD_USER_ID,
        )
        .options(undefer(WebinarListExportJob.csv_content))
    )
    row = result.first()
    if not row:
        raise HTTPException(404, "Export job not found")
    job, webinar_number = row
    if job.status != "ready" or job.csv_content is None:
        raise HTTPException(409, f"Export not ready (status: {job.status})")

    filename = f"webinar-{webinar_number}-lists.csv"
    return Response(
        content=job.csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
