"""Public read-only stats: contact counts for external apps.

Protected by a dedicated key (X-API-Key header), not the app bearer token.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_stats_key
from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import Contact, OutreachBucket
from db.session import get_db

router = APIRouter()


def _is_disqualified(name: str | None) -> bool:
    return (name or "").strip().lower() == "disqualified"


@router.get("/contact-counts")
async def contact_counts(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_stats_key),
):
    """Return total, available, and disqualified contact counts.

    - total_contacts: every contact (all statuses, all buckets, incl. unbucketed).
    - available_contacts: outreach_status='available' in non-Disqualified buckets
      (mirrors the Planning page "available" number).
    - disqualified_contacts: contacts in the Disqualified bucket.
    """
    # Split the user's live buckets into Disqualified vs. the rest.
    bucket_rows = await db.execute(
        select(OutreachBucket.id, OutreachBucket.name).where(
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
        )
    )
    dq_bucket_ids: list[str] = []
    non_dq_bucket_ids: list[str] = []
    for bucket_id, name in bucket_rows:
        (dq_bucket_ids if _is_disqualified(name) else non_dq_bucket_ids).append(bucket_id)

    total_contacts = await db.scalar(
        select(sa_func.count()).select_from(Contact).where(Contact.user_id == LLOYD_USER_ID)
    )

    disqualified_contacts = 0
    if dq_bucket_ids:
        disqualified_contacts = await db.scalar(
            select(sa_func.count())
            .select_from(Contact)
            .where(Contact.bucket_id.in_(dq_bucket_ids))
        )

    available_contacts = 0
    if non_dq_bucket_ids:
        available_contacts = await db.scalar(
            select(sa_func.count())
            .select_from(Contact)
            .where(
                Contact.outreach_status == "available",
                Contact.bucket_id.in_(non_dq_bucket_ids),
            )
        )

    return {
        "total_contacts": total_contacts or 0,
        "available_contacts": available_contacts or 0,
        "disqualified_contacts": disqualified_contacts or 0,
    }
