"""Outreach sub-router: Buckets + Bucket Copies CRUD."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_, select, func as sa_func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.auth import require_auth
from api.routers.outreach._helpers import (
    LLOYD_USER_ID, bucket_dict, claimable_conditions,
    compute_blocklist_counts_per_bucket, copy_dict, employee_count_filter,
    reuse_cutoff_to_ts,
)
from api.schemas import (
    BucketCreate, BucketMergeRequest, BucketUpdate, CopyBulkGenerateRequest,
    CopyCreate, CopyGenerateRequest, CopyRegenerateRequest, CopyUpdate,
)
from db.models import (
    BucketCopy, BucketCopyGenerationJob, Contact, OutreachBucket,
    WebinarContactMembership, WebinarListAssignment,
)
from db.session import AsyncSessionLocal, get_db
from services.generation import generate_bucket_copies, regenerate_bucket_copy

logger = logging.getLogger(__name__)

router = APIRouter()

# Keep references so tasks aren't garbage-collected mid-flight
_active_copy_gen_tasks: dict[str, asyncio.Task] = {}




# ═══════════════════════════════════════════════════════════════════════════
# BUCKETS
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/buckets")
async def list_buckets(
    include: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    q = select(OutreachBucket).where(
        OutreachBucket.user_id == LLOYD_USER_ID,
        OutreachBucket.deleted_at.is_(None),
    ).order_by(OutreachBucket.remaining_contacts.desc())
    if include == "copies":
        q = q.options(selectinload(OutreachBucket.copies))
    result = await db.execute(q)
    buckets = result.scalars().all()

    # Serve the stored total_contacts / remaining_contacts counters directly.
    #
    # `remaining_contacts` is the fresh baseline — contacts never invited and not
    # in-flight on any unsent list (assigned_membership_count = 0) — and is
    # maintained at write time (imports, assignments, releases, merges). This
    # block used to re-derive it here with a `GROUP BY bucket_id` aggregate over
    # the *entire* contacts table (3.8M+ rows) on every single Planning-page load,
    # then write the results back. That aggregate routinely blew past the DB's
    # 120s statement_timeout, which surfaced as a 500 on GET /outreach/buckets and
    # made Planning take minutes (or fail to load at all). Trusting the stored
    # counters — the same thing the webinar/assignment endpoints already do —
    # keeps this read cheap. The live, reuse-filter-aware remaining is served
    # separately by GET /outreach/buckets/eligible.
    bucket_ids = [b.id for b in buckets]

    # When including copies, also fetch which copy IDs are actively assigned
    assigned_copy_ids: set[str] = set()
    if include == "copies" and bucket_ids:
        assigned_result = await db.execute(
            select(WebinarListAssignment.title_copy_id, WebinarListAssignment.desc_copy_id)
            .where(
                WebinarListAssignment.user_id == LLOYD_USER_ID,
                WebinarListAssignment.bucket_id.in_(bucket_ids),
            )
        )
        for row in assigned_result:
            if row.title_copy_id:
                assigned_copy_ids.add(row.title_copy_id)
            if row.desc_copy_id:
                assigned_copy_ids.add(row.desc_copy_id)

    blocklist_counts = await compute_blocklist_counts_per_bucket(db, bucket_ids)

    return {"buckets": [
        bucket_dict(
            b,
            include_copies=(include == "copies"),
            assigned_copy_ids=assigned_copy_ids,
            blocklist_counts=blocklist_counts.get(b.id),
        )
        for b in buckets
    ]}


# 60s micro-cache for /buckets/eligible: the Planning panel re-requests the
# same filter combos constantly (toggling filters back and forth), and each
# miss costs 1-8s of index scans. Single uvicorn worker → a module dict is the
# whole story. Mutating endpoints call invalidate_eligible_cache().
_ELIGIBLE_CACHE: dict[tuple, tuple[float, dict]] = {}
_ELIGIBLE_TTL = 60.0


def invalidate_eligible_cache() -> None:
    _ELIGIBLE_CACHE.clear()


@router.get("/buckets/eligible")
async def bucket_eligible_counts(
    reuse_cutoff: str | None = Query(None),
    reuse_before: str | None = Query(None),
    reuse_only: bool = Query(False),
    webinar_id: str | None = Query(None),
    country: list[str] | None = Query(None),
    emp_min: int | None = Query(None),
    emp_max: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Per-bucket claimable-remaining under the current reuse filter.

    Powers the "remaining" the Planning assign panel shows above the bucket list:
    bucket TOTAL is unchanged, but REMAINING reflects the reuse cutoff. Uses the
    exact same predicate the claim uses (via `claimable_conditions`) so the number
    ties out to what an assign would actually grab. Blocklisted contacts are
    excluded; `country` and `emp_min`/`emp_max` mirror the assign-form country and
    employee-count filters so the shown remaining matches what a claim would grab.
    """
    from datetime import date as _date

    # Empty-string ids (an empty <select> submits "") would reach the NOT
    # EXISTS as an invalid UUID and 500.
    webinar_id = webinar_id or None

    parsed_before = None
    if reuse_before:
        try:
            parsed_before = _date.fromisoformat(reuse_before)
        except ValueError:
            raise HTTPException(400, "reuse_before must be an ISO date (YYYY-MM-DD)")
    import time as _time

    cache_key = (
        reuse_cutoff, reuse_before, reuse_only, webinar_id,
        tuple(sorted(country)) if country else None, emp_min, emp_max,
    )
    hit = _ELIGIBLE_CACHE.get(cache_key)
    if hit and (_time.monotonic() - hit[0]) < _ELIGIBLE_TTL:
        return hit[1]

    cutoff_ts = reuse_cutoff_to_ts(reuse_cutoff, parsed_before)
    # Build the claimable predicate WITHOUT the target-webinar exclusion: any
    # predicate on contacts.id breaks the index-only scan over the covering
    # partial indexes (id isn't in them), turning this into ~600k heap fetches
    # that blow the 120s cap. Instead, the target webinar's already-member
    # overlap is counted separately below (driven from the SMALL membership
    # side) and subtracted per bucket.
    claimable = claimable_conditions(cutoff_ts, None, reuse_only=reuse_only)

    country_cond = None
    if country:
        blank_country = or_(Contact.country.is_(None), Contact.country == "")
        country_cond = or_(
            Contact.country.in_(country),
            and_(blank_country, Contact.list_location.in_(country)),
        )
    emp_conds = employee_count_filter(emp_min, emp_max)

    conds = [
        Contact.user_id == LLOYD_USER_ID,
        Contact.bucket_id.isnot(None),
        # NOT form matches ix_contacts_claimable's partial predicate exactly
        # (the planner does not prove `IS false` ⇒ `NOT col`).
        ~Contact.is_blocklisted,
        *claimable,
    ]
    if country_cond is not None:
        conds.append(country_cond)
    conds.extend(emp_conds)

    # The 1-3 aggregates below are independent — run them CONCURRENTLY on their
    # own pooled connections (they ran sequentially before; with filters active
    # that stacked 2-8s of index scans back to back).
    import asyncio as _asyncio

    from db.session import AsyncSessionLocal as _Session

    async def _grouped(stmt) -> dict:
        async with _Session() as s:
            res = await s.execute(stmt)
            return {row[0]: int(row[1] or 0) for row in res}

    counts_stmt = (
        select(Contact.bucket_id, sa_func.count())
        .where(*conds)
        .group_by(Contact.bucket_id)
    )

    # Contacts already members of the target webinar. Driven from the SMALL
    # membership side (one PK probe per member — zero/tiny for a freshly created
    # webinar), which keeps the big scan index-only. Equivalent to the old
    # in-query NOT-member predicate: base(claimable) − overlap(claimable ∧
    # member) = claimable ∧ ¬member, per bucket.
    overlap_stmt = None
    if webinar_id is not None:
        overlap_conds = [
            WebinarContactMembership.webinar_id == webinar_id,
            Contact.user_id == LLOYD_USER_ID,
            Contact.bucket_id.isnot(None),
            ~Contact.is_blocklisted,
            *claimable,
        ]
        if country_cond is not None:
            overlap_conds.append(country_cond)
        overlap_conds.extend(emp_conds)
        overlap_stmt = (
            select(Contact.bucket_id, sa_func.count())
            .select_from(WebinarContactMembership)
            .join(Contact, Contact.id == WebinarContactMembership.contact_id)
            .where(*overlap_conds)
            .group_by(Contact.bucket_id)
        )

    # When a country/employee filter is active, also return a per-bucket TOTAL
    # that respects the SAME filter (all matching contacts, not just fresh) so the
    # assign panel's Total column tracks the filter alongside Remaining. Skipped
    # when no filter is set: an unfiltered GROUP BY over the whole contacts table
    # is the exact aggregate list_buckets deliberately avoids for performance —
    # the client uses the static bucket.total_contacts in that case.
    totals_stmt = None
    if country_cond is not None or emp_conds:
        total_conds = [
            Contact.user_id == LLOYD_USER_ID,
            Contact.bucket_id.isnot(None),
            ~Contact.is_blocklisted,
        ]
        if country_cond is not None:
            total_conds.append(country_cond)
        total_conds.extend(emp_conds)
        totals_stmt = (
            select(Contact.bucket_id, sa_func.count())
            .where(*total_conds)
            .group_by(Contact.bucket_id)
        )

    tasks = [_grouped(counts_stmt)]
    if overlap_stmt is not None:
        tasks.append(_grouped(overlap_stmt))
    if totals_stmt is not None:
        tasks.append(_grouped(totals_stmt))
    results = await _asyncio.gather(*tasks)

    counts = results[0]
    idx = 1
    if overlap_stmt is not None:
        for _bid, _cnt in results[idx].items():
            if _bid in counts:
                counts[_bid] = max(0, counts[_bid] - _cnt)
        idx += 1
    totals: dict = results[idx] if totals_stmt is not None else {}

    resp = {"buckets": counts, "total": sum(counts.values()), "totals": totals}
    _ELIGIBLE_CACHE[cache_key] = (_time.monotonic(), resp)
    return resp


