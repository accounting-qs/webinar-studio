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
from sqlalchemy import select, func as sa_func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.auth import require_auth
from api.routers.outreach._helpers import (
    LLOYD_USER_ID, bucket_dict, compute_blocklist_counts_per_bucket, copy_dict,
)
from api.schemas import (
    BucketCreate, BucketMergeRequest, BucketUpdate, CopyBulkGenerateRequest,
    CopyCreate, CopyGenerateRequest, CopyRegenerateRequest, CopyUpdate,
)
from db.models import (
    BucketCopy, BucketCopyGenerationJob, Contact, OutreachBucket,
    WebinarListAssignment,
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

    # Compute actual total/remaining from contacts table
    bucket_ids = [b.id for b in buckets]
    if bucket_ids:
        count_result = await db.execute(
            select(
                Contact.bucket_id,
                sa_func.count().label("total"),
                sa_func.count().filter(Contact.outreach_status == "available").label("available"),
            )
            .where(Contact.bucket_id.in_(bucket_ids))
            .group_by(Contact.bucket_id)
        )
        count_map = {row.bucket_id: (row.total, row.available) for row in count_result}

        # Sync stored counters with actual counts
        for b in buckets:
            total, available = count_map.get(b.id, (0, 0))
            if b.total_contacts != total or b.remaining_contacts != available:
                b.total_contacts = total
                b.remaining_contacts = available
        await db.flush()

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
                    job.error_message = str(exc)[:500]
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

    # Recompute keeper's counts from contacts table
    count_result = await db.execute(
        select(
            sa_func.count().label("total"),
            sa_func.count().filter(Contact.outreach_status == "available").label("available"),
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
        conds.append(Contact.outreach_status == "available")
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
