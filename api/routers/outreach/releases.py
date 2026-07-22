"""Outreach sub-router: release contacts back to the bucket pool after a webinar.

Operators upload a CSV of emails that could not be contacted in time. We revert
those contacts (status `assigned` or `used` → `available`) so they can be
re-assigned to a future webinar. `WebinarListAssignment.volume` is left
untouched so the original "planned" number is preserved for plan-vs-actual
comparison on the statistics page.

Each released contact is recorded in `contact_release_log` for a future undo /
auth-aware audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func as sa_func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID, recompute_contact_caches
from db.models import (
    Contact, ContactReleaseLog, OutreachBucket, Webinar,
    WebinarContactMembership, WebinarListAssignment,
)
from db.session import get_db


router = APIRouter()


class ReleaseRequest(BaseModel):
    emails: list[str]
    # Optional batch id to group multiple chunked requests into one audit
    # entry. The frontend uploads in 1k-row chunks for progress reporting;
    # all chunks for the same upload share a release_batch_id so the audit
    # log + future "undo" action treat them atomically. The first chunk
    # omits this and the server generates one; subsequent chunks pass it back.
    release_batch_id: str | None = None


class ReleaseByIdRequest(BaseModel):
    contact_ids: list[str]
    release_batch_id: str | None = None
    # Scope guard: the assignment(s) the operator is currently looking at.
    # The server will refuse to release any contact_id whose current
    # assignment_id is not in this set — protects against a future UI bug
    # accidentally submitting ids outside the visible page.
    assignment_ids: list[str] | None = None


def _normalize_email(raw: str) -> str | None:
    if not raw:
        return None
    e = raw.strip().lower()
    return e or None


# asyncpg caps bind parameters at 32,767 per query. Our largest IN-clauses use
# one parameter per email (plus a few constants), so cap at 5,000 to stay well
# under the limit and match the chunking pattern used by the import pipeline.
_DB_CHUNK_SIZE = 5000


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


@router.post("/webinars/{webinar_id}/releases", status_code=201)
async def release_contacts(
    webinar_id: str,
    body: ReleaseRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Release contacts in this webinar back to `available`.

    For each email in `body.emails` that maps to a contact assigned to one of
    this webinar's WebinarListAssignments and currently in status `assigned`
    or `used`: revert the contact (clear assignment_id, used_at, assigned_date;
    set status to `available`) and snapshot the prior state into
    `contact_release_log` under one shared `release_batch_id`.

    Bucket `remaining_contacts` is restored from the live `available` count for
    each touched bucket. Assignment `volume` is intentionally untouched so the
    planned-send number is preserved for statistics comparison.
    """
    w_result = await db.execute(
        select(Webinar).where(
            Webinar.id == webinar_id,
            Webinar.user_id == LLOYD_USER_ID,
        )
    )
    webinar = w_result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")

    a_result = await db.execute(
        select(WebinarListAssignment).where(
            WebinarListAssignment.webinar_id == webinar_id,
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
    )
    assignments_by_id: dict[str, WebinarListAssignment] = {
        a.id: a for a in a_result.scalars().all()
    }
    assignment_ids = list(assignments_by_id.keys())
    if not assignment_ids:
        return {
            "release_batch_id": None,
            "released": 0,
            "not_found": [],
            "already_available": [],
            "by_status": {"assigned": 0, "used": 0},
            "bucket_updates": {},
        }

    # Normalize + dedupe input emails, drop empties
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in body.emails:
        e = _normalize_email(raw)
        if e and e not in seen:
            seen.add(e)
            normalized.append(e)

    if not normalized:
        raise HTTPException(400, "No valid emails provided")

    # Match against THIS webinar's membership rows (not the single legacy slot),
    # so a reused contact can be released from this webinar even though its legacy
    # slot points at an earlier one. UNIQUE(webinar_id, contact_id) ⇒ at most one
    # membership per email here. Keyed by lowercased email.
    by_email: dict[str, dict] = {}
    for chunk in _chunked(normalized, _DB_CHUNK_SIZE):
        c_result = await db.execute(
            select(
                WebinarContactMembership.contact_id,
                sa_func.lower(Contact.email).label("email"),
                WebinarContactMembership.status,
                WebinarContactMembership.assignment_id,
                WebinarContactMembership.bucket_id,
                WebinarContactMembership.used_at,
                Contact.assignment_id.label("legacy_assignment_id"),
            )
            .join(Contact, Contact.id == WebinarContactMembership.contact_id)
            .where(
                WebinarContactMembership.webinar_id == webinar_id,
                WebinarContactMembership.user_id == LLOYD_USER_ID,
                sa_func.lower(Contact.email).in_(chunk),
            )
        )
        for row in c_result.all():
            by_email[row.email] = {
                "id": row.contact_id,
                "status": row.status,
                "assignment_id": row.assignment_id,
                "bucket_id": row.bucket_id,
                "used_at": row.used_at,
                "legacy_assignment_id": row.legacy_assignment_id,
            }

    release_batch_id = body.release_batch_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    not_found: list[str] = []
    already_available: list[str] = []
    by_status_count = {"assigned": 0, "used": 0}
    touched_bucket_ids: set[str] = set()
    log_rows: list[dict] = []
    contact_ids_to_release: list[str] = []
    legacy_reset_ids: list[str] = []

    for email in normalized:
        target = by_email.get(email)
        if target is None:
            # No membership in this webinar — nothing to release here.
            not_found.append(email)
            continue

        log_rows.append({
            "user_id": LLOYD_USER_ID,
            "webinar_id": webinar_id,
            "release_batch_id": release_batch_id,
            "released_at": now,
            "released_by": None,
            "contact_id": target["id"],
            "email": email,
            "prior_status": target["status"],
            "prior_assignment_id": target["assignment_id"],
            "prior_bucket_id": target["bucket_id"],
            "prior_used_at": target["used_at"],
        })
        contact_ids_to_release.append(target["id"])
        by_status_count[target["status"]] += 1
        if target["bucket_id"]:
            touched_bucket_ids.add(target["bucket_id"])
        # Only reset the legacy slot when it actually represents THIS webinar's
        # membership; a reused contact's slot points at another webinar and must
        # be left intact.
        if target["legacy_assignment_id"] and target["legacy_assignment_id"] == target["assignment_id"]:
            legacy_reset_ids.append(target["id"])

        # `assignment.remaining` tracks "claimed but not yet marked used"
        # (mark_contacts_used decrements it). Releasing an `assigned` contact
        # removes one from that pool. Releasing a `used` contact doesn't
        # touch it — it was already decremented at mark-used time.
        if target["status"] == "assigned" and target["assignment_id"]:
            asn = assignments_by_id.get(target["assignment_id"])
            if asn:
                asn.remaining = max(0, (asn.remaining or 0) - 1)

    # Legacy slot dual-write — only contacts whose slot represents this webinar.
    for chunk in _chunked(legacy_reset_ids, _DB_CHUNK_SIZE):
        await db.execute(
            update(Contact)
            .where(Contact.id.in_(chunk))
            .values(
                outreach_status="available",
                assignment_id=None,
                assigned_date=None,
                used_at=None,
            )
        )

    # Bulk INSERT audit-log rows. asyncpg's param cap is 32,767; each row has
    # 11 columns so ~2,900 rows per insert is the hard limit — we use 2,000.
    LOG_CHUNK = 2000
    for chunk in _chunked(log_rows, LOG_CHUNK):
        await db.execute(insert(ContactReleaseLog), chunk)

    # Remove this webinar's membership rows for the released contacts (release =
    # "never scheduled for this webinar" → drops out of its metrics and stops
    # counting toward times_invited), then recompute the affected caches.
    for chunk in _chunked(contact_ids_to_release, _DB_CHUNK_SIZE):
        await db.execute(
            delete(WebinarContactMembership).where(
                WebinarContactMembership.webinar_id == webinar_id,
                WebinarContactMembership.contact_id.in_(chunk),
            )
        )
    await recompute_contact_caches(db, contact_ids_to_release)

    # Reconcile bucket.remaining_contacts from the live fresh baseline (never
    # invited, not in-flight) — keeps the field self-healing if it ever drifts.
    bucket_updates: dict[str, int] = {}
    if touched_bucket_ids:
        await db.flush()  # so the cache updates are visible to the count query
        for bucket_id in touched_bucket_ids:
            cnt_result = await db.execute(
                select(sa_func.count()).where(
                    Contact.bucket_id == bucket_id,
                    Contact.last_invited_at.is_(None),
                    Contact.assigned_membership_count == 0,
                )
            )
            available_count = int(cnt_result.scalar() or 0)
            await db.execute(
                update(OutreachBucket)
                .where(OutreachBucket.id == bucket_id)
                .values(remaining_contacts=available_count)
            )
            bucket_updates[bucket_id] = available_count

    await db.flush()
    released_count = len(contact_ids_to_release)

    # Always return the batch_id (even on a 0-released chunk) so the client
    # can pass it through to subsequent chunks of the same upload.
    return {
        "release_batch_id": release_batch_id,
        "released": released_count,
        "not_found": not_found,
        "already_available": already_available,
        "by_status": by_status_count,
        "bucket_updates": bucket_updates,
    }


@router.get("/webinars/{webinar_id}/releases")
async def list_releases(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """List release batches for this webinar (newest first)."""
    w_result = await db.execute(
        select(Webinar.id).where(
            Webinar.id == webinar_id,
            Webinar.user_id == LLOYD_USER_ID,
        )
    )
    if not w_result.scalar_one_or_none():
        raise HTTPException(404, "Webinar not found")

    from sqlalchemy import func as sa_func
    r = await db.execute(
        select(
            ContactReleaseLog.release_batch_id,
            sa_func.min(ContactReleaseLog.released_at).label("released_at"),
            sa_func.count().label("count"),
            sa_func.count().filter(ContactReleaseLog.prior_status == "used").label("used_count"),
            sa_func.count().filter(ContactReleaseLog.prior_status == "assigned").label("assigned_count"),
        )
        .where(
            ContactReleaseLog.webinar_id == webinar_id,
            ContactReleaseLog.user_id == LLOYD_USER_ID,
        )
        .group_by(ContactReleaseLog.release_batch_id)
        .order_by(sa_func.min(ContactReleaseLog.released_at).desc())
    )
    batches = [
        {
            "release_batch_id": row.release_batch_id,
            "released_at": row.released_at.isoformat() if row.released_at else None,
            "count": int(row.count or 0),
            "used_count": int(row.used_count or 0),
            "assigned_count": int(row.assigned_count or 0),
        }
        for row in r.all()
    ]
    return {"batches": batches}


@router.post("/contacts/releases", status_code=201)
async def release_contacts_by_id(
    body: ReleaseByIdRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Release a set of contacts (by id) back to `available`.

    Used by the per-assignment / per-group contacts pages where the operator
    selects rows directly. Same revert + audit-log + bucket-reconcile pipeline
    as the email-based endpoint above. Contacts can span multiple webinars and
    assignments — each contact is logged against its current webinar.
    """
    # Dedup, preserve order
    seen: set[str] = set()
    contact_ids = [c for c in body.contact_ids if c and not (c in seen or seen.add(c))]
    if not contact_ids:
        raise HTTPException(400, "No contact_ids provided")

    scope_assignment_ids: set[str] | None = (
        set(body.assignment_ids) if body.assignment_ids else None
    )

    # Load the membership rows for these contacts (optionally restricted to the
    # assignment(s) the operator is viewing). The membership carries webinar_id
    # and the authoritative status — so a reused contact is released from the
    # viewed webinar, not whatever its legacy slot happens to point at.
    m = WebinarContactMembership
    mem_by_contact: dict[str, dict] = {}
    for chunk in _chunked(contact_ids, _DB_CHUNK_SIZE):
        conds = [m.user_id == LLOYD_USER_ID, m.contact_id.in_(chunk)]
        if scope_assignment_ids is not None:
            conds.append(m.assignment_id.in_(scope_assignment_ids))
        c_result = await db.execute(
            select(
                m.contact_id,
                sa_func.lower(Contact.email).label("email"),
                m.status, m.webinar_id, m.assignment_id, m.bucket_id, m.used_at,
                Contact.assignment_id.label("legacy_assignment_id"),
            )
            .join(Contact, Contact.id == m.contact_id)
            .where(*conds)
        )
        for row in c_result.all():
            # One membership per contact for the viewed page (scope narrows it).
            mem_by_contact.setdefault(row.contact_id, {
                "id": row.contact_id,
                "email": row.email,
                "status": row.status,
                "webinar_id": row.webinar_id,
                "assignment_id": row.assignment_id,
                "bucket_id": row.bucket_id,
                "used_at": row.used_at,
                "legacy_assignment_id": row.legacy_assignment_id,
            })

    # Touched assignments — load once so we can decrement remaining counters.
    touched_assignment_ids = [
        r["assignment_id"] for r in mem_by_contact.values()
        if r["assignment_id"] and r["status"] == "assigned"
    ]
    assignments_by_id: dict[str, WebinarListAssignment] = {}
    if touched_assignment_ids:
        a_result = await db.execute(
            select(WebinarListAssignment).where(
                WebinarListAssignment.id.in_(set(touched_assignment_ids)),
                WebinarListAssignment.user_id == LLOYD_USER_ID,
            )
        )
        assignments_by_id = {a.id: a for a in a_result.scalars().all()}

    release_batch_id = body.release_batch_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    not_found: list[str] = []
    already_available: list[str] = []
    out_of_scope: list[str] = []
    by_status_count = {"assigned": 0, "used": 0}
    touched_bucket_ids: set[str] = set()
    log_rows: list[dict] = []
    contact_ids_to_release: list[str] = []
    legacy_reset_ids: list[str] = []

    for cid in contact_ids:
        row = mem_by_contact.get(cid)
        if row is None:
            # No membership (in scope) → nothing to release for this contact.
            not_found.append(cid)
            continue

        log_rows.append({
            "user_id": LLOYD_USER_ID,
            "webinar_id": row["webinar_id"],
            "release_batch_id": release_batch_id,
            "released_at": now,
            "released_by": None,
            "contact_id": row["id"],
            "email": row["email"],
            "prior_status": row["status"],
            "prior_assignment_id": row["assignment_id"],
            "prior_bucket_id": row["bucket_id"],
            "prior_used_at": row["used_at"],
        })
        contact_ids_to_release.append(row["id"])
        by_status_count[row["status"]] += 1
        if row["bucket_id"]:
            touched_bucket_ids.add(row["bucket_id"])
        if row["legacy_assignment_id"] and row["legacy_assignment_id"] == row["assignment_id"]:
            legacy_reset_ids.append(row["id"])

        if row["status"] == "assigned" and row["assignment_id"]:
            asn = assignments_by_id.get(row["assignment_id"])
            if asn:
                asn.remaining = max(0, (asn.remaining or 0) - 1)

    # Legacy slot reset — only where the slot represents the released membership.
    for chunk in _chunked(legacy_reset_ids, _DB_CHUNK_SIZE):
        await db.execute(
            update(Contact)
            .where(Contact.id.in_(chunk))
            .values(
                outreach_status="available",
                assignment_id=None,
                assigned_date=None,
                used_at=None,
            )
        )

    LOG_CHUNK = 2000
    for chunk in _chunked(log_rows, LOG_CHUNK):
        await db.execute(insert(ContactReleaseLog), chunk)

    # Remove the membership rows for exactly the (contact, webinar) pairs being
    # released (webinar_id came from each contact's viewed assignment), so a
    # reused contact loses only the membership for THIS webinar. Then recompute
    # the affected caches.
    release_by_webinar: dict[str, list[str]] = {}
    for lr in log_rows:
        release_by_webinar.setdefault(lr["webinar_id"], []).append(lr["contact_id"])
    for wid, cids in release_by_webinar.items():
        for chunk in _chunked(cids, _DB_CHUNK_SIZE):
            await db.execute(
                delete(WebinarContactMembership).where(
                    WebinarContactMembership.webinar_id == wid,
                    WebinarContactMembership.contact_id.in_(chunk),
                )
            )
    await recompute_contact_caches(db, contact_ids_to_release)

    bucket_updates: dict[str, int] = {}
    if touched_bucket_ids:
        await db.flush()
        for bucket_id in touched_bucket_ids:
            cnt_result = await db.execute(
                select(sa_func.count()).where(
                    Contact.bucket_id == bucket_id,
                    Contact.last_invited_at.is_(None),
                    Contact.assigned_membership_count == 0,
                )
            )
            available_count = int(cnt_result.scalar() or 0)
            await db.execute(
                update(OutreachBucket)
                .where(OutreachBucket.id == bucket_id)
                .values(remaining_contacts=available_count)
            )
            bucket_updates[bucket_id] = available_count

    await db.flush()

    return {
        "release_batch_id": release_batch_id,
        "released": len(contact_ids_to_release),
        "not_found": not_found,
        "already_available": already_available,
        "out_of_scope": out_of_scope,
        "by_status": by_status_count,
        "bucket_updates": bucket_updates,
    }