# Country-name buckets for the Good-Available geo split. Matched case-insensitively
# on a letters-only normalization of the effective location, so dirty values like
# "'United States']" still classify. Turkey/Russia are intentionally left out of
# Europe (transcontinental) — adjust here if you want them counted.
_GOOD_GEO_US_CA = {
    "united states", "united states of america", "usa", "us", "u s a", "u s",
    "america", "canada",
}
_GOOD_GEO_EUROPE = {
    "united kingdom", "uk", "great britain", "england", "scotland", "wales",
    "northern ireland", "ireland", "germany", "france", "netherlands",
    "the netherlands", "italy", "spain", "sweden", "switzerland", "belgium",
    "poland", "finland", "austria", "denmark", "norway", "portugal",
    "czech republic", "czechia", "greece", "hungary", "romania", "slovakia",
    "slovenia", "croatia", "bulgaria", "lithuania", "latvia", "estonia",
    "luxembourg", "iceland", "malta", "cyprus", "ukraine", "serbia",
    "liechtenstein", "monaco", "andorra", "san marino",
}


def _norm_location(s: str | None) -> str:
    """Lowercase, keep only letters/spaces, collapse whitespace — repairs dirty
    country values (e.g. "'United States']" -> "united states") for classifying."""
    if not s:
        return ""
    import re
    cleaned = re.sub(r"[^a-z ]", " ", s.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


@router.get("/buckets/good-available")
async def good_available_counts(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Fresh 'ideal' inventory for the Planning header.

    Counts claimable contacts in good/medium/unmarked buckets (excludes only
    'bad' quality and the 'disqualified' bucket), with each bucket's saved
    Statistics→Segments employee range applied where set — unknown-size contacts
    are excluded when a range is set, and there's no size restriction when a
    bucket has no range. 'Fresh' = never invited and not in-flight
    (last_invited_at IS NULL, assigned_membership_count = 0), the same baseline
    the bucket 'remaining' uses; blocklisted contacts are excluded.

    Returns the total plus geo splits (US+Canada, Europe, no-location). The three
    splits are subsets of the total — the rest of the world (APAC/LATAM/etc.) is
    in the total but in none of the three splits.
    """
    # Per-bucket employee range: apply the saved range where set (excluding
    # unknown-size contacts), otherwise no size restriction.
    emp_ok = or_(
        and_(OutreachBucket.stat_emp_min.is_(None), OutreachBucket.stat_emp_max.is_(None)),
        and_(
            Contact.employee_count.isnot(None),
            or_(OutreachBucket.stat_emp_min.is_(None), Contact.employee_count >= OutreachBucket.stat_emp_min),
            or_(OutreachBucket.stat_emp_max.is_(None), Contact.employee_count <= OutreachBucket.stat_emp_max),
        ),
    )
    # Effective location: per-contact country, else list-level location.
    loc_expr = sa_func.coalesce(
        sa_func.nullif(sa_func.trim(Contact.country), ""),
        sa_func.nullif(sa_func.trim(Contact.list_location), ""),
    )
    result = await db.execute(
        select(loc_expr.label("loc"), sa_func.count())
        .select_from(Contact)
        .join(OutreachBucket, OutreachBucket.id == Contact.bucket_id)
        .where(
            Contact.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
            # good + medium + unmarked → exclude only 'bad'
            or_(OutreachBucket.quality.is_(None), OutreachBucket.quality != "bad"),
            sa_func.lower(OutreachBucket.name) != "disqualified",
            # NOT form matches ix_contacts_claimable's partial predicate.
            ~Contact.is_blocklisted,
            Contact.last_invited_at.is_(None),
            Contact.assigned_membership_count == 0,
            emp_ok,
        )
        .group_by(loc_expr)
    )

    total = us_ca = europe = no_location = 0
    for loc, cnt in result:
        cnt = int(cnt or 0)
        total += cnt
        n = _norm_location(loc)
        if not n:
            no_location += cnt
        elif n in _GOOD_GEO_US_CA:
            us_ca += cnt
        elif n in _GOOD_GEO_EUROPE:
            europe += cnt
    return {"total": total, "us_ca": us_ca, "europe": europe, "no_location": no_location}


@router.post("/buckets", status_code=201)
async def create_bucket(
    body: BucketCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    existing = await db.execute(
        select(OutreachBucket).where(
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.name == body.name,
            OutreachBucket.deleted_at.is_(None),
        )
    )
    bucket = existing.scalar_one_or_none()
    if bucket:
        bucket.total_contacts += body.total_contacts
        bucket.remaining_contacts += (body.remaining_contacts or body.total_contacts)
        if body.countries:
            existing_countries = set(bucket.countries or [])
            existing_countries.update(body.countries)
            bucket.countries = list(existing_countries)
        if body.emp_range and not bucket.emp_range:
            bucket.emp_range = body.emp_range
        if body.industry and not bucket.industry:
            bucket.industry = body.industry
    else:
        bucket = OutreachBucket(
            user_id=LLOYD_USER_ID,
            name=body.name,
            industry=body.industry,
            total_contacts=body.total_contacts,
            remaining_contacts=body.remaining_contacts or body.total_contacts,
            countries=body.countries,
            emp_range=body.emp_range,
            source_file=body.source_file,
        )
        db.add(bucket)
    await db.flush()
    return bucket_dict(bucket)


@router.put("/buckets/{bucket_id}")
async def update_bucket(
    bucket_id: str,
    body: BucketUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(OutreachBucket).where(OutreachBucket.id == bucket_id, OutreachBucket.user_id == LLOYD_USER_ID)
    )
    bucket = result.scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    updates = body.model_dump(exclude_unset=True)

    # Pre-check the (user_id, name) uniqueness so we surface a friendly 409
    # instead of a 500 from the IntegrityError. Only checks among non-deleted
    # rows since soft-deleted buckets keep their old names but don't block
    # reuse from the operator's perspective.
    new_name = updates.get("name")
    if new_name is not None and new_name != bucket.name:
        clash = await db.execute(
            select(OutreachBucket.id).where(
                OutreachBucket.user_id == LLOYD_USER_ID,
                OutreachBucket.name == new_name,
                OutreachBucket.id != bucket_id,
                OutreachBucket.deleted_at.is_(None),
            )
        )
        if clash.scalar_one_or_none():
            raise HTTPException(409, f"A bucket named '{new_name}' already exists.")

    # Pre-check the employee-range invariant (mirrors the name-clash pre-check
    # above) so an inverted range returns a friendly 400 instead of a 500 from
    # the ck_outreach_buckets_stat_emp_range IntegrityError. Resolve against the
    # bucket's current values since either bound may be omitted from this update.
    eff_emp_min = updates["stat_emp_min"] if "stat_emp_min" in updates else bucket.stat_emp_min
    eff_emp_max = updates["stat_emp_max"] if "stat_emp_max" in updates else bucket.stat_emp_max
    if eff_emp_min is not None and eff_emp_max is not None and eff_emp_min > eff_emp_max:
        raise HTTPException(400, "Employee-count min must be less than or equal to max.")

    for field, val in updates.items():
        setattr(bucket, field, val)
    await db.flush()
    return bucket_dict(bucket)


# ═══════════════════════════════════════════════════════════════════════════
# BUCKET COPIES
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/buckets/{bucket_id}/copies")
async def get_bucket_copies(
    bucket_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(BucketCopy).where(
            BucketCopy.bucket_id == bucket_id,
            BucketCopy.user_id == LLOYD_USER_ID,
            BucketCopy.deleted_at.is_(None),
        ).order_by(BucketCopy.copy_type, BucketCopy.variant_index)
    )
    copies = result.scalars().all()
    titles = [copy_dict(c) for c in copies if c.copy_type == "title"]
    descriptions = [copy_dict(c) for c in copies if c.copy_type == "description"]
    return {"bucket_id": bucket_id, "titles": titles, "descriptions": descriptions}


@router.post("/buckets/{bucket_id}/copies", status_code=201)
async def create_copy(
    bucket_id: str,
    body: CopyCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(OutreachBucket)
        .where(OutreachBucket.id == bucket_id, OutreachBucket.user_id == LLOYD_USER_ID)
        .with_for_update()
    )
    bucket = result.scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    max_idx_result = await db.execute(
        select(sa_func.max(BucketCopy.variant_index)).where(
            BucketCopy.bucket_id == bucket_id,
            BucketCopy.copy_type == body.copy_type,
        )
    )
    max_idx = max_idx_result.scalar()
    next_idx = (max_idx + 1) if max_idx is not None else 0

    copy = BucketCopy(
        user_id=LLOYD_USER_ID,
        bucket_id=bucket_id,
        copy_type=body.copy_type,
        variant_index=next_idx,
        text=body.text,
        is_primary=False,
    )
    db.add(copy)
    await db.flush()
    return copy_dict(copy)


@router.post("/buckets/{bucket_id}/copies/generate", status_code=201)
async def generate_copies(
    bucket_id: str,
    body: CopyGenerateRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(OutreachBucket)
        .where(OutreachBucket.id == bucket_id, OutreachBucket.user_id == LLOYD_USER_ID)
        .with_for_update()
    )
    bucket = result.scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")

    batch_id = str(uuid.uuid4())
    generated_titles = []
    generated_descs = []

    types_to_gen = []
    if body.copy_type in ("title", "both"):
        types_to_gen.append("title")
    if body.copy_type in ("description", "both"):
        types_to_gen.append("description")

    for copy_type in types_to_gen:
        # Un-primary old copies
        old_copies = await db.execute(
            select(BucketCopy).where(
                BucketCopy.bucket_id == bucket_id,
                BucketCopy.copy_type == copy_type,
                BucketCopy.deleted_at.is_(None),
            )
        )
        for old in old_copies.scalars().all():
            old.is_primary = False

        # Get max variant_index so new copies continue the sequence
        max_idx_result = await db.execute(
            select(sa_func.max(BucketCopy.variant_index)).where(
                BucketCopy.bucket_id == bucket_id,
                BucketCopy.copy_type == copy_type,
            )
        )
        max_idx = max_idx_result.scalar()
        next_start = (max_idx + 1) if max_idx is not None else 0

        # Generate copies via AI brain
        try:
            texts = await generate_bucket_copies(
                db=db,
                user_id=LLOYD_USER_ID,
                bucket_name=bucket.name,
                industry=bucket.industry,
                countries=bucket.countries,
                emp_range=bucket.emp_range,
                copy_type=copy_type,
                count=body.variant_count,
            )
        except ValueError as e:
            logger.error("AI generation failed for bucket %s: %s", bucket.name, e)
            raise HTTPException(422, f"Generation failed: {e}")

        for i, text in enumerate(texts):
            copy = BucketCopy(
                user_id=LLOYD_USER_ID,
                bucket_id=bucket_id,
                copy_type=copy_type,
                variant_index=next_start + i,
                text=text,
                is_primary=(i == 0),
                generation_batch_id=batch_id,
            )
            db.add(copy)
            if copy_type == "title":
                generated_titles.append(copy)
            else:
                generated_descs.append(copy)

    await db.flush()

    return {
        "bucket_id": bucket_id,
        "batch_id": batch_id,
        "titles": [copy_dict(c) for c in generated_titles],
        "descriptions": [copy_dict(c) for c in generated_descs],
    }


@router.put("/copies/{copy_id}")
async def update_copy(
    copy_id: str,
    body: CopyUpdate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(BucketCopy).where(BucketCopy.id == copy_id, BucketCopy.user_id == LLOYD_USER_ID)
    )
    copy = result.scalar_one_or_none()
    if not copy:
        raise HTTPException(404, "Copy not found")

    if body.text is not None:
        copy.text = body.text

    if body.is_primary is True:
        await db.execute(
            update(BucketCopy).where(
                BucketCopy.bucket_id == copy.bucket_id,
                BucketCopy.copy_type == copy.copy_type,
                BucketCopy.id != copy_id,
                BucketCopy.deleted_at.is_(None),
            ).values(is_primary=False, primary_picked_by_user=False)
        )
        copy.is_primary = True
        copy.primary_picked_by_user = True

    await db.flush()
    return copy_dict(copy)


@router.post("/copies/{copy_id}/regenerate", status_code=201)
async def regenerate_copy(
    copy_id: str,
    body: CopyRegenerateRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(BucketCopy).where(BucketCopy.id == copy_id, BucketCopy.user_id == LLOYD_USER_ID)
    )
    original = result.scalar_one_or_none()
    if not original:
        raise HTTPException(404, "Copy not found")

    original.ai_feedback = body.feedback

    bucket_result = await db.execute(
        select(OutreachBucket).where(OutreachBucket.id == original.bucket_id).with_for_update()
    )
    bucket = bucket_result.scalar_one_or_none()

    max_idx_result = await db.execute(
        select(sa_func.max(BucketCopy.variant_index)).where(
            BucketCopy.bucket_id == original.bucket_id,
            BucketCopy.copy_type == original.copy_type,
        )
    )
    max_idx = max_idx_result.scalar()
    next_idx = (max_idx + 1) if max_idx is not None else 0

    # Regenerate via AI brain with feedback
    try:
        text = await regenerate_bucket_copy(
            db=db,
            user_id=LLOYD_USER_ID,
            original_text=original.text,
            copy_type=original.copy_type,
            feedback=body.feedback,
            bucket_name=bucket.name if bucket else "Unknown",
            industry=bucket.industry if bucket else None,
        )
    except ValueError as e:
        logger.error("AI regeneration failed: %s", e)
        raise HTTPException(422, f"Regeneration failed: {e}")

    new_copy = BucketCopy(
        user_id=LLOYD_USER_ID,
        bucket_id=original.bucket_id,
        copy_type=original.copy_type,
        variant_index=next_idx,
        text=text,
        is_primary=False,
        ai_feedback=body.feedback,
        generation_batch_id=original.generation_batch_id,
    )
    db.add(new_copy)
    await db.flush()
    return copy_dict(new_copy)


@router.delete("/copies/{copy_id}", status_code=204)
async def delete_copy(
    copy_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(BucketCopy).where(BucketCopy.id == copy_id, BucketCopy.user_id == LLOYD_USER_ID)
    )
    copy = result.scalar_one_or_none()
    if not copy:
        raise HTTPException(404, "Copy not found")

    was_primary = copy.is_primary
    copy.deleted_at = datetime.utcnow()
    copy.is_primary = False

    if was_primary:
        next_result = await db.execute(
            select(BucketCopy).where(
                BucketCopy.bucket_id == copy.bucket_id,
                BucketCopy.copy_type == copy.copy_type,
                BucketCopy.id != copy_id,
                BucketCopy.deleted_at.is_(None),
            ).order_by(BucketCopy.variant_index).limit(1)
        )
        next_copy = next_result.scalar_one_or_none()
        if next_copy:
            next_copy.is_primary = True

    await db.flush()


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND COPY GENERATION
# Survives browser navigation — work continues server-side after the HTTP
# response is returned. Frontend polls status instead of awaiting.
# ═══════════════════════════════════════════════════════════════════════════


def _friendly_generation_error(exc: Exception) -> str:
    """Map a raw generation exception to a concise, user-facing message.

    Classifies by exception name / HTTP status so we can surface something
    actionable (e.g. a rotated API key) instead of a raw stack-trace string.
    """
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    raw = str(exc).strip()

    if name == "AuthenticationError" or status == 401:
        return ("AI service authentication failed — the API key is invalid or has "
                "been rotated. Update ANTHROPIC_API_KEY, then retry.")
    if name == "PermissionDeniedError" or status == 403:
        return ("AI service denied the request (403) — the API key may lack access "
                "to the model. Check the key's permissions, then retry.")
    if name == "RateLimitError" or status == 429:
        return ("AI service is rate-limited (429) — too many requests. "
                "Wait a moment and retry.")
    if name in ("APIConnectionError", "APITimeoutError"):
        return ("Could not reach the AI service — a network or timeout error "
                "occurred. Retry in a moment.")
    if isinstance(status, int) and 500 <= status < 600:
        return (f"AI service error ({status}) — the provider is temporarily "
                "unavailable. Retry shortly.")
    if raw.startswith("Model returned invalid JSON") or raw.startswith("Model did not return"):
        return "The AI returned an unexpected response. Retry to generate again."
    return raw[:300] if raw else "Copy generation failed for an unknown reason."


async def _run_single_copy_generation_job(job_id: str) -> None:
    """Execute one copy-generation job. Uses its own DB session."""
    async with AsyncSessionLocal() as db:
        try:
            job_result = await db.execute(
                select(BucketCopyGenerationJob).where(BucketCopyGenerationJob.id == job_id)
            )
            job = job_result.scalar_one_or_none()
            if not job:
                logger.warning("Copy generation job %s not found", job_id)
                return

            # Mark generating
            job.status = "generating"
            job.started_at = datetime.utcnow()
            job.error_message = None
            await db.commit()

            bucket_result = await db.execute(
                select(OutreachBucket).where(
                    OutreachBucket.id == job.bucket_id,
                    OutreachBucket.user_id == job.user_id,
                ).with_for_update()
            )
            bucket = bucket_result.scalar_one_or_none()
            if not bucket:
                job.status = "failed"
                job.error_message = "Bucket not found"
                job.completed_at = datetime.utcnow()
                await db.commit()
                return

            # Un-primary old copies of this type
            old_copies = await db.execute(
                select(BucketCopy).where(
                    BucketCopy.bucket_id == job.bucket_id,
                    BucketCopy.copy_type == job.copy_type,
                    BucketCopy.deleted_at.is_(None),
                )
            )
            for old in old_copies.scalars().all():
                old.is_primary = False

            # Continue variant_index sequence (avoid duplicate V-numbers)
            max_idx_result = await db.execute(
                select(sa_func.max(BucketCopy.variant_index)).where(
                    BucketCopy.bucket_id == job.bucket_id,
                    BucketCopy.copy_type == job.copy_type,
                )
            )
            max_idx = max_idx_result.scalar()
            next_start = (max_idx + 1) if max_idx is not None else 0
            is_first_ever = max_idx is None

            texts = await generate_bucket_copies(
                db=db,
                user_id=job.user_id,
                bucket_name=bucket.name,
                industry=bucket.industry,
                countries=bucket.countries,
                emp_range=bucket.emp_range,
                copy_type=job.copy_type,
                count=job.variant_count,
            )

            batch_id = str(uuid.uuid4())
            for i, text in enumerate(texts):
                db.add(BucketCopy(
                    user_id=job.user_id,
                    bucket_id=job.bucket_id,
                    copy_type=job.copy_type,
                    variant_index=next_start + i,
                    text=text,
                    is_primary=(is_first_ever and i == 0),
                    generation_batch_id=batch_id,
                ))

            job.status = "done"
            job.completed_at = datetime.utcnow()
            await db.commit()
        except Exception as exc:
            logger.exception("Copy generation job %s failed", job_id)
            try:
                await db.rollback()
                fail_result = await db.execute(
                    select(BucketCopyGenerationJob).where(BucketCopyGenerationJob.id == job_id)
                )
                job = fail_result.scalar_one_or_none()
                if job:
                    job.status = "failed"
                    job.error_message = _friendly_generation_error(exc)
                    job.completed_at = datetime.utcnow()
                    await db.commit()
            except Exception:
                logger.exception("Failed to mark job %s as failed", job_id)
        finally:
            _active_copy_gen_tasks.pop(job_id, None)


def _spawn_copy_generation_job(job_id: str) -> None:
    """Fire-and-forget: runs the job in a detached task."""
    task = asyncio.create_task(_run_single_copy_generation_job(job_id))
    _active_copy_gen_tasks[job_id] = task


@router.post("/buckets/copies/generate-bulk", status_code=202)
async def generate_copies_bulk(
    body: CopyBulkGenerateRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Kick off background copy generation for one or more buckets.

    Returns immediately with created job IDs. Poll /generation-status to
    track progress. Work continues server-side regardless of client.
    """
    if not body.bucket_ids:
        raise HTTPException(400, "bucket_ids is required")

    # Validate buckets belong to user
    b_result = await db.execute(
        select(OutreachBucket.id).where(
            OutreachBucket.id.in_(body.bucket_ids),
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
        )
    )
    valid_bucket_ids = {row[0] for row in b_result.all()}
    if not valid_bucket_ids:
        raise HTTPException(404, "No valid buckets found")

    types_to_gen = []
    if body.copy_type in ("title", "both"):
        types_to_gen.append("title")
    if body.copy_type in ("description", "both"):
        types_to_gen.append("description")

    created_jobs: list[BucketCopyGenerationJob] = []
    for bucket_id in valid_bucket_ids:
        for ctype in types_to_gen:
            # If there's already a live job for this (bucket, type), skip
            existing = await db.execute(
                select(BucketCopyGenerationJob).where(
                    BucketCopyGenerationJob.bucket_id == bucket_id,
                    BucketCopyGenerationJob.copy_type == ctype,
                    BucketCopyGenerationJob.status.in_(("pending", "generating")),
                )
            )
            if existing.scalar_one_or_none():
                continue

            job = BucketCopyGenerationJob(
                user_id=LLOYD_USER_ID,
                bucket_id=bucket_id,
                copy_type=ctype,
                variant_count=body.variant_count,
                status="pending",
            )
            db.add(job)
            created_jobs.append(job)

    await db.flush()
    # Commit now so the background task can see the job rows on its own session
    await db.commit()

    for job in created_jobs:
        _spawn_copy_generation_job(job.id)

    return {
        "jobs": [
            {
                "id": j.id,
                "bucket_id": j.bucket_id,
                "copy_type": j.copy_type,
                "status": j.status,
            } for j in created_jobs
        ],
    }


@router.get("/buckets/copies/generation-status")
async def get_copy_generation_status(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Return the latest generation job per (bucket, copy_type).

    Used by the frontend to restore status badges after navigation and to
    poll progress during active generation.
    """
    # Latest job per (bucket_id, copy_type) via window function — but
    # simpler: fetch all recent and dedupe in Python.
    result = await db.execute(
        select(BucketCopyGenerationJob)
        .where(BucketCopyGenerationJob.user_id == LLOYD_USER_ID)
        .order_by(BucketCopyGenerationJob.created_at.desc())
    )
    rows = result.scalars().all()

    latest: dict[tuple[str, str], BucketCopyGenerationJob] = {}
    for j in rows:
        key = (j.bucket_id, j.copy_type)
        if key not in latest:
            latest[key] = j

    return {
        "jobs": [
            {
                "id": j.id,
                "bucket_id": j.bucket_id,
                "copy_type": j.copy_type,
                "status": j.status,
                "error_message": j.error_message,
                "variant_count": j.variant_count,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            }
            for j in latest.values()
        ],
    }


@router.post("/buckets/copies/generation-jobs/{job_id}/retry", status_code=202)
async def retry_copy_generation_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Retry a failed generation job.

    Creates a new job row with the same (bucket, copy_type, variant_count)
    and kicks off the background task. Keeps the old row for audit.
    """
    result = await db.execute(
        select(BucketCopyGenerationJob).where(
            BucketCopyGenerationJob.id == job_id,
            BucketCopyGenerationJob.user_id == LLOYD_USER_ID,
        )
    )
    old_job = result.scalar_one_or_none()
    if not old_job:
        raise HTTPException(404, "Job not found")
    if old_job.status in ("pending", "generating"):
        raise HTTPException(409, "Job is still running")

    new_job = BucketCopyGenerationJob(
        user_id=LLOYD_USER_ID,
        bucket_id=old_job.bucket_id,
        copy_type=old_job.copy_type,
        variant_count=old_job.variant_count,
        status="pending",
    )
    db.add(new_job)
    await db.flush()
    await db.commit()

    _spawn_copy_generation_job(new_job.id)

    return {
        "id": new_job.id,
        "bucket_id": new_job.bucket_id,
        "copy_type": new_job.copy_type,
        "status": new_job.status,
    }


# ═══════════════════════════════════════════════════════════════════════════
# BUCKET MERGE
# Move all contacts from N source buckets into a single keeper bucket.
# Refuses if any source has webinar assignments (would orphan copy refs).
# Future imports with a source bucket's name redirect to the keeper via
# `merged_into_bucket_id`.
# ═══════════════════════════════════════════════════════════════════════════


@router.post("/buckets/merge", status_code=200)
async def merge_buckets(
    body: BucketMergeRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if not body.source_bucket_ids:
        raise HTTPException(400, "source_bucket_ids is required")
    if body.keeper_bucket_id in body.source_bucket_ids:
        raise HTTPException(400, "keeper_bucket_id cannot also be a source")

    # Validate keeper
    keeper_result = await db.execute(
        select(OutreachBucket).where(
            OutreachBucket.id == body.keeper_bucket_id,
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
        )
    )
    keeper = keeper_result.scalar_one_or_none()
    if not keeper:
        raise HTTPException(404, "Keeper bucket not found")

    # Validate all sources
    src_result = await db.execute(
        select(OutreachBucket).where(
            OutreachBucket.id.in_(body.source_bucket_ids),
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
        )
    )
    sources = src_result.scalars().all()
    if len(sources) != len(set(body.source_bucket_ids)):
        raise HTTPException(404, "One or more source buckets not found")

    # Refuse if any source has webinar assignments
    src_ids = [s.id for s in sources]
    assign_result = await db.execute(
        select(
            WebinarListAssignment.bucket_id,
            sa_func.count().label("n"),
        )
        .where(WebinarListAssignment.bucket_id.in_(src_ids))
        .group_by(WebinarListAssignment.bucket_id)
    )
    blocking = {row.bucket_id: row.n for row in assign_result}
    if blocking:
        name_by_id = {s.id: s.name for s in sources}
        raise HTTPException(
            409,
            detail={
                "message": "One or more buckets have webinar assignments and cannot be merged.",
                "blocking_buckets": [
                    {"id": bid, "name": name_by_id.get(bid, "Unknown"), "assignment_count": n}
                    for bid, n in blocking.items()
                ],
            },
        )

    now = datetime.utcnow()

    # Move contacts to the keeper
    contacts_moved_result = await db.execute(
        update(Contact)
        .where(Contact.bucket_id.in_(src_ids), Contact.user_id == LLOYD_USER_ID)
        .values(bucket_id=keeper.id)
    )
    contacts_moved = contacts_moved_result.rowcount or 0

    # Repoint any membership rows whose source-bucket snapshot was a merged
    # bucket, so per-bucket membership reads follow the moved contacts.
    await db.execute(
        update(WebinarContactMembership)
        .where(
            WebinarContactMembership.bucket_id.in_(src_ids),
            WebinarContactMembership.user_id == LLOYD_USER_ID,
        )
        .values(bucket_id=keeper.id)
    )

    # Soft-delete source copies
    await db.execute(
        update(BucketCopy)
        .where(BucketCopy.bucket_id.in_(src_ids), BucketCopy.deleted_at.is_(None))
        .values(deleted_at=now, is_primary=False)
    )

    # Point sources at the keeper and soft-delete them
    await db.execute(
        update(OutreachBucket)
        .where(OutreachBucket.id.in_(src_ids))
        .values(merged_into_bucket_id=keeper.id, deleted_at=now)
    )

    await db.flush()

    # Recompute keeper's counts from contacts table (fresh baseline for remaining)
    count_result = await db.execute(
        select(
            sa_func.count().label("total"),
            sa_func.count().filter(and_(
                Contact.last_invited_at.is_(None),
                Contact.assigned_membership_count == 0,
            )).label("available"),
        ).where(Contact.bucket_id == keeper.id)
    )
    row = count_result.one()
    keeper.total_contacts = row.total or 0
    keeper.remaining_contacts = row.available or 0
    await db.flush()

    return {
        "keeper_bucket_id": keeper.id,
        "keeper_name": keeper.name,
        "contacts_moved": contacts_moved,
        "merged_bucket_ids": src_ids,
        "merged_bucket_count": len(src_ids),
        "keeper_total_contacts": keeper.total_contacts,
        "keeper_remaining_contacts": keeper.remaining_contacts,
    }


# ═══════════════════════════════════════════════════════════════════════════
# BUCKET CONTACTS (view + export by bucket)
# ═══════════════════════════════════════════════════════════════════════════

# Buckets can hold tens of thousands of contacts; paginate the UI fetch. The CSV
# export below streams with no page limit.
_BUCKET_CONTACTS_DEFAULT_LIMIT = 1000
_BUCKET_CONTACTS_MAX_LIMIT = 5000


def _bucket_contacts_conditions(bucket_id: str, scope: str):
    """WHERE clauses matching the bucket's displayed counts (see merge recount):
    `total` = every contact in the bucket; `remaining` = the available ones."""
    conds = [Contact.bucket_id == bucket_id, Contact.user_id == LLOYD_USER_ID]
    if scope == "remaining":
        # Fresh baseline — matches the bucket's displayed "remaining".
        conds.append(Contact.last_invited_at.is_(None))
        conds.append(Contact.assigned_membership_count == 0)
    return conds


async def _load_bucket_or_404(db: AsyncSession, bucket_id: str) -> OutreachBucket:
    bucket = (await db.execute(
        select(OutreachBucket).where(
            OutreachBucket.id == bucket_id,
            OutreachBucket.user_id == LLOYD_USER_ID,
            OutreachBucket.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if not bucket:
        raise HTTPException(404, "Bucket not found")
    return bucket


@router.get("/buckets/{bucket_id}/contacts")
async def get_bucket_contacts(
    bucket_id: str,
    scope: str = Query("total", regex="^(total|remaining)$"),
    limit: int = Query(_BUCKET_CONTACTS_DEFAULT_LIMIT, ge=1, le=_BUCKET_CONTACTS_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    bucket = await _load_bucket_or_404(db, bucket_id)
    conds = _bucket_contacts_conditions(bucket_id, scope)

    total = (await db.execute(select(sa_func.count()).where(*conds))).scalar_one()

    rows = (await db.execute(
        select(Contact.id, Contact.first_name, Contact.last_name, Contact.email)
        .where(*conds)
        .order_by(Contact.first_name, Contact.email)
        .limit(limit)
        .offset(offset)
    )).all()

    return {
        "bucket": {"id": bucket.id, "name": bucket.name, "scope": scope},
        "contacts": [
            {"id": r.id, "first_name": r.first_name, "last_name": r.last_name, "email": r.email}
            for r in rows
        ],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(rows),
            "filtered_total": int(total or 0),
        },
    }


@router.get("/buckets/{bucket_id}/contacts.csv")
async def stream_bucket_contacts_csv(
    bucket_id: str,
    scope: str = Query("total", regex="^(total|remaining)$"),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    bucket = await _load_bucket_or_404(db, bucket_id)
    conds = _bucket_contacts_conditions(bucket_id, scope)

    async def row_iter():
        header_buf = io.StringIO()
        csv.writer(header_buf).writerow(["first_name", "last_name", "email"])
        yield header_buf.getvalue()

        q = (
            select(Contact.first_name, Contact.last_name, Contact.email)
            .where(*conds)
            .order_by(Contact.first_name, Contact.email)
            .execution_options(yield_per=2000)
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        stream = await db.stream(q)
        async for first_name, last_name, email in stream:
            writer.writerow([first_name or "", last_name or "", email or ""])
            # Flush in ~64KB chunks so bytes leave the server steadily.
            if buf.tell() > 64 * 1024:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        if buf.tell() > 0:
            yield buf.getvalue()

    safe = "".join(ch for ch in (bucket.name or "bucket") if ch.isalnum() or ch in (" ", "-", "_")).strip().replace(" ", "_") or "bucket"
    filename = f"{safe}_{scope}.csv"
    return StreamingResponse(
        row_iter(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
