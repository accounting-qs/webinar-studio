"""Blocklist router: emails excluded from outreach."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import BlocklistEntry, GHLContact, WebinarGeekSubscriber
from db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Chunk size for bulk INSERT ... ON CONFLICT. Postgres caps a single
# statement at 32,767 bind parameters; with 5 cols per blocklist row,
# 1000 rows = 5000 params, well under the limit. Matches the batch
# size used by services/ghl_sync.py.
_BACKFILL_CHUNK_SIZE = 1000


async def _chunked_blocklist_upsert(db: AsyncSession, payload: list[dict]) -> int:
    added = 0
    for i in range(0, len(payload), _BACKFILL_CHUNK_SIZE):
        chunk = payload[i : i + _BACKFILL_CHUNK_SIZE]
        stmt = pg_insert(BlocklistEntry).values(chunk).on_conflict_do_nothing(
            index_elements=["user_id", "email"]
        )
        result = await db.execute(stmt)
        added += result.rowcount or 0
    return added


class BlocklistAddRequest(BaseModel):
    email: str
    reason: Optional[str] = None


class BlocklistBulkRequest(BaseModel):
    emails: list[str]
    reason: Optional[str] = None


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _valid_email(email: str) -> bool:
    return "@" in email and "." in email.split("@")[-1]


def _serialize(e: BlocklistEntry) -> dict:
    return {
        "id": e.id,
        "email": e.email,
        "source": e.source,
        "reason": e.reason,
        "source_ref": e.source_ref,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("")
async def list_blocklist(
    q: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    stmt = select(BlocklistEntry).where(BlocklistEntry.user_id == LLOYD_USER_ID)
    count_stmt = select(func.count()).select_from(BlocklistEntry).where(
        BlocklistEntry.user_id == LLOYD_USER_ID
    )
    if q:
        like = f"%{_normalize(q)}%"
        stmt = stmt.where(BlocklistEntry.email.ilike(like))
        count_stmt = count_stmt.where(BlocklistEntry.email.ilike(like))
    if source:
        stmt = stmt.where(BlocklistEntry.source == source)
        count_stmt = count_stmt.where(BlocklistEntry.source == source)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(BlocklistEntry.created_at.desc()).limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()

    src_rows = (await db.execute(
        select(BlocklistEntry.source, func.count())
        .where(BlocklistEntry.user_id == LLOYD_USER_ID)
        .group_by(BlocklistEntry.source)
    )).all()
    by_source = {row[0]: row[1] for row in src_rows}

    return {
        "entries": [_serialize(r) for r in rows],
        "total": total,
        "by_source": by_source,
    }


@router.post("", status_code=201)
async def add_blocklist_entry(
    body: BlocklistAddRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    email = _normalize(body.email)
    if not _valid_email(email):
        raise HTTPException(400, "Valid email is required")
    stmt = pg_insert(BlocklistEntry).values(
        user_id=LLOYD_USER_ID, email=email, source="manual", reason=body.reason,
    ).on_conflict_do_nothing(index_elements=["user_id", "email"])
    await db.execute(stmt)

    row = (await db.execute(
        select(BlocklistEntry).where(
            BlocklistEntry.user_id == LLOYD_USER_ID,
            BlocklistEntry.email == email,
        )
    )).scalar_one()
    return _serialize(row)


@router.post("/bulk", status_code=201)
async def bulk_add_blocklist(
    body: BlocklistBulkRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if not body.emails:
        return {"added": 0, "skipped": 0, "invalid": 0}

    invalid = 0
    seen: set[str] = set()
    rows: list[dict] = []
    for raw in body.emails:
        e = _normalize(raw)
        if not _valid_email(e):
            invalid += 1
            continue
        if e in seen:
            continue
        seen.add(e)
        rows.append({
            "user_id": LLOYD_USER_ID,
            "email": e,
            "source": "csv",
            "reason": body.reason,
        })

    if not rows:
        return {"added": 0, "skipped": 0, "invalid": invalid}

    stmt = pg_insert(BlocklistEntry).values(rows).on_conflict_do_nothing(
        index_elements=["user_id", "email"]
    )
    result = await db.execute(stmt)
    inserted = result.rowcount or 0
    skipped = len(rows) - inserted
    return {"added": inserted, "skipped": skipped, "invalid": invalid}


@router.delete("/{entry_id}", status_code=204)
async def delete_blocklist_entry(
    entry_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        delete(BlocklistEntry).where(
            BlocklistEntry.id == entry_id,
            BlocklistEntry.user_id == LLOYD_USER_ID,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(404, "Entry not found")


@router.post("/backfill")
async def backfill_blocklist(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Scan already-synced WebinarGeek subscribers + GHL contacts and upsert
    any unsubscribes/DNDs into the blocklist. Idempotent — existing entries
    keep their original source/created_at via ON CONFLICT DO NOTHING.
    """
    added_wg = 0
    added_ghl = 0

    # WebinarGeek unsubscribes
    wg_rows = (await db.execute(
        select(
            WebinarGeekSubscriber.email,
            WebinarGeekSubscriber.unsubscribe_source,
            WebinarGeekSubscriber.subscriber_id,
        ).where(WebinarGeekSubscriber.unsubscribed_at.isnot(None))
    )).all()
    wg_payload: list[dict] = []
    seen_wg: set[str] = set()
    for email, unsub_source, sub_id in wg_rows:
        e = _normalize(email or "")
        if not _valid_email(e) or e in seen_wg:
            continue
        seen_wg.add(e)
        wg_payload.append({
            "user_id": LLOYD_USER_ID,
            "email": e,
            "source": "wg_unsub",
            "reason": unsub_source or "WebinarGeek unsubscribed",
            "source_ref": sub_id,
        })
    if wg_payload:
        added_wg = await _chunked_blocklist_upsert(db, wg_payload)

    # GHL cold-calendar unsubscribes
    ghl_rows = (await db.execute(
        select(GHLContact.email, GHLContact.ghl_contact_id)
        .where(
            GHLContact.cold_calendar_unsubscribe_date.isnot(None),
            GHLContact.email.isnot(None),
        )
    )).all()
    ghl_payload: list[dict] = []
    seen_ghl: set[str] = set()
    for email, contact_id in ghl_rows:
        e = _normalize(email or "")
        if not _valid_email(e) or e in seen_ghl:
            continue
        seen_ghl.add(e)
        ghl_payload.append({
            "user_id": LLOYD_USER_ID,
            "email": e,
            "source": "ghl_dnd",
            "reason": "GHL cold calendar unsubscribe",
            "source_ref": contact_id,
        })
    if ghl_payload:
        added_ghl = await _chunked_blocklist_upsert(db, ghl_payload)

    return {
        "wg_scanned": len(wg_rows),
        "wg_added": added_wg,
        "ghl_scanned": len(ghl_rows),
        "ghl_added": added_ghl,
    }
