"""Per-webinar "Added to Calendar" CSV ingestion.

Mirrors api/routers/outreach/uploads.py: presign → browser PUT to Supabase
Storage → confirm → import (background) → status. Differs in:

- Upload is scoped to a single webinar (chosen up-front; webinar_id stored
  on the upload row).
- Fixed schema, no user column mapping. Auto-mapped headers:
    Email, Calendar_invited_date, Calendar_account, Calendar account prefix,
    Calendar_webinar_series, Calendar_invite_response (optional)
  The three test_* columns are ignored.
- Upsert key is (webinar_id, email): re-uploading updates rows; the same
  email across webinars yields independent rows.
- Matching is strict: only contacts attached to one of *this* webinar's
  WebinarListAssignment rows count as matched. Anything else = "No List Data"
  (matched_assignment_id NULL).
"""
import asyncio
import csv
import io
import os
import tempfile
import time as _time
import traceback
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func as sa_func, update, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy import pool

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import (
    CalendarAccountSender, Contact, OutreachSender, Webinar,
    WebinarCalendarInvite, WebinarCalendarUpload, WebinarListAssignment,
    WebinarNonjoinerInvite,
)
from db.session import get_db

router = APIRouter()


# ── Storage config (shared with outreach/uploads.py) ──────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CSV_BUCKET = "csv-uploads"

_BG_DATABASE_URL = os.environ.get("DATABASE_URL", "")
if _BG_DATABASE_URL.startswith("postgres://"):
    _BG_DATABASE_URL = _BG_DATABASE_URL.replace("postgres://", "postgresql://", 1)
if "postgresql+asyncpg://" not in _BG_DATABASE_URL:
    _BG_DATABASE_URL = _BG_DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

_bg_engine = create_async_engine(_BG_DATABASE_URL, poolclass=pool.NullPool) if _BG_DATABASE_URL else None

_active_import_tasks: dict[str, asyncio.Task] = {}
_import_pause_events: dict[str, asyncio.Event] = {}   # set = running, clear = paused
_import_cancel_flags: dict[str, bool] = {}


MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
BATCH_SIZE = 2000  # ~10 cols per row → ~20k params, well under asyncpg's 32767 limit


def _supabase_base_url() -> str:
    base = SUPABASE_URL.strip()
    if not base:
        raise HTTPException(500, "Supabase storage is not configured: missing SUPABASE_URL")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return base.rstrip("/")


def _supabase_service_key() -> str:
    key = SUPABASE_SERVICE_KEY.strip()
    if not key:
        raise HTTPException(500, "Supabase storage is not configured: missing SUPABASE_SERVICE_KEY")
    return key


def _supabase_headers(**extra: str) -> dict[str, str]:
    h = {"Authorization": f"Bearer {_supabase_service_key()}"}
    h.update(extra)
    return h


def _storage_url(path: str) -> str:
    return f"{_supabase_base_url()}{path}"


def _parse_csv_line(line: str) -> list[str]:
    for row in csv.reader(io.StringIO(line)):
        return [cell.strip() for cell in row]
    return []


# Headers we recognise and the model column they map to. Anything else is ignored.
# `response` / `calendar_response` are accepted aliases so a Non-joiners CSV can
# be just two columns (Email, Response).
_HEADER_MAP = {
    "email": "email",
    "calendar_invited_date": "calendar_invited_date",
    "calendar_account": "calendar_account",
    "calendar account prefix": "calendar_account_prefix",
    "calendar_account_prefix": "calendar_account_prefix",
    "calendar_webinar_series": "calendar_webinar_series",
    "calendar_invite_response": "calendar_invite_response",
    "calendar_response": "calendar_invite_response",
    "response": "calendar_invite_response",
}


def _normalize_header(h: str) -> str:
    return h.strip().lower()


def _build_col_map(headers: list[str]) -> dict[int, str]:
    col_map: dict[int, str] = {}
    for idx, h in enumerate(headers):
        target = _HEADER_MAP.get(_normalize_header(h))
        if target:
            col_map[idx] = target
    return col_map


def _parse_invited_date(value: str) -> datetime | None:
    """Accept both '2026-05-08 14:07' and '5/6/2026'. Returns timezone-aware
    UTC datetimes (the CSV doesn't carry a TZ, so we treat values as UTC)."""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%-m/%-d/%Y",
        "%m/%d/%y %I:%M %p",
        "%m/%d/%y %H:%M",
        "%m/%d/%y",
        "%-m/%-d/%y",
    ):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Fallback: try ISO
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return None


def _upload_dict(
    u: WebinarCalendarUpload,
    webinar_label: str | None = None,
    sender_name: str | None = None,
) -> dict:
    return {
        "id": u.id,
        "webinar_id": u.webinar_id,
        "webinar_label": webinar_label,
        "kind": u.kind,
        "sender_id": u.sender_id,
        "sender_name": sender_name,
        "file_name": u.file_name,
        "status": u.status,
        "progress": u.progress,
        "has_responses": u.has_responses,
        "total_rows": u.total_rows,
        "processed_rows": u.processed_rows,
        "matched_count": u.matched_count,
        "unmatched_count": u.unmatched_count,
        "error_message": u.error_message,
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "completed_at": u.completed_at.isoformat() if u.completed_at else None,
    }


def _webinar_label(w: Webinar) -> str:
    base = f"E{w.number}"
    if w.variant_label:
        return f"{base} — {w.variant_label}"
    return base


