"""
Shared constants and serialization helpers for outreach sub-routers.
"""
from sqlalchemy import and_, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    BlocklistEntry, BucketCopy, Contact, OutreachBucket, OutreachSender,
    Webinar, WebinarListAssignment,
)

# Hardcoded to Lloyd's user_id — single-tenant for now
LLOYD_USER_ID = "9baf8117-db65-4f30-87a5-a76cf4f23d82"


# ── Blocklist helpers ─────────────────────────────────────────────────────

def _blocklist_email_subquery():
    """A scalar subquery yielding every blocklisted email for the current user.

    Blocklist emails are stored already lowercased, so callers should
    compare against LOWER(Contact.email).
    """
    return (
        select(BlocklistEntry.email)
        .where(BlocklistEntry.user_id == LLOYD_USER_ID)
        .scalar_subquery()
    )


async def compute_blocklist_counts_per_bucket(
    db: AsyncSession, bucket_ids: list[str]
) -> dict[str, dict]:
    """Return {bucket_id: {"total": N, "available": M}} of blocklisted contacts.

    - total: blocklisted contacts in the bucket, any status.
    - available: blocklisted contacts still in outreach_status='available'.
    """
    if not bucket_ids:
        return {}
    blocklisted_expr = sa_func.lower(Contact.email).in_(_blocklist_email_subquery())
    result = await db.execute(
        select(
            Contact.bucket_id,
            sa_func.count().filter(blocklisted_expr).label("total"),
            sa_func.count().filter(
                and_(Contact.outreach_status == "available", blocklisted_expr)
            ).label("available"),
        )
        .where(Contact.bucket_id.in_(bucket_ids))
        .group_by(Contact.bucket_id)
    )
    return {
        row.bucket_id: {"total": row.total or 0, "available": row.available or 0}
        for row in result
    }


async def compute_blocklist_counts_per_assignment(
    db: AsyncSession, assignment_ids: list[str]
) -> dict[str, dict]:
    """Return {assignment_id: {"total": N, "assigned": M}} of blocklisted contacts.

    - total: blocklisted contacts ever claimed by the assignment (any status).
    - assigned: blocklisted contacts still in outreach_status='assigned'.
    """
    if not assignment_ids:
        return {}
    blocklisted_expr = sa_func.lower(Contact.email).in_(_blocklist_email_subquery())
    result = await db.execute(
        select(
            Contact.assignment_id,
            sa_func.count().filter(blocklisted_expr).label("total"),
            sa_func.count().filter(
                and_(Contact.outreach_status == "assigned", blocklisted_expr)
            ).label("assigned"),
        )
        .where(Contact.assignment_id.in_(assignment_ids))
        .group_by(Contact.assignment_id)
    )
    return {
        row.assignment_id: {"total": row.total or 0, "assigned": row.assigned or 0}
        for row in result
    }


# ── Serialization helpers ─────────────────────────────────────────────────

def bucket_dict(
    b: OutreachBucket,
    include_copies: bool = False,
    assigned_copy_ids: set[str] | None = None,
    blocklist_counts: dict | None = None,
) -> dict:
    try:
        all_copies = b.copies or []
    except Exception:
        all_copies = []
    titles = [c for c in all_copies if c.copy_type == "title" and not c.deleted_at]
    descs = [c for c in all_copies if c.copy_type == "description" and not c.deleted_at]
    bl = blocklist_counts or {}
    blocklisted_total = bl.get("total", 0)
    blocklisted_available = bl.get("available", 0)
    raw_total = b.total_contacts or 0
    raw_remaining = b.remaining_contacts or 0
    d = {
        "id": b.id,
        "name": b.name,
        "industry": b.industry,
        # Counts exposed to the UI exclude blocklisted contacts so the user
        # sees only the volume they can actually use. Raw values are kept as
        # *_raw for diagnostics or future UI.
        "total_contacts": max(0, raw_total - blocklisted_total),
        "remaining_contacts": max(0, raw_remaining - blocklisted_available),
        "total_contacts_raw": raw_total,
        "remaining_contacts_raw": raw_remaining,
        "blocklisted_total": blocklisted_total,
        "blocklisted_available": blocklisted_available,
        "countries": b.countries or [],
        "emp_range": b.emp_range,
        "source_file": b.source_file,
        "copies_count": {"titles": len(titles), "descriptions": len(descs)},
        "has_primary_title": any(c.is_primary for c in titles),
        "has_primary_description": any(c.is_primary for c in descs),
        "title_primary_picked": any(c.is_primary and c.primary_picked_by_user for c in titles),
        "desc_primary_picked": any(c.is_primary and c.primary_picked_by_user for c in descs),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }
    if include_copies:
        aids = assigned_copy_ids or set()
        d["titles"] = [copy_dict(c, is_assigned=c.id in aids) for c in sorted(titles, key=lambda x: x.variant_index)]
        d["descriptions"] = [copy_dict(c, is_assigned=c.id in aids) for c in sorted(descs, key=lambda x: x.variant_index)]
    return d


