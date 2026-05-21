"""Outreach sub-router: CSV Uploads + Background Import."""
import asyncio
import csv
import io
import os
import traceback
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func as sa_func, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy import pool

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from api.schemas import ImportStartCreate
from db.models import UploadHistory, ContactCustomField, Contact, OutreachBucket
from db.session import get_db

router = APIRouter()

# Supabase Storage config
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CSV_BUCKET = "csv-uploads"

# Database URL for background tasks (can't share request-scoped sessions)
_BG_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _BG_DATABASE_URL.startswith("postgres://"):
    _BG_DATABASE_URL = _BG_DATABASE_URL.replace("postgres://", "postgresql://", 1)
if "postgresql+asyncpg://" not in _BG_DATABASE_URL:
    _BG_DATABASE_URL = _BG_DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

_bg_engine = create_async_engine(_BG_DATABASE_URL, poolclass=pool.NullPool) if _BG_DATABASE_URL else None

# Store background task references so they aren't garbage-collected
_active_import_tasks: dict[str, asyncio.Task] = {}

# Import control: pause/cancel state per upload_id
_import_pause_events: dict[str, asyncio.Event] = {}   # set=running, clear=paused
_import_cancel_flags: dict[str, bool] = {}


def _get_supabase_base_url() -> str:
    base_url = SUPABASE_URL.strip()
    if not base_url:
        raise HTTPException(500, "Supabase storage is not configured: missing SUPABASE_URL")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/")


def _get_supabase_service_key() -> str:
    service_key = SUPABASE_SERVICE_KEY.strip()
    if not service_key:
        raise HTTPException(500, "Supabase storage is not configured: missing SUPABASE_SERVICE_KEY")
    return service_key