# ═══════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.get("")
async def list_calendar_uploads(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """All calendar uploads for the user, newest first."""
    result = await db.execute(
        select(WebinarCalendarUpload)
        .where(WebinarCalendarUpload.user_id == LLOYD_USER_ID)
        .order_by(WebinarCalendarUpload.created_at.desc())
    )
    uploads = result.scalars().all()

    # Resolve webinar labels in one query
    webinar_ids = list({u.webinar_id for u in uploads})
    label_map: dict[str, str] = {}
    if webinar_ids:
        wresult = await db.execute(
            select(Webinar).where(Webinar.id.in_(webinar_ids))
        )
        for w in wresult.scalars().all():
            label_map[w.id] = _webinar_label(w)

    sender_ids = list({u.sender_id for u in uploads if u.sender_id})
    sender_name_map: dict[str, str] = {}
    if sender_ids:
        sresult = await db.execute(
            select(OutreachSender).where(OutreachSender.id.in_(sender_ids))
        )
        for s in sresult.scalars().all():
            sender_name_map[s.id] = s.name

    return {
        "uploads": [
            _upload_dict(
                u,
                label_map.get(u.webinar_id),
                sender_name_map.get(u.sender_id) if u.sender_id else None,
            )
            for u in uploads
        ]
    }


@router.get("/account-health")
async def calendar_account_health(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Per-webinar, per-Calendar_account aggregates for the Account Health tab.

    For each (webinar, calendar_account):
      total_sent = COUNT(*) of invite rows
      yes        = COUNT(*) WHERE calendar_invite_response = 'Yes'
      maybe      = COUNT(*) WHERE calendar_invite_response = 'Maybe'

    webinar_calendar_invites is unique on (webinar_id, email), so COUNT(*)
    equals COUNT(DISTINCT email) — same result as the source spreadsheet's
    COUNTUNIQUEIFS without needing DISTINCT.

    Only webinars whose date is on or before today are included (future
    webinars haven't happened yet, so there's nothing to report). Newest
    first by number; `has_upload` flags whether any calendar upload exists
    for that webinar.
    """
    today = datetime.now(tz=timezone.utc).date()
    wresult = await db.execute(
        select(Webinar)
        .where(Webinar.user_id == LLOYD_USER_ID, Webinar.date <= today)
        .order_by(Webinar.number.desc(), Webinar.id.asc())
    )
    webinars = wresult.scalars().all()
    webinar_ids = [w.id for w in webinars]

    with_upload: set[str] = set()
    if webinar_ids:
        ur = await db.execute(
            select(WebinarCalendarUpload.webinar_id)
            .where(
                WebinarCalendarUpload.user_id == LLOYD_USER_ID,
                WebinarCalendarUpload.webinar_id.in_(webinar_ids),
            )
            .distinct()
        )
        with_upload = {row[0] for row in ur.all()}

    invite = WebinarCalendarInvite.__table__
    agg_rows: list = []
    if webinar_ids:
        agg = await db.execute(
            select(
                invite.c.webinar_id,
                invite.c.calendar_account,
                sa_func.count().label("total_sent"),
                sa_func.count()
                .filter(invite.c.calendar_invite_response == "Yes")
                .label("yes"),
                sa_func.count()
                .filter(invite.c.calendar_invite_response == "Maybe")
                .label("maybe"),
            )
            .where(invite.c.webinar_id.in_(webinar_ids))
            .group_by(invite.c.webinar_id, invite.c.calendar_account)
        )
        agg_rows = agg.all()

    accounts_map: dict[str, dict[str, dict[str, int]]] = {}
    totals: dict[str, dict[str, int]] = {}
    for r in agg_rows:
        acc = (r.calendar_account or "").strip() or "(unknown)"
        cell = {
            "total_sent": int(r.total_sent or 0),
            "yes": int(r.yes or 0),
            "maybe": int(r.maybe or 0),
        }
        accounts_map.setdefault(acc, {})[r.webinar_id] = cell
        t = totals.setdefault(
            r.webinar_id, {"total_sent": 0, "yes": 0, "maybe": 0}
        )
        t["total_sent"] += cell["total_sent"]
        t["yes"] += cell["yes"]
        t["maybe"] += cell["maybe"]

    # Sort accounts: "(unknown)" last, everything else alphabetical
    sorted_accounts = sorted(
        accounts_map.keys(),
        key=lambda x: (x == "(unknown)", x.lower()),
    )

    # (webinar_id, calendar_account) → sender_id; plus sender id → name.
    sender_map: dict[str, dict[str, str]] = {}
    sender_names: dict[str, str] = {}
    if webinar_ids:
        cas = await db.execute(
            select(
                CalendarAccountSender.webinar_id,
                CalendarAccountSender.calendar_account,
                CalendarAccountSender.sender_id,
            ).where(
                CalendarAccountSender.user_id == LLOYD_USER_ID,
                CalendarAccountSender.webinar_id.in_(webinar_ids),
            )
        )
        for wid, acc, sid in cas.all():
            sender_map.setdefault(wid, {})[acc] = sid

    sresult = await db.execute(
        select(OutreachSender)
        .where(OutreachSender.user_id == LLOYD_USER_ID)
        .order_by(OutreachSender.display_order.asc(), OutreachSender.name.asc())
    )
    senders = sresult.scalars().all()
    for s in senders:
        sender_names[s.id] = s.name

    return {
        "webinars": [
            {
                "id": w.id,
                "number": w.number,
                "variant_label": w.variant_label,
                "label": _webinar_label(w),
                "has_upload": w.id in with_upload,
            }
            for w in webinars
        ],
        "accounts": [
            {"calendar_account": acc, "per_webinar": accounts_map[acc]}
            for acc in sorted_accounts
        ],
        "totals": totals,
        "senders": [
            {"id": s.id, "name": s.name, "color": s.color}
            for s in senders
        ],
        # sender_map[webinar_id][calendar_account] = sender_id
        "sender_map": sender_map,
        # sender_names[sender_id] = name (handy for frontend display)
        "sender_names": sender_names,
    }


@router.get("/day-of-week")
async def calendar_day_of_week(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Per-(webinar, calendar_account, day-of-week) invite stats for the
    Send Day tab on Statistics.

    Counts come from webinar_calendar_invites, grouped by
    EXTRACT(DOW FROM calendar_invited_date) (0=Sun..6=Sat). Rows with a NULL
    invited date are skipped — without a date we can't bucket them.

    Only past webinars (Webinar.date <= today) are included; future webinars
    have no responses to report on yet.
    """
    today = datetime.now(tz=timezone.utc).date()
    wresult = await db.execute(
        select(Webinar)
        .where(Webinar.user_id == LLOYD_USER_ID, Webinar.date <= today)
        .order_by(Webinar.number.desc(), Webinar.id.asc())
    )
    webinars = wresult.scalars().all()
    webinar_ids = [w.id for w in webinars]

    with_upload: set[str] = set()
    if webinar_ids:
        ur = await db.execute(
            select(WebinarCalendarUpload.webinar_id)
            .where(
                WebinarCalendarUpload.user_id == LLOYD_USER_ID,
                WebinarCalendarUpload.webinar_id.in_(webinar_ids),
            )
            .distinct()
        )
        with_upload = {row[0] for row in ur.all()}

    invite = WebinarCalendarInvite.__table__
    cells: list[dict] = []
    if webinar_ids:
        dow_expr = sa_func.extract("dow", invite.c.calendar_invited_date)
        agg = await db.execute(
            select(
                invite.c.webinar_id,
                invite.c.calendar_account,
                dow_expr.label("dow"),
                sa_func.count().label("total_sent"),
                sa_func.count()
                .filter(invite.c.calendar_invite_response == "Yes")
                .label("yes"),
                sa_func.count()
                .filter(invite.c.calendar_invite_response == "Maybe")
                .label("maybe"),
            )
            .where(
                invite.c.webinar_id.in_(webinar_ids),
                invite.c.calendar_invited_date.isnot(None),
            )
            .group_by(invite.c.webinar_id, invite.c.calendar_account, dow_expr)
        )
        for r in agg.all():
            cells.append(
                {
                    "webinar_id": r.webinar_id,
                    "calendar_account": (r.calendar_account or "").strip() or "(unknown)",
                    "dow": int(r.dow or 0),
                    "sent": int(r.total_sent or 0),
                    "yes": int(r.yes or 0),
                    "maybe": int(r.maybe or 0),
                }
            )

    # Invites with NULL calendar_invited_date — can't bucket by weekday, but
    # we surface the count per (webinar, account) so users can see what's
    # missing from the day-of-week analysis.
    skipped: list[dict] = []
    if webinar_ids:
        skip_agg = await db.execute(
            select(
                invite.c.webinar_id,
                invite.c.calendar_account,
                sa_func.count().label("count"),
            )
            .where(
                invite.c.webinar_id.in_(webinar_ids),
                invite.c.calendar_invited_date.is_(None),
            )
            .group_by(invite.c.webinar_id, invite.c.calendar_account)
        )
        for r in skip_agg.all():
            skipped.append(
                {
                    "webinar_id": r.webinar_id,
                    "calendar_account": (r.calendar_account or "").strip() or "(unknown)",
                    "count": int(r.count or 0),
                }
            )

    sender_map: dict[str, dict[str, str]] = {}
    sender_names: dict[str, str] = {}
    if webinar_ids:
        cas = await db.execute(
            select(
                CalendarAccountSender.webinar_id,
                CalendarAccountSender.calendar_account,
                CalendarAccountSender.sender_id,
            ).where(
                CalendarAccountSender.user_id == LLOYD_USER_ID,
                CalendarAccountSender.webinar_id.in_(webinar_ids),
            )
        )
        for wid, acc, sid in cas.all():
            sender_map.setdefault(wid, {})[acc] = sid

    sresult = await db.execute(
        select(OutreachSender)
        .where(OutreachSender.user_id == LLOYD_USER_ID)
        .order_by(OutreachSender.display_order.asc(), OutreachSender.name.asc())
    )
    senders = sresult.scalars().all()
    for s in senders:
        sender_names[s.id] = s.name

    return {
        "webinars": [
            {
                "id": w.id,
                "number": w.number,
                "variant_label": w.variant_label,
                "label": _webinar_label(w),
                "has_upload": w.id in with_upload,
            }
            for w in webinars
        ],
        "cells": cells,
        "skipped": skipped,
        "senders": [
            {"id": s.id, "name": s.name, "color": s.color}
            for s in senders
        ],
        "sender_map": sender_map,
        "sender_names": sender_names,
    }


@router.post("/account-senders/bulk", status_code=200)
async def set_account_senders_bulk(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Pattern B: upsert sender mappings for a list of calendar_accounts
    on a single webinar. Body:
        { webinar_id, sender_id, accounts: [str, ...] }
    Accounts can be a newline- or comma-separated string OR a list; we
    normalize either form. Existing (webinar, account) → sender rows are
    overwritten (last write wins).
    """
    webinar_id = (body.get("webinar_id") or "").strip()
    sender_id = (body.get("sender_id") or "").strip()
    raw_accounts = body.get("accounts")

    if not webinar_id:
        raise HTTPException(400, "webinar_id is required")
    if not sender_id:
        raise HTTPException(400, "sender_id is required")

    # Normalize accounts: accept list or newline/comma-separated string
    accounts: list[str] = []
    if isinstance(raw_accounts, list):
        candidates = [str(a) for a in raw_accounts]
    else:
        text = str(raw_accounts or "")
        candidates = [piece for line in text.splitlines() for piece in line.split(",")]
    for a in candidates:
        cleaned = a.strip().lower()
        if cleaned:
            accounts.append(cleaned)
    # Dedupe, preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for a in accounts:
        if a not in seen:
            seen.add(a)
            deduped.append(a)
    accounts = deduped

    if not accounts:
        raise HTTPException(400, "No accounts provided")

    # Verify webinar & sender belong to this user
    wresult = await db.execute(
        select(Webinar).where(
            Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID
        )
    )
    if not wresult.scalar_one_or_none():
        raise HTTPException(404, "Webinar not found")

    sresult = await db.execute(
        select(OutreachSender).where(
            OutreachSender.id == sender_id,
            OutreachSender.user_id == LLOYD_USER_ID,
        )
    )
    if not sresult.scalar_one_or_none():
        raise HTTPException(404, "Sender not found")

    now = datetime.now(tz=timezone.utc)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "user_id": LLOYD_USER_ID,
            "webinar_id": webinar_id,
            "calendar_account": acc,
            "sender_id": sender_id,
            "updated_at": now,
        }
        for acc in accounts
    ]

    stmt = pg_insert(CalendarAccountSender.__table__).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_cas_webinar_account",
        set_={
            "sender_id": stmt.excluded.sender_id,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)
    await db.flush()

    return {
        "webinar_id": webinar_id,
        "sender_id": sender_id,
        "saved": len(accounts),
    }


@router.post("/presign", status_code=201)
async def presign_calendar_upload(
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Step 1: signed URL for direct browser→Supabase upload. Requires webinar_id.
    Optional sender_id (Pattern A): when set, every distinct calendar_account
    in this CSV is mapped to that sender after import."""
    filename = (body.get("filename") or "calendar.csv").strip()
    file_size = int(body.get("file_size") or 0)
    webinar_id = (body.get("webinar_id") or "").strip()
    is_nonjoiner = bool(body.get("is_nonjoiner"))
    kind = "nonjoiner" if is_nonjoiner else "calendar"
    raw_sender_id = body.get("sender_id")
    # Sender (Pattern A) only applies to normal calendar uploads.
    sender_id = None if is_nonjoiner else ((raw_sender_id or "").strip() or None)

    if not filename.endswith(".csv"):
        raise HTTPException(400, "Only CSV files are accepted")
    if file_size > MAX_UPLOAD_SIZE:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_SIZE // (1024*1024)} MB limit")
    if not webinar_id:
        raise HTTPException(400, "webinar_id is required")

    # Verify webinar belongs to this user
    wresult = await db.execute(
        select(Webinar).where(Webinar.id == webinar_id, Webinar.user_id == LLOYD_USER_ID)
    )
    if not wresult.scalar_one_or_none():
        raise HTTPException(404, "Webinar not found")

    if sender_id:
        sresult = await db.execute(
            select(OutreachSender).where(
                OutreachSender.id == sender_id,
                OutreachSender.user_id == LLOYD_USER_ID,
            )
        )
        if not sresult.scalar_one_or_none():
            raise HTTPException(404, "Sender not found")

    storage_path = f"{LLOYD_USER_ID}/calendar/{int(datetime.now().timestamp())}_{filename}"

    import httpx
    signed_endpoint = _storage_url(f"/storage/v1/object/upload/sign/{CSV_BUCKET}/{storage_path}")
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
        raise HTTPException(502, f"Failed to get signed URL ({resp.status_code}): {resp.text[:300]}")
    try:
        signed_data = resp.json()
    except ValueError as exc:
        raise HTTPException(502, "Supabase returned an invalid signed upload response") from exc

    relative_url = signed_data.get("url", "")
    if not relative_url.startswith("/"):
        raise HTTPException(502, "Supabase signed upload response is missing a valid URL")
    signed_url = _storage_url(f"/storage/v1{relative_url}")

    upload = WebinarCalendarUpload(
        user_id=LLOYD_USER_ID,
        webinar_id=webinar_id,
        sender_id=sender_id,
        kind=kind,
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


@router.post("/{upload_id}/confirm", status_code=200)
async def confirm_calendar_upload(
    upload_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Step 2: read headers from Storage, detect Calendar_invite_response presence,
    estimate row count, set status to 'pending' awaiting import start."""
    result = await db.execute(
        select(WebinarCalendarUpload).where(
            WebinarCalendarUpload.id == upload_id,
            WebinarCalendarUpload.user_id == LLOYD_USER_ID,
        )
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if not upload.storage_path:
        raise HTTPException(400, "No storage path")

    file_size = int(body.get("file_size") or 0)

    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _storage_url(f"/storage/v1/object/{CSV_BUCKET}/{upload.storage_path}"),
            headers=_supabase_headers(Range="bytes=0-32767"),
            timeout=30.0,
        )
        if resp.status_code not in (200, 206):
            raise HTTPException(500, f"Failed to read CSV from Storage: {resp.status_code}")

    lines = [l.strip() for l in resp.text.split("\n") if l.strip()]
    if not lines:
        raise HTTPException(400, "CSV file appears empty")

    headers = _parse_csv_line(lines[0])
    normalized = {_normalize_header(h) for h in headers}
    has_email = "email" in normalized
    # A response column may be named Calendar_invite_response / Calendar_response / Response.
    has_responses = bool(normalized & {"calendar_invite_response", "calendar_response", "response"})

    if upload.kind == "nonjoiner":
        if not has_email:
            raise HTTPException(400, "Non-joiners CSV needs an 'Email' column")
        if not has_responses:
            raise HTTPException(400, "Non-joiners CSV needs a response column (Yes/Maybe)")

    # Estimate total rows from file size & average row length (same trick as outreach uploads)
    if len(lines) > 1 and file_size > 0:
        sample_bytes = sum(len(l.encode("utf-8")) + 1 for l in lines[:min(20, len(lines))])
        avg_row_bytes = sample_bytes / min(20, len(lines))
        total_rows = max(1, int(file_size / avg_row_bytes) - 1)
    else:
        total_rows = max(0, len(lines) - 1)

    preview_rows = [_parse_csv_line(lines[i]) for i in range(1, min(6, len(lines)))]

    upload.total_rows = total_rows
    upload.has_responses = has_responses
    upload.status = "pending"
    await db.flush()

    return {
        "id": upload.id,
        "file_name": upload.file_name,
        "webinar_id": upload.webinar_id,
        "total_rows": total_rows,
        "has_responses": has_responses,
        "headers": headers,
        "preview_rows": preview_rows,
    }


@router.post("/{upload_id}/import", status_code=202)
async def start_calendar_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Step 3: kick off background import. Auto-mapped — no user mapping required."""
    result = await db.execute(
        select(WebinarCalendarUpload).where(
            WebinarCalendarUpload.id == upload_id,
            WebinarCalendarUpload.user_id == LLOYD_USER_ID,
        )
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status not in ("pending", "uploading"):
        raise HTTPException(
            409,
            f"Cannot start import: status is '{upload.status}', expected 'pending'",
        )

    upload.status = "processing"
    upload.progress = 0
    await db.flush()

    pause_event = asyncio.Event()
    pause_event.set()
    _import_pause_events[upload_id] = pause_event
    _import_cancel_flags[upload_id] = False

    def _cleanup(_t):
        _active_import_tasks.pop(upload_id, None)
        _import_pause_events.pop(upload_id, None)
        _import_cancel_flags.pop(upload_id, None)

    task = asyncio.create_task(
        _process_calendar_csv(
            upload_id, upload.webinar_id, upload.storage_path, upload.sender_id,
            kind=upload.kind,
        )
    )
    _active_import_tasks[upload_id] = task
    task.add_done_callback(_cleanup)

    return {"id": upload_id, "status": "processing"}


@router.get("/{upload_id}/status")
async def calendar_upload_status(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(WebinarCalendarUpload).where(
            WebinarCalendarUpload.id == upload_id,
            WebinarCalendarUpload.user_id == LLOYD_USER_ID,
        )
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")
    return _upload_dict(upload)


@router.post("/{upload_id}/pause", status_code=200)
async def pause_calendar_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")
    ev = _import_pause_events.get(upload_id)
    if not ev:
        raise HTTPException(404, "No active import for this upload")
    ev.clear()

    result = await db.execute(
        select(WebinarCalendarUpload).where(WebinarCalendarUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "paused"
        await db.flush()
    return {"id": upload_id, "status": "paused"}


@router.post("/{upload_id}/resume", status_code=200)
async def resume_calendar_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")
    ev = _import_pause_events.get(upload_id)
    if not ev:
        raise HTTPException(404, "No active import for this upload")
    ev.set()

    result = await db.execute(
        select(WebinarCalendarUpload).where(WebinarCalendarUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "processing"
        await db.flush()
    return {"id": upload_id, "status": "processing"}


@router.post("/{upload_id}/cancel", status_code=200)
async def cancel_calendar_import(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if upload_id not in _active_import_tasks:
        raise HTTPException(404, "No active import for this upload")
    _import_cancel_flags[upload_id] = True
    ev = _import_pause_events.get(upload_id)
    if ev:
        ev.set()  # wake up paused loop so it can exit

    result = await db.execute(
        select(WebinarCalendarUpload).where(WebinarCalendarUpload.id == upload_id)
    )
    upload = result.scalar_one_or_none()
    if upload:
        upload.status = "cancelled"
        await db.flush()
    return {"id": upload_id, "status": "cancelled"}


@router.delete("/{upload_id}", status_code=200)
async def delete_calendar_upload(
    upload_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Delete an upload and all its invite rows. Cancels running imports first."""
    result = await db.execute(
        select(WebinarCalendarUpload).where(
            WebinarCalendarUpload.id == upload_id,
            WebinarCalendarUpload.user_id == LLOYD_USER_ID,
        )
    )
    upload = result.scalar_one_or_none()
    if not upload:
        raise HTTPException(404, "Upload not found")

    if upload.status in ("processing", "paused"):
        if upload_id in _active_import_tasks:
            _import_cancel_flags[upload_id] = True
            ev = _import_pause_events.get(upload_id)
            if ev:
                ev.set()
            task = _active_import_tasks.get(upload_id)
            if task:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    task.cancel()

    # Storage cleanup (best effort)
    if upload.storage_path:
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await client.delete(
                    _storage_url(f"/storage/v1/object/{CSV_BUCKET}/{upload.storage_path}"),
                    headers=_supabase_headers(),
                    timeout=30.0,
                )
        except Exception:
            pass

    # FK cascade on upload_id removes invites
    await db.execute(
        delete(WebinarCalendarUpload).where(WebinarCalendarUpload.id == upload_id)
    )
    return {"id": upload_id, "deleted": True}


# ═══════════════════════════════════════════════════════════════════════════
# Background import
# ═══════════════════════════════════════════════════════════════════════════

def resume_orphan_calendar_import(upload: "WebinarCalendarUpload") -> bool:
    """Re-attach worker state and respawn _process_calendar_csv for an upload that
    was left in status='processing' or 'paused' by a backend restart.

    Returns True if the task was spawned, False if we can't resume (missing
    storage_path). Caller is responsible for marking the row as failed in that
    case. Single-instance safe; see note in outreach resume_orphan_import.
    """
    if upload.id in _active_import_tasks:
        return True
    if not upload.storage_path:
        return False

    pause_event = asyncio.Event()
    pause_event.set()
    _import_pause_events[upload.id] = pause_event
    _import_cancel_flags[upload.id] = False

    def _cleanup(_t):
        _active_import_tasks.pop(upload.id, None)
        _import_pause_events.pop(upload.id, None)
        _import_cancel_flags.pop(upload.id, None)

    task = asyncio.create_task(
        _process_calendar_csv(
            upload.id,
            upload.webinar_id,
            upload.storage_path,
            upload.sender_id,
            kind=upload.kind,
            start_from_row=upload.processed_rows or 0,
            initial_matched=upload.matched_count or 0,
            initial_unmatched=upload.unmatched_count or 0,
        )
    )
    _active_import_tasks[upload.id] = task
    task.add_done_callback(_cleanup)
    return True


async def _process_calendar_csv(
    upload_id: str,
    webinar_id: str,
    storage_path: str,
    sender_id: str | None,
    *,
    kind: str = "calendar",
    start_from_row: int = 0,
    initial_matched: int = 0,
    initial_unmatched: int = 0,
):
    """Stream-parse the CSV from Supabase Storage, upsert in batches of BATCH_SIZE.
    If sender_id is set, every distinct calendar_account in the CSV gets upserted
    into calendar_account_senders on successful completion (Pattern A).

    Pass ``start_from_row`` > 0 to resume an orphaned import after a restart: the
    reader is advanced that many rows past the header and counters seed from the
    DB-persisted values.
    """
    engine = _bg_engine
    if not engine:
        print(f"[CAL_IMPORT] FAILED: no DATABASE_URL configured")
        return

    tmp_path: str | None = None
    processed = start_from_row
    matched = initial_matched
    unmatched = initial_unmatched
    upserted = 0
    accounts_seen: set[str] = set()

    try:
        import httpx
        start = _time.monotonic()
        print(f"[CAL_IMPORT] Starting: {upload_id} (webinar {webinar_id})")

        # Estimate timeout from row count
        async with engine.begin() as conn:
            r = await conn.execute(
                select(WebinarCalendarUpload.__table__.c.total_rows).where(
                    WebinarCalendarUpload.__table__.c.id == upload_id
                )
            )
            est_rows = r.scalar() or 0
            est_size_mb = max(1, (est_rows * 200) / (1024 * 1024))  # ~200B/row for this schema
            read_timeout = max(120.0, est_size_mb * 2.0)

        # Download to temp file
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="cal_import_")
        os.close(tmp_fd)

        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "GET",
                        _storage_url(f"/storage/v1/object/{CSV_BUCKET}/{storage_path}"),
                        headers=_supabase_headers(),
                        timeout=httpx.Timeout(connect=30.0, read=read_timeout, write=30.0, pool=30.0),
                    ) as resp:
                        if resp.status_code != 200:
                            raise Exception(f"Storage download failed: {resp.status_code}")
                        with open(tmp_path, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                f.write(chunk)
                break
            except Exception as e:
                if attempt < 2:
                    print(f"[CAL_IMPORT] Download retry {attempt+1}: {e}")
                    await asyncio.sleep(2)
                else:
                    raise

        file_size = os.path.getsize(tmp_path)
        print(f"[CAL_IMPORT] Downloaded {file_size/1024/1024:.1f}MB in {_time.monotonic()-start:.1f}s")

        # Open & parse
        csv_file = open(tmp_path, "r", encoding="utf-8", errors="replace")
        reader = csv.reader(csv_file)
        try:
            headers = [h.strip() for h in next(reader)]
        except StopIteration:
            csv_file.close()
            raise Exception("CSV is empty")

        # Resume support: skip past rows we already processed in a previous run.
        if start_from_row > 0:
            print(f"[CAL_IMPORT] Resuming {upload_id} — skipping first {start_from_row} rows")
            skipped_ahead = 0
            for _ in range(start_from_row):
                try:
                    next(reader)
                    skipped_ahead += 1
                except StopIteration:
                    break
            if skipped_ahead < start_from_row:
                print(f"[CAL_IMPORT] Resume skip-ahead ran out of rows at {skipped_ahead} (expected {start_from_row}) — finalizing")

        col_map = _build_col_map(headers)
        if "email" not in col_map.values():
            raise Exception("CSV is missing an 'Email' column")
        has_responses = "calendar_invite_response" in col_map.values()

        # Persist has_responses (in case confirm step missed it on a tiny preview)
        async with engine.begin() as conn:
            await conn.execute(
                update(WebinarCalendarUpload.__table__)
                .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                .values(has_responses=has_responses, status="processing")
            )

        total_rows_estimate = est_rows

        def _parse_row(row: list[str]) -> dict | None:
            rec: dict = {
                "calendar_invited_date": None,
                "calendar_account": None,
                "calendar_account_prefix": None,
                "calendar_webinar_series": None,
                "calendar_invite_response": None,
                "email": None,
            }
            for idx, target in col_map.items():
                if idx >= len(row):
                    continue
                value = row[idx].strip() if row[idx] else ""
                if not value:
                    continue
                if target == "calendar_invited_date":
                    rec[target] = _parse_invited_date(value)
                elif target == "calendar_webinar_series":
                    rec[target] = _parse_int(value)
                elif target == "email":
                    rec[target] = value.lower()
                else:
                    rec[target] = value
            email = rec.get("email")
            if not email:
                return None
            return rec

        async def _flush_batch(batch: list[dict]) -> tuple[int, int, int]:
            """Match each row against the webinar's assigned contacts, then upsert.
            Returns (matched_in_batch, unmatched_in_batch, rows_affected)."""
            if not batch:
                return 0, 0, 0

            # Dedupe within the batch on email — keep the LAST occurrence so
            # later rows in the same CSV override earlier ones. Without this,
            # ON CONFLICT errors out when one statement updates the same key twice.
            by_email: dict[str, dict] = {}
            for r in batch:
                by_email[r["email"]] = r
            rows = list(by_email.values())
            emails = list(by_email.keys())

            # Non-joiner uploads: no per-list matching (one cohort). Store
            # email + response only into webinar_nonjoiner_invites; the
            # Statistics Nonjoiners row intersects these with the auto-derived
            # cohort at read time.
            if kind == "nonjoiner":
                now = datetime.now(tz=timezone.utc)
                nj_rows = [{
                    "id": str(uuid.uuid4()),
                    "upload_id": upload_id,
                    "webinar_id": webinar_id,
                    "email": r["email"],
                    "calendar_invite_response": r["calendar_invite_response"],
                    "updated_at": now,
                } for r in rows]
                rows_affected = 0
                for attempt in range(3):
                    try:
                        async with engine.begin() as conn:
                            stmt = pg_insert(WebinarNonjoinerInvite.__table__).values(nj_rows)
                            stmt = stmt.on_conflict_do_update(
                                constraint="uq_wnji_webinar_email",
                                set_={
                                    "upload_id": stmt.excluded.upload_id,
                                    "calendar_invite_response": stmt.excluded.calendar_invite_response,
                                    "updated_at": stmt.excluded.updated_at,
                                },
                            )
                            result = await conn.execute(stmt)
                            rows_affected = result.rowcount or 0
                        break
                    except Exception as e:
                        if attempt < 2 and ("connection" in str(e).lower() or "timeout" in str(e).lower()):
                            print(f"[CAL_IMPORT] NJ upsert retry {attempt+1}: {e}")
                            await asyncio.sleep(1)
                        else:
                            raise
                # matched/unmatched aren't meaningful for nonjoiner uploads
                return 0, 0, rows_affected

            # Match: contacts assigned to this webinar via WebinarListAssignment
            match_map: dict[str, tuple[str, str]] = {}  # email → (assignment_id, contact_id)
            for attempt in range(3):
                try:
                    async with engine.begin() as conn:
                        mres = await conn.execute(
                            select(
                                sa_func.lower(Contact.__table__.c.email).label("email"),
                                Contact.__table__.c.id.label("contact_id"),
                                WebinarListAssignment.__table__.c.id.label("assignment_id"),
                            )
                            .select_from(
                                Contact.__table__.join(
                                    WebinarListAssignment.__table__,
                                    Contact.__table__.c.assignment_id == WebinarListAssignment.__table__.c.id,
                                )
                            )
                            .where(
                                WebinarListAssignment.__table__.c.webinar_id == webinar_id,
                                Contact.__table__.c.user_id == LLOYD_USER_ID,
                                sa_func.lower(Contact.__table__.c.email).in_(emails),
                            )
                        )
                        for row in mres:
                            # If a contact appears in multiple assignments, take the first hit
                            if row.email not in match_map:
                                match_map[row.email] = (row.assignment_id, row.contact_id)
                    break
                except Exception as e:
                    if attempt < 2 and ("connection" in str(e).lower() or "timeout" in str(e).lower()):
                        print(f"[CAL_IMPORT] Match retry {attempt+1}: {e}")
                        await asyncio.sleep(1)
                    else:
                        raise

            now = datetime.now(tz=timezone.utc)
            insert_rows: list[dict] = []
            local_matched = 0
            for r in rows:
                hit = match_map.get(r["email"])
                if hit:
                    local_matched += 1
                insert_rows.append({
                    "id": str(uuid.uuid4()),
                    "upload_id": upload_id,
                    "webinar_id": webinar_id,
                    "email": r["email"],
                    "calendar_invited_date": r["calendar_invited_date"],
                    "calendar_account": r["calendar_account"],
                    "calendar_account_prefix": r["calendar_account_prefix"],
                    "calendar_webinar_series": r["calendar_webinar_series"],
                    "calendar_invite_response": r["calendar_invite_response"],
                    "matched_assignment_id": hit[0] if hit else None,
                    "matched_contact_id": hit[1] if hit else None,
                    "updated_at": now,
                })

            local_unmatched = len(insert_rows) - local_matched

            # Upsert
            rows_affected = 0
            for attempt in range(3):
                try:
                    async with engine.begin() as conn:
                        stmt = pg_insert(WebinarCalendarInvite.__table__).values(insert_rows)
                        set_cols = {
                            "upload_id": stmt.excluded.upload_id,
                            "calendar_invited_date": stmt.excluded.calendar_invited_date,
                            "calendar_account": stmt.excluded.calendar_account,
                            "calendar_account_prefix": stmt.excluded.calendar_account_prefix,
                            "calendar_webinar_series": stmt.excluded.calendar_webinar_series,
                            "calendar_invite_response": stmt.excluded.calendar_invite_response,
                            "matched_assignment_id": stmt.excluded.matched_assignment_id,
                            "matched_contact_id": stmt.excluded.matched_contact_id,
                            "updated_at": stmt.excluded.updated_at,
                        }
                        stmt = stmt.on_conflict_do_update(
                            constraint="uq_wci_webinar_email",
                            set_=set_cols,
                        )
                        result = await conn.execute(stmt)
                        rows_affected = result.rowcount or 0
                    break
                except Exception as e:
                    if attempt < 2 and ("connection" in str(e).lower() or "timeout" in str(e).lower()):
                        print(f"[CAL_IMPORT] Upsert retry {attempt+1}: {e}")
                        await asyncio.sleep(1)
                    else:
                        raise

            return local_matched, local_unmatched, rows_affected

        batch: list[dict] = []
        for raw in reader:
            if not any(cell.strip() for cell in raw):
                continue
            parsed = _parse_row(raw)
            if not parsed:
                # Row had no email — count toward processed but skip
                processed += 1
                continue
            if parsed.get("calendar_account"):
                accounts_seen.add(parsed["calendar_account"])
            batch.append(parsed)

            if len(batch) >= BATCH_SIZE:
                # Cancel check
                if _import_cancel_flags.get(upload_id, False):
                    print(f"[CAL_IMPORT] Cancelled: {upload_id} at row {processed}")
                    async with engine.begin() as conn:
                        await conn.execute(
                            update(WebinarCalendarUpload.__table__)
                            .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                            .values(status="cancelled", processed_rows=processed,
                                    matched_count=matched, unmatched_count=unmatched)
                        )
                    csv_file.close()
                    return

                pause_event = _import_pause_events.get(upload_id)
                if pause_event and not pause_event.is_set():
                    await pause_event.wait()
                    if _import_cancel_flags.get(upload_id, False):
                        async with engine.begin() as conn:
                            await conn.execute(
                                update(WebinarCalendarUpload.__table__)
                                .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                                .values(status="cancelled", processed_rows=processed,
                                        matched_count=matched, unmatched_count=unmatched)
                            )
                        csv_file.close()
                        return

                m, u, r = await _flush_batch(batch)
                matched += m
                unmatched += u
                upserted += r
                processed += len(batch)
                batch = []

                total = max(total_rows_estimate, processed)
                pct = min(99, int((processed / total) * 100))
                elapsed = _time.monotonic() - start
                rate = processed / elapsed if elapsed > 0 else 0
                print(f"[CAL_IMPORT] {processed}/{total} ({pct}%) — {rate:.0f} rows/s — {matched} matched")
                async with engine.begin() as conn:
                    await conn.execute(
                        update(WebinarCalendarUpload.__table__)
                        .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                        .values(progress=pct, processed_rows=processed,
                                matched_count=matched, unmatched_count=unmatched)
                    )
                await asyncio.sleep(0)

        if batch:
            m, u, r = await _flush_batch(batch)
            matched += m
            unmatched += u
            upserted += r
            processed += len(batch)

        csv_file.close()

        async with engine.begin() as conn:
            await conn.execute(
                update(WebinarCalendarUpload.__table__)
                .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                .values(
                    status="complete",
                    progress=100,
                    processed_rows=processed,
                    matched_count=matched,
                    unmatched_count=unmatched,
                    completed_at=datetime.now(tz=timezone.utc),
                )
            )

        # Pattern A: stamp every distinct calendar_account in this CSV with
        # the sender chosen at upload time. Last write wins. (Calendar uploads
        # only — Non-joiner CSVs carry no calendar_account.)
        if kind != "nonjoiner" and sender_id and accounts_seen:
            now = datetime.now(tz=timezone.utc)
            cas_rows = [
                {
                    "id": str(uuid.uuid4()),
                    "user_id": LLOYD_USER_ID,
                    "webinar_id": webinar_id,
                    "calendar_account": acc,
                    "sender_id": sender_id,
                    "updated_at": now,
                }
                for acc in accounts_seen
            ]
            async with engine.begin() as conn:
                stmt = pg_insert(CalendarAccountSender.__table__).values(cas_rows)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_cas_webinar_account",
                    set_={
                        "sender_id": stmt.excluded.sender_id,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await conn.execute(stmt)
            print(f"[CAL_IMPORT] Stamped {len(cas_rows)} accounts with sender {sender_id}")

        # Storage cleanup (best effort)
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    _storage_url(f"/storage/v1/object/{CSV_BUCKET}/{storage_path}"),
                    headers=_supabase_headers(),
                    timeout=30.0,
                )
        except Exception:
            pass

        print(f"[CAL_IMPORT] Done: {upload_id} — {processed} rows, {matched} matched, {unmatched} no-list")

    except Exception as e:
        print(f"[CAL_IMPORT] FAILED: {upload_id} at row {processed} — {e}")
        traceback.print_exc()
        err = f"Import stopped at row {processed:,}. {matched:,} matched. Error: {str(e)[:300]}"
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    update(WebinarCalendarUpload.__table__)
                    .where(WebinarCalendarUpload.__table__.c.id == upload_id)
                    .values(
                        status="failed",
                        error_message=err,
                        processed_rows=processed,
                        matched_count=matched,
                        unmatched_count=unmatched,
                    )
                )
        except Exception:
            pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