def copy_dict(c: BucketCopy, is_assigned: bool | None = None) -> dict:
    d = {
        "id": c.id,
        "bucket_id": c.bucket_id,
        "copy_type": c.copy_type,
        "variant_index": c.variant_index,
        "text": c.text,
        "is_primary": c.is_primary,
        "ai_feedback": c.ai_feedback,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if is_assigned is not None:
        d["is_assigned"] = is_assigned
    return d


def sender_dict(s: OutreachSender) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "total_accounts": s.total_accounts,
        "send_per_account": s.send_per_account,
        "days_per_webinar": s.days_per_webinar,
        "color": s.color,
        "display_order": s.display_order,
        "is_active": s.is_active,
    }


def webinar_dict(w: Webinar) -> dict:
    assignments = w.assignments or []
    return {
        "id": w.id,
        "number": w.number,
        "variant_label": w.variant_label,
        "webinargeek_credential_id": w.webinargeek_credential_id,
        "nonjoiner_source_webinar_id": w.nonjoiner_source_webinar_id,
        "date": w.date.isoformat() if w.date else None,
        "status": w.status,
        "broadcast_id": w.broadcast_id,
        "main_title": w.main_title,
        "registration_link": w.registration_link,
        "unsubscribe_link": w.unsubscribe_link,
        "assignment_count": len(assignments),
        "total_volume": sum(a.volume for a in assignments),
        "total_remaining": sum(a.remaining for a in assignments),
        "total_accounts": sum(a.accounts_used for a in assignments),
    }


def assignment_dict(
    a: WebinarListAssignment,
    blocklist_counts: dict | None = None,
) -> dict:
    bl = blocklist_counts or {}
    blocklisted_total = bl.get("total", 0)
    blocklisted_assigned = bl.get("assigned", 0)
    raw_volume = a.volume or 0
    raw_remaining = a.remaining or 0
    return {
        "id": a.id,
        "webinar_id": a.webinar_id,
        "bucket": {"id": a.bucket.id, "name": a.bucket.name, "industry": a.bucket.industry} if a.bucket else None,
        "sender": {"id": a.sender.id, "name": a.sender.name, "color": a.sender.color} if a.sender else None,
        "description": a.description,
        "list_url": a.list_url,
        # Volume / remaining shown to the user exclude blocklisted contacts
        # so the displayed list size matches what is actually usable.
        "volume": max(0, raw_volume - blocklisted_total),
        "remaining": max(0, raw_remaining - blocklisted_assigned),
        "volume_raw": raw_volume,
        "remaining_raw": raw_remaining,
        "blocklisted_total": blocklisted_total,
        "blocklisted_assigned": blocklisted_assigned,
        "gcal_invited": a.gcal_invited,
        "accounts_used": a.accounts_used,
        "send_per_account": a.send_per_account,
        "days": a.days,
        "title_copy": copy_dict(a.title_copy) if a.title_copy else None,
        "desc_copy": copy_dict(a.desc_copy) if a.desc_copy else None,
        "countries_override": a.countries_override,
        "emp_range_override": a.emp_range_override,
        "is_nonjoiners": a.is_nonjoiners,
        "is_no_list_data": a.is_no_list_data,
        "is_setup": a.is_setup,
        "source_type": a.source_type,
        "source_upload_id": a.source_upload_id,
        "list_name": a.list_name,
        "display_order": a.display_order,
    }