def _supabase_headers(**extra_headers: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {_get_supabase_service_key()}"}
    headers.update(extra_headers)
    return headers


def _storage_api_url(path: str) -> str:
    return f"{_get_supabase_base_url()}{path}"


def _parse_csv_line(line: str) -> list[str]:
    """Parse a single CSV line handling quoted fields and escaped quotes ("")."""
    reader = csv.reader(io.StringIO(line))
    for row in reader:
        return [cell.strip() for cell in row]
    return []


@router.get("/uploads")
async def list_uploads(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.user_id == LLOYD_USER_ID)
        .order_by(UploadHistory.created_at.desc())
    )
    uploads = result.scalars().all()
    return {
        "uploads": [
            {
                "id": u.id,
                "file_name": u.file_name,
                "total_contacts": u.total_contacts,
                "total_buckets": u.total_buckets,
                "bucket_summary": u.bucket_summary,
                "status": u.status,
                "progress": u.progress,
                "processed_rows": u.processed_rows,
                "inserted_count": u.inserted_count,
                "skipped_count": u.skipped_count,
                "overwritten_count": u.overwritten_count,
                "error_message": u.error_message,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in uploads
        ]
    }


@router.get("/uploads/custom-lists")
async def list_custom_lists(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """List completed custom-list uploads with available contact counts."""
    result = await db.execute(
        select(UploadHistory).where(
            UploadHistory.user_id == LLOYD_USER_ID,
            UploadHistory.upload_mode == "custom_list",
            UploadHistory.status == "complete",
        ).order_by(UploadHistory.created_at.desc())
    )
    uploads = result.scalars().all()
    upload_ids = [u.id for u in uploads]

    # Single GROUP BY query instead of 2*N round-trips
    count_map: dict[str, tuple[int, int]] = {}
    if upload_ids:
        count_result = await db.execute(
            select(
                Contact.upload_id,
                sa_func.count().label("total"),
                sa_func.count().filter(Contact.outreach_status == "available").label("available"),
            )
            .where(Contact.upload_id.in_(upload_ids))
            .group_by(Contact.upload_id)
        )
        count_map = {row.upload_id: (row.total, row.available) for row in count_result}

    lists = []
    for u in uploads:
        total, available = count_map.get(u.id, (0, 0))
        lists.append({
            "id": u.id,
            "name": u.custom_list_name or u.file_name,
            "total_contacts": total,
            "available_contacts": available,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })

    return {"lists": lists}


@router.post("/uploads/{upload_id}/copies/generate", status_code=201)
async def generate_custom_list_copies(
    upload_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Generate AI title/description copies for a custom list using its name as context."""
    from db.models import BucketCopy
    from services.generation import generate_bucket_copies

    result = await db.execute(
        select(UploadHistory).where(
            UploadHistory.id == upload_id,
            UploadHistory.user_id == LLOYD_USER_ID,
            UploadHistory.upload_mode == "custom_list",
        )
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Custom list not found")

    copy_type = body.get("copy_type", "both")
    variant_count = body.get("variant_count", 3)
    list_name = upload.custom_list_name or upload.file_name
    batch_id = str(uuid.uuid4())

    generated_titles = []
    generated_descriptions = []

    for ct in (["title", "description"] if copy_type == "both" else [copy_type]):
        # Use the custom list name as the AI prompt context
        texts = await generate_bucket_copies(
            db=db,
            user_id=LLOYD_USER_ID,
            bucket_name=list_name,
            industry=None,
            countries=None,
            emp_range=None,
            copy_type=ct,
            count=variant_count,
        )
        # Get max variant_index for this upload+type
        from sqlalchemy import func as sqla_func
        max_idx_result = await db.execute(
            select(sqla_func.max(BucketCopy.variant_index)).where(
                BucketCopy.upload_id == upload_id,
                BucketCopy.bucket_id.is_(None),
                BucketCopy.copy_type == ct,
            )
        )
        max_idx = max_idx_result.scalar() or -1

        for i, text in enumerate(texts):
            copy = BucketCopy(
                user_id=LLOYD_USER_ID,
                bucket_id=None,
                upload_id=upload_id,
                copy_type=ct,
                variant_index=max_idx + 1 + i,
                text=text,
                is_primary=(i == 0 and max_idx == -1),
                generation_batch_id=batch_id,
            )
            db.add(copy)
            if ct == "title":
                generated_titles.append(copy)
            else:
                generated_descriptions.append(copy)

    await db.flush()
    from api.routers.outreach._helpers import copy_dict
    return {
        "upload_id": upload_id,
        "batch_id": batch_id,
        "titles": [copy_dict(c) for c in generated_titles],
        "descriptions": [copy_dict(c) for c in generated_descriptions],
    }


@router.post("/uploads/{upload_id}/copies", status_code=201)
async def create_custom_list_copy(
    upload_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Create a manual copy variant for a custom list."""
    from db.models import BucketCopy
    from api.routers.outreach._helpers import copy_dict
    from sqlalchemy import func as sqla_func

    copy_type = body.get("copy_type", "title")
    text = body.get("text", "")

    max_idx_result = await db.execute(
        select(sqla_func.max(BucketCopy.variant_index)).where(
            BucketCopy.upload_id == upload_id,
            BucketCopy.bucket_id.is_(None),
            BucketCopy.copy_type == copy_type,
        )
    )
    max_idx = max_idx_result.scalar()
    next_idx = (max_idx + 1) if max_idx is not None else 0

    copy = BucketCopy(
        user_id=LLOYD_USER_ID,
        bucket_id=None,
        upload_id=upload_id,
        copy_type=copy_type,
        variant_index=next_idx,
        text=text,
        is_primary=False,
    )
    db.add(copy)
    await db.flush()
    return copy_dict(copy)


@router.get("/uploads/{upload_id}/copies")
async def get_custom_list_copies(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Get title and description copies for a custom list (by upload_id)."""
    from db.models import BucketCopy
    from api.routers.outreach._helpers import copy_dict

    result = await db.execute(
        select(BucketCopy).where(
            BucketCopy.upload_id == upload_id,
            BucketCopy.bucket_id.is_(None),
            BucketCopy.deleted_at.is_(None),
        ).order_by(BucketCopy.copy_type, BucketCopy.variant_index)
    )
    copies = result.scalars().all()
    titles = [copy_dict(c) for c in copies if c.copy_type == "title"]
    descriptions = [copy_dict(c) for c in copies if c.copy_type == "description"]
    return {"upload_id": upload_id, "titles": titles, "descriptions": descriptions}


MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB


# ═══════════════════════════════════════════════════════════════════════════
# DIRECT-TO-SUPABASE UPLOAD (presign → upload → confirm)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/uploads/presign", status_code=201)
async def presign_upload(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Step 1: Get a signed URL for direct browser-to-Supabase upload.
    Browser will PUT the file directly to Supabase Storage.
    """
    filename = body.get("filename", "upload.csv")
    file_size = body.get("file_size", 0)
    if not filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    if file_size > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_SIZE // (1024*1024)} MB limit")

    storage_path = f"{LLOYD_USER_ID}/{int(datetime.now().timestamp())}_{filename}"

    # Get signed upload URL from Supabase Storage
    import httpx
    signed_endpoint = _storage_api_url(f"/storage/v1/object/upload/sign/{CSV_BUCKET}/{storage_path}")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                signed_endpoint,
                headers=_supabase_headers(**{"Content-Type": "application/json"}),
                json={},
                timeout=30.0,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Failed to reach Supabase Storage: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(502, f"Failed to get signed URL from Supabase ({resp.status_code}): {resp.text[:300]}")
    try:
        signed_data = resp.json()
    except ValueError as exc:
        raise HTTPException(502, "Supabase returned an invalid signed upload response") from exc

    # Supabase returns a relative URL with token — construct the full upload URL
    relative_url = signed_data.get("url", "")
    if not relative_url.startswith("/"):
        raise HTTPException(502, "Supabase signed upload response is missing a valid URL")
    signed_url = _storage_api_url(f"/storage/v1{relative_url}")

    upload = UploadHistory(
        user_id=LLOYD_USER_ID,
        file_name=filename,
        storage_path=storage_path,
        status="uploading",
    )
    db.add(upload)
    await db.flush()

    return {
        "upload_id": upload.id,
        "signed_url": signed_url,
        "storage_path": storage_path,
    }


@router.post("/uploads/{upload_id}/confirm", status_code=200)
async def confirm_upload(
    upload_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Step 2: Called after browser finishes uploading to Supabase Storage.
    Reads headers + preview from Storage, estimates row count.
    """
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if not upload.storage_path:
        raise HTTPException(400, "No storage path")

    file_size = body.get("file_size", 0)

    # Read first 32KB from Storage for headers + preview
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _storage_api_url(f"/storage/v1/object/{CSV_BUCKET}/{upload.storage_path}"),
            headers=_supabase_headers(Range="bytes=0-32767"),
            timeout=30.0,
        )
        if resp.status_code not in (200, 206):
            raise HTTPException(500, f"Failed to read CSV from Storage: {resp.status_code}")

    text = resp.text
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        raise HTTPException(400, "CSV file appears empty")

    headers = _parse_csv_line(lines[0])
    preview_rows = [_parse_csv_line(lines[i]) for i in range(1, min(6, len(lines)))]

    # Estimate total rows from file size and average row length
    if len(lines) > 1 and file_size > 0:
        sample_bytes = sum(len(l.encode("utf-8")) + 1 for l in lines[:min(20, len(lines))])
        avg_row_bytes = sample_bytes / min(20, len(lines))
        total_rows = max(1, int(file_size / avg_row_bytes) - 1)  # subtract header
    else:
        total_rows = max(0, len(lines) - 1)

    # Update upload record
    upload.total_contacts = total_rows
    await db.flush()

    return {
        "id": upload.id,
        "file_name": upload.file_name,
        "storage_path": upload.storage_path,
        "total_rows": total_rows,
        "file_size": file_size,
        "headers": headers,
        "preview_rows": preview_rows,
    }


@router.post("/uploads/{upload_id}/import", status_code=202)
async def start_import(
    upload_id: str,
    body: ImportStartCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Step 2: Start background import after user maps columns.
    Returns immediately while import processes in background.
    """
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status not in ("uploading",):
        raise HTTPException(409, f"Cannot start import: upload status is '{upload.status}', expected 'uploading'")

    # Validate custom list mode
    if body.upload_mode == "custom_list":
        if not body.custom_list_name or not body.custom_list_name.strip():
            raise HTTPException(400, "Custom list name is required")
        body.custom_list_name = body.custom_list_name.strip()

    upload.field_mappings = body.field_mappings
    upload.duplicate_mode = body.duplicate_mode
    upload.upload_mode = body.upload_mode
    upload.custom_list_name = body.custom_list_name
    upload.status = "processing"
    upload.progress = 0

    # Upsert custom fields
    for csv_header, target in body.field_mappings.items():
        if target.startswith("custom:"):
            field_name = target[7:]
            existing_field = await db.execute(
                select(ContactCustomField).where(
                    ContactCustomField.user_id == LLOYD_USER_ID,
                    ContactCustomField.field_name == field_name,
                )
            )
            if not existing_field.scalar_one_or_none():
                db.add(ContactCustomField(
                    user_id=LLOYD_USER_ID,
                    field_name=field_name,
                    field_type="text",
                ))

    await db.flush()

    # Set up control state
    pause_event = asyncio.Event()
    pause_event.set()  # start running (not paused)
    _import_pause_events[upload_id] = pause_event
    _import_cancel_flags[upload_id] = False

    def _cleanup(t):
        _active_import_tasks.pop(upload_id, None)
        _import_pause_events.pop(upload_id, None)
        _import_cancel_flags.pop(upload_id, None)

    task = asyncio.create_task(
        _process_csv_import(upload_id, upload.storage_path, body.field_mappings, body.duplicate_mode, body.upload_mode)
    )
    _active_import_tasks[upload_id] = task
    task.add_done_callback(_cleanup)

    return {"id": upload_id, "status": "processing"}


@router.post("/uploads/{upload_id}/pause", status_code=200)
async def pause_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Pause a running import. The import will finish its current batch then wait."""
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")

    event = _import_pause_events.get(upload_id)
    if not event:
        raise HTTPException(404, "No active import for this upload")

    event.clear()  # clear = paused

    result = await db.execute(select(UploadHistory).where(UploadHistory.id == upload_id))
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "paused"
        await db.flush()

    return {"id": upload_id, "status": "paused"}


@router.post("/uploads/{upload_id}/resume", status_code=200)
async def resume_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Resume a paused import."""
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")

    event = _import_pause_events.get(upload_id)
    if not event:
        raise HTTPException(404, "No active import for this upload")

    event.set()  # set = running

    result = await db.execute(select(UploadHistory).where(UploadHistory.id == upload_id))
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "processing"
        await db.flush()

    return {"id": upload_id, "status": "processing"}


@router.post("/uploads/{upload_id}/cancel", status_code=200)
async def cancel_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Cancel a running or paused import. Already-inserted rows remain in the database."""
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")

    _import_cancel_flags[upload_id] = True
    # If paused, unpause so the loop can exit
    event = _import_pause_events.get(upload_id)
    if event:
        event.set()

    result = await db.execute(select(UploadHistory).where(UploadHistory.id == upload_id))
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "cancelled"
        await db.flush()

    return {"id": upload_id, "status": "cancelled"}


@router.get("/uploads/{upload_id}/status")
async def get_upload_status(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Poll this endpoint to track import progress."""
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")

    return {
        "id": upload.id,
        "file_name": upload.file_name,
        "status": upload.status,
        "progress": upload.progress,
        "total_rows": upload.total_contacts,
        "processed_rows": upload.processed_rows,
        "inserted_count": upload.inserted_count,
        "skipped_count": upload.skipped_count,
        "overwritten_count": upload.overwritten_count,
        "error_message": upload.error_message,
        "bucket_summary": upload.bucket_summary,
    }


@router.get("/uploads/{upload_id}/headers")
async def get_upload_headers(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Re-fetch CSV headers from Storage for uploads awaiting mapping."""
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if not upload.storage_path:
        raise HTTPException(400, "No storage path — CSV may have been cleaned up")

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _storage_api_url(f"/storage/v1/object/{CSV_BUCKET}/{upload.storage_path}"),
            headers=_supabase_headers(Range="bytes=0-8191"),
            timeout=30.0,
        )
        if resp.status_code not in (200, 206):
            raise HTTPException(500, "Failed to read CSV from Storage")

    text = resp.text
    lines = text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    headers = _parse_csv_line(lines[0])
    preview_rows = [_parse_csv_line(lines[i]) for i in range(1, min(6, len(lines)))]

    return {
        "id": upload.id,
        "file_name": upload.file_name,
        "storage_path": upload.storage_path,
        "total_rows": upload.total_contacts,
        "headers": headers,
        "preview_rows": preview_rows,
    }


@router.delete("/uploads/{upload_id}", status_code=200)
async def delete_upload(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    Delete an upload and its associated data.
    - uploading/pending: delete CSV from Storage + upload record
    - complete/failed: delete all contacts with this upload_id + upload record
    - processing: reject (409) — can't delete while import is running
    """
    result = await db.execute(
        select(UploadHistory).where(UploadHistory.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")

    # Block deletion of custom lists with active assignments
    if upload.upload_mode == "custom_list":
        from db.models import WebinarListAssignment
        active_assignments = await db.execute(
            select(sa_func.count()).where(
                WebinarListAssignment.source_upload_id == upload_id,
            )
        )
        if (active_assignments.scalar() or 0) > 0:
            raise HTTPException(409, "This custom list has active assignments. Remove them from webinars first.")

    if upload.status in ("processing", "paused"):
        # Cancel the import first if it's still running
        if upload_id in _active_import_tasks:
            _import_cancel_flags[upload_id] = True
            event = _import_pause_events.get(upload_id)
            if event:
                event.set()
            # Wait briefly for task to finish
            task = _active_import_tasks.get(upload_id)
            if task:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    task.cancel()

        # Re-query upload status — it may have changed during cancellation
        refreshed = await db.execute(
            select(UploadHistory).where(UploadHistory.id == upload_id)
        )
        upload = refreshed.scalar_one_or_none()
        if not upload:
            raise HTTPException(404, "Upload not found after cancellation")

    # Always delete contacts associated with this upload (regardless of status)
    count_result = await db.execute(
        select(sa_func.count()).select_from(Contact).where(Contact.upload_id == upload_id)
    )
    deleted_contacts = count_result.scalar() or 0

    if deleted_contacts > 0:
        await db.execute(
            delete(Contact).where(Contact.upload_id == upload_id)
        )

    if upload.storage_path:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.delete(
                    _storage_api_url(f"/storage/v1/object/{CSV_BUCKET}/{upload.storage_path}"),
                    headers=_supabase_headers(),
                    timeout=30.0,
                )
        except Exception:
            pass

    await db.execute(
        delete(UploadHistory).where(UploadHistory.id == upload_id)
    )

    return {
        "id": upload_id,
        "deleted_contacts": deleted_contacts,
        "message": f"Upload deleted. {deleted_contacts} contacts removed." if deleted_contacts else "Upload deleted.",
    }


# ── Helpers ────────────────────────────────────────────────────────────────

async def _resolve_merge_chain(conn, start_id: str, preloaded: dict | None = None) -> str | None:
    """Follow merged_into_bucket_id until we hit a non-deleted bucket.

    Returns the final active bucket's id, or None if the chain is orphaned.
    `preloaded` is an optional id→row dict to avoid extra queries.
    """
    seen: set[str] = set()
    cache = dict(preloaded or {})

    async def load(bid: str):
        if bid in cache:
            return cache[bid]
        r = await conn.execute(
            select(
                OutreachBucket.__table__.c.id,
                OutreachBucket.__table__.c.merged_into_bucket_id,
                OutreachBucket.__table__.c.deleted_at,
            ).where(OutreachBucket.__table__.c.id == bid)
        )
        row = r.first()
        cache[bid] = row
        return row

    current = await load(start_id)
    while current and current.deleted_at and current.merged_into_bucket_id:
        if current.id in seen:
            return None  # cycle guard
        seen.add(current.id)
        current = await load(current.merged_into_bucket_id)
    if current and current.deleted_at is None:
        return current.id
    return None


async def _ensure_buckets(engine, new_names: set, bucket_cache: dict):
    """Create missing buckets (race-safe) and update bucket_cache with their IDs.

    Names that match a soft-deleted bucket with `merged_into_bucket_id` are
    resolved to the keeper so new contacts automatically land in the merged
    target instead of the orphaned source.
    """
    if not new_names:
        return
    async with engine.begin() as conn:
        new_buckets = [
            {"id": str(uuid.uuid4()), "user_id": LLOYD_USER_ID,
             "name": bname, "total_contacts": 0, "remaining_contacts": 0}
            for bname in new_names
        ]
        stmt = pg_insert(OutreachBucket.__table__).values(new_buckets)
        stmt = stmt.on_conflict_do_nothing(constraint="uq_outreach_buckets_user_name")
        await conn.execute(stmt)
        # Re-fetch all rows for these names — including soft-deleted merged ones
        result = await conn.execute(
            select(
                OutreachBucket.__table__.c.id,
                OutreachBucket.__table__.c.name,
                OutreachBucket.__table__.c.merged_into_bucket_id,
                OutreachBucket.__table__.c.deleted_at,
            ).where(
                OutreachBucket.__table__.c.user_id == LLOYD_USER_ID,
                OutreachBucket.__table__.c.name.in_(new_names),
            )
        )
        rows = list(result)
        preload = {r.id: r for r in rows}

        # First pass: active buckets take precedence
        for row in rows:
            if row.deleted_at is None:
                bucket_cache[row.name] = row.id

        # Second pass: resolve merged sources — don't overwrite active entries
        for row in rows:
            if row.name in bucket_cache:
                continue
            if row.deleted_at and row.merged_into_bucket_id:
                target = await _resolve_merge_chain(conn, row.id, preloaded=preload)
                if target:
                    bucket_cache[row.name] = target


# ── Background import task ────────────────────────────────────────────────

async def _process_csv_import(
    upload_id: str,
    storage_path: str,
    field_mappings: dict,
    duplicate_mode: str,
    upload_mode: str = "bucket",
):
    """Background task: download CSV from Storage, parse, bulk insert."""
    engine = _bg_engine
    if not engine:
        print(f"[IMPORT] FAILED: no DATABASE_URL configured")
        return

    import tempfile
    tmp_path = None
    processed = 0
    inserted = 0
    skipped = 0
    overwritten = 0

    try:
        import httpx
        import time as _time

        start_time = _time.monotonic()
        print(f"[IMPORT] Starting: {upload_id} — downloading CSV to temp file...")

        # Look up expected file size for dynamic timeout
        async with engine.begin() as conn:
            sz_result = await conn.execute(
                select(UploadHistory.__table__.c.total_contacts).where(
                    UploadHistory.__table__.c.id == upload_id
                )
            )
            # Estimate: ~600 bytes per row average for enriched CSVs
            est_rows = sz_result.scalar() or 0
            est_size_mb = max(1, (est_rows * 600) / (1024 * 1024))
            read_timeout = max(120.0, est_size_mb * 2.0)  # 2s per MB, min 120s

        # Download CSV to a temp file with retry — never hold full file in memory
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="import_")
        os.close(tmp_fd)

        for dl_attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "GET",
                        _storage_api_url(f"/storage/v1/object/{CSV_BUCKET}/{storage_path}"),
                        headers=_supabase_headers(),
                        timeout=httpx.Timeout(connect=30.0, read=read_timeout, write=30.0, pool=30.0),
                    ) as resp:
                        if resp.status_code != 200:
                            raise Exception(f"Failed to download CSV: {resp.status_code}")
                        with open(tmp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                f.write(chunk)
                break  # success
            except Exception as dl_err:
                if dl_attempt < 2:
                    print(f"[IMPORT] Download retry {dl_attempt+1}: {dl_err}")
                    await asyncio.sleep(2)
                else:
                    raise

        dl_time = _time.monotonic() - start_time
        file_size = os.path.getsize(tmp_path)
        print(f"[IMPORT] Downloaded {file_size/1024/1024:.1f}MB in {dl_time:.1f}s — parsing...")

        # Parse from disk — no full file in memory
        csv_file = open(tmp_path, "r", encoding="utf-8", errors="replace")
        reader = csv.reader(csv_file)
        try:
            csv_headers = [h.strip() for h in next(reader)]
        except StopIteration:
            csv_file.close()
            raise Exception("CSV file is empty")

        # Build column mapping
        col_map: dict[int, str] = {}
        for csv_header, target in field_mappings.items():
            if target == "skip" or not target:
                continue
            try:
                idx = csv_headers.index(csv_header)
            except ValueError:
                continue
            col_map[idx] = target

        STANDARD_FIELDS = {
            "contact_id", "first_name", "last_name", "email", "company_website",
            "bucket_name", "classification", "confidence", "reasoning", "cost",
            "status", "lead_list_name", "segment_name", "created_date",
            "industry", "employee_range", "country", "database_provider", "scraper",
            "enrichment_classification", "primary_identity", "sub_identity", "sector",
        }
        FLOAT_FIELDS = {"confidence", "cost"}
        is_custom_list = upload_mode == "custom_list"
        if is_custom_list:
            print(f"[IMPORT] Custom list mode — skipping bucket classification")
        bucket_target_idx = next((idx for idx, t in col_map.items() if t == "bucket"), None) if not is_custom_list else None

        # Load existing buckets (harmless for custom lists — bucket_target_idx is None so no contacts get bucket_id)
        bucket_cache: dict[str, str] = {}
        async with engine.begin() as conn:
            result = await conn.execute(
                select(
                    OutreachBucket.__table__.c.id,
                    OutreachBucket.__table__.c.name,
                    OutreachBucket.__table__.c.merged_into_bucket_id,
                    OutreachBucket.__table__.c.deleted_at,
                ).where(OutreachBucket.__table__.c.user_id == LLOYD_USER_ID)
            )
            all_rows = list(result)
            preload = {r.id: r for r in all_rows}

            # Active buckets first — canonical name → id mapping
            for row in all_rows:
                if row.deleted_at is None:
                    bucket_cache[row.name] = row.id

            # Merged sources — resolve to keeper, don't overwrite active
            for row in all_rows:
                if row.name in bucket_cache:
                    continue
                if row.deleted_at and row.merged_into_bucket_id:
                    target = await _resolve_merge_chain(conn, row.id, preloaded=preload)
                    if target:
                        bucket_cache[row.name] = target

        # Use total_rows from the upload step (newline count) — no need for a counting pass
        # The upload handler already set total_contacts
        total_rows_estimate = 0
        async with engine.begin() as conn:
            r = await conn.execute(
                select(UploadHistory.__table__.c.total_contacts).where(
                    UploadHistory.__table__.c.id == upload_id
                )
            )
            total_rows_estimate = r.scalar() or 0

        async with engine.begin() as conn:
            await conn.execute(
                update(UploadHistory.__table__)
                .where(UploadHistory.__table__.c.id == upload_id)
                .values(status="processing")
            )

        def _build_contact(parsed: list[str]) -> dict:
            contact: dict = {
                "id": str(uuid.uuid4()),
                "user_id": LLOYD_USER_ID,
                "upload_id": upload_id,
                "bucket_id": None,
                "outreach_status": "available",
                "custom_data": {},
            }
            for f in STANDARD_FIELDS:
                contact[f] = None
            custom_data: dict = {}
            for col_idx, target in col_map.items():
                value = parsed[col_idx].strip() if col_idx < len(parsed) else ""
                if not value:
                    continue
                if target == "bucket":
                    contact["bucket_name"] = value
                    contact["bucket_id"] = bucket_cache.get(value)
                elif target in FLOAT_FIELDS:
                    try:
                        contact[target] = float(value)
                    except (ValueError, TypeError):
                        contact[target] = None
                elif target.startswith("custom:"):
                    custom_data[target[7:]] = value
                else:
                    contact[target] = value
            if custom_data:
                contact["custom_data"] = custom_data
            if contact.get("email"):
                contact["email"] = contact["email"].lower().strip()
            return contact

        async def _flush_batch(rows_to_insert: list[dict]) -> tuple[int, int, int]:
            """Deduplicate within batch, insert, return (inserted, skipped, overwritten)."""
            seen: dict[str, int] = {}
            dupes = 0
            for i, r in enumerate(rows_to_insert):
                email = r.get("email")
                if email:
                    if email in seen:
                        dupes += 1
                    seen[email] = i
            if dupes > 0:
                keep = set(seen.values()) | {i for i, r in enumerate(rows_to_insert) if not r.get("email")}
                rows_to_insert = [rows_to_insert[i] for i in sorted(keep)]

            b_ins, b_skip, b_over = 0, dupes, 0
            # Retry up to 3 times on connection errors (Supabase pooler drops idle connections)
            for attempt in range(3):
                try:
                    async with engine.begin() as conn:
                        stmt = pg_insert(Contact.__table__).values(rows_to_insert)
                        if duplicate_mode == "overwrite":
                            set_cols = {
                                c.name: getattr(stmt.excluded, c.name)
                                for c in Contact.__table__.columns
                                if c.name not in ("id", "user_id", "email", "created_at")
                            }
                            stmt = stmt.on_conflict_do_update(constraint="uq_contacts_user_email", set_=set_cols)
                            result = await conn.execute(stmt)
                            b_over = result.rowcount
                        else:
                            stmt = stmt.on_conflict_do_nothing(constraint="uq_contacts_user_email")
                            result = await conn.execute(stmt)
                            b_ins = result.rowcount
                            b_skip += len(rows_to_insert) - result.rowcount
                    break  # success
                except Exception as e:
                    if attempt < 2 and ("connection" in str(e).lower() or "timeout" in str(e).lower()):
                        print(f"[IMPORT] DB retry {attempt+1}: {e}")
                        await asyncio.sleep(1)
                    else:
                        raise
            return b_ins, b_skip, b_over

        # Single-pass: iterate rows, create buckets on the fly, batch insert
        # asyncpg limit: 32767 params per query. Each contact has ~29 columns.
        # 32767 / 29 ≈ 1130, so 1000 rows per batch is safe.
        BATCH_SIZE = 1000
        inserted = 0
        skipped = 0
        overwritten = 0
        processed = 0
        batch_rows: list[list[str]] = []
        new_bucket_names: set[str] = set()

        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            # Discover new bucket names on the fly
            if bucket_target_idx is not None:
                val = row[bucket_target_idx].strip() if bucket_target_idx < len(row) else ""
                if val and val not in bucket_cache:
                    new_bucket_names.add(val)

            batch_rows.append(row)

            if len(batch_rows) >= BATCH_SIZE:
                # Create any new buckets discovered in this batch
                if new_bucket_names:
                    await _ensure_buckets(engine, new_bucket_names, bucket_cache)
                    new_bucket_names.clear()

                # Check cancel/pause
                if _import_cancel_flags.get(upload_id, False):
                    print(f"[IMPORT] Cancelled: {upload_id} at row {processed}")
                    async with engine.begin() as conn:
                        await conn.execute(
                            update(UploadHistory.__table__)
                            .where(UploadHistory.__table__.c.id == upload_id)
                            .values(status="cancelled", processed_rows=processed,
                                    inserted_count=inserted, skipped_count=skipped,
                                    overwritten_count=overwritten)
                        )
                    return

                pause_event = _import_pause_events.get(upload_id)
                if pause_event and not pause_event.is_set():
                    await pause_event.wait()
                    if _import_cancel_flags.get(upload_id, False):
                        async with engine.begin() as conn:
                            await conn.execute(
                                update(UploadHistory.__table__)
                                .where(UploadHistory.__table__.c.id == upload_id)
                                .values(status="cancelled", processed_rows=processed,
                                        inserted_count=inserted, skipped_count=skipped,
                                        overwritten_count=overwritten)
                            )
                        return

                # Build contacts and flush
                contacts = [_build_contact(r) for r in batch_rows]
                try:
                    b_ins, b_skip, b_over = await _flush_batch(contacts)
                    inserted += b_ins
                    skipped += b_skip
                    overwritten += b_over
                except Exception as e:
                    print(f"[IMPORT] Batch error at row {processed}: {e}")
                    traceback.print_exc()
                    skipped += len(batch_rows)

                processed += len(batch_rows)
                batch_rows = []

                # Update progress
                total_rows = max(total_rows_estimate, processed)
                progress_pct = min(99, int((processed / total_rows) * 100))
                elapsed = _time.monotonic() - start_time
                rate = processed / elapsed if elapsed > 0 else 0
                print(f"[IMPORT] {processed}/{total_rows} ({progress_pct}%) — {rate:.0f} rows/s — {inserted} ins, {skipped} skip")
                async with engine.begin() as conn:
                    await conn.execute(
                        update(UploadHistory.__table__)
                        .where(UploadHistory.__table__.c.id == upload_id)
                        .values(progress=progress_pct, processed_rows=processed,
                                inserted_count=inserted, skipped_count=skipped,
                                overwritten_count=overwritten)
                    )
                await asyncio.sleep(0)

        # Flush remaining rows
        if batch_rows:
            if new_bucket_names:
                await _ensure_buckets(engine, new_bucket_names, bucket_cache)

            contacts = [_build_contact(r) for r in batch_rows]
            try:
                b_ins, b_skip, b_over = await _flush_batch(contacts)
                inserted += b_ins
                skipped += b_skip
                overwritten += b_over
            except Exception as e:
                print(f"[IMPORT] Final batch error: {e}")
                traceback.print_exc()
                skipped += len(batch_rows)
            processed += len(batch_rows)

        total_rows = processed  # actual count after full iteration
        csv_file.close()

        # Finalize: recalculate bucket counts (bucket mode) or just mark complete (custom list)
        async with engine.begin() as conn:
            bucket_summary = []

            if not is_custom_list:
                touched_bucket_ids = list(bucket_cache.values()) if bucket_cache else []
                if touched_bucket_ids:
                    bucket_counts = await conn.execute(
                        select(
                            Contact.__table__.c.bucket_id,
                            sa_func.count(Contact.__table__.c.id).label("total"),
                            sa_func.count(Contact.__table__.c.id).filter(
                                Contact.__table__.c.outreach_status == "available"
                            ).label("available"),
                        )
                        .where(Contact.__table__.c.user_id == LLOYD_USER_ID,
                               Contact.__table__.c.bucket_id.in_(touched_bucket_ids))
                        .group_by(Contact.__table__.c.bucket_id)
                    )
                    count_map = {row.bucket_id: {"total": row.total, "available": row.available} for row in bucket_counts}
                else:
                    count_map = {}

                if count_map:
                    from sqlalchemy import case
                    bucket_ids_to_update = list(count_map.keys())
                    total_cases = case(
                        *[(OutreachBucket.__table__.c.id == bid, count_map[bid]["total"]) for bid in bucket_ids_to_update],
                        else_=OutreachBucket.__table__.c.total_contacts,
                    )
                    remaining_cases = case(
                        *[(OutreachBucket.__table__.c.id == bid, count_map[bid]["available"]) for bid in bucket_ids_to_update],
                        else_=OutreachBucket.__table__.c.remaining_contacts,
                    )
                    await conn.execute(
                        update(OutreachBucket.__table__)
                        .where(OutreachBucket.__table__.c.id.in_(bucket_ids_to_update))
                        .values(total_contacts=total_cases, remaining_contacts=remaining_cases)
                    )

                buckets_result = await conn.execute(
                    select(OutreachBucket.__table__.c.id, OutreachBucket.__table__.c.name,
                           OutreachBucket.__table__.c.countries, OutreachBucket.__table__.c.emp_range)
                    .where(OutreachBucket.__table__.c.user_id == LLOYD_USER_ID,
                           OutreachBucket.__table__.c.deleted_at.is_(None))
                )
                for b in buckets_result:
                    real_count = count_map.get(b.id, {"total": 0})["total"]
                    bucket_summary.append({"name": b.name, "count": real_count,
                        "countries": b.countries or [], "empRanges": [b.emp_range] if b.emp_range else [],
                        "avgConfidence": 0})

            await conn.execute(
                update(UploadHistory.__table__)
                .where(UploadHistory.__table__.c.id == upload_id)
                .values(status="complete", progress=100,
                        total_contacts=total_rows, processed_rows=total_rows,
                        inserted_count=inserted, skipped_count=skipped, overwritten_count=overwritten,
                        total_buckets=len(bucket_summary),
                        bucket_summary=sorted(bucket_summary, key=lambda x: x["count"], reverse=True) if bucket_summary else None)
            )

        # Cleanup CSV from Storage
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    _storage_api_url(f"/storage/v1/object/{CSV_BUCKET}/{storage_path}"),
                    headers=_supabase_headers(),
                    timeout=30.0,
                )
            print(f"[IMPORT] Cleaned up: {storage_path}")
        except Exception:
            print(f"[IMPORT] Warning: cleanup failed for {storage_path}")

        print(f"[IMPORT] Done: {upload_id} — {inserted} inserted, {skipped} skipped, {overwritten} overwritten")

    except Exception as e:
        print(f"[IMPORT] FAILED: {upload_id} at row {processed} — {e}")
        traceback.print_exc()
        # Write partial success info so user can see what was imported before the crash
        error_detail = f"Import stopped at row {processed:,}. {inserted:,} contacts were successfully imported. Error: {str(e)[:300]}"
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    update(UploadHistory.__table__)
                    .where(UploadHistory.__table__.c.id == upload_id)
                    .values(
                        status="failed",
                        error_message=error_detail,
                        processed_rows=processed,
                        inserted_count=inserted,
                        skipped_count=skipped,
                        overwritten_count=overwritten,
                    )
                )
        except Exception:
            pass
    finally:
        # Clean up temp file
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
