"""Persisted statistics snapshots — precompute & store the heavy per-webinar
statistics payloads so the dashboard reads them back instantly.

Read path: services.statistics.get_statistics_webinar_one() consults a snapshot
before falling back to a live (~30s) compute, and get_statistics_segments()
reads every snapshot in one query.

Write path: recompute() computes fresh payloads (bypassing caches) and upserts
them. It's scheduled fire-and-forget from every source-data choke-point
(GHL sync, WebinarGeek sync, calendar upload, webinar edit) and from the manual
POST /statistics/recompute trigger. A single asyncio lock serializes runs so two
recomputes never overlap; identical "all" requests are coalesced.

Status (running / progress) is process-local; "last updated" is read from the
snapshot table so it survives restarts. The app runs a single uvicorn worker
(see services.statistics cache note), so process-local status is sufficient.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _snap_source(source: str) -> str:
    """Canonical snapshot source key. 'auto'/'ghl' both map to 'ghl' so reads
    and writes line up regardless of which alias the caller passed."""
    return "workbook" if source == "workbook" else "ghl"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

async def read_snapshot_payload(source: str, webinar_id: str) -> dict[str, Any] | None:
    from sqlalchemy import select
    from db.models import StatisticsSnapshot
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(StatisticsSnapshot.payload).where(
                StatisticsSnapshot.source == _snap_source(source),
                StatisticsSnapshot.webinar_id == webinar_id,
            )
        )
        return r.scalar_one_or_none()


async def read_all_payloads(source: str) -> dict[str, dict[str, Any]]:
    """Every snapshot payload for a source, keyed by webinar_id. One query —
    powers the Segments rollup without N per-webinar reads."""
    from sqlalchemy import select
    from db.models import StatisticsSnapshot
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(StatisticsSnapshot.webinar_id, StatisticsSnapshot.payload).where(
                StatisticsSnapshot.source == _snap_source(source)
            )
        )).all()
    return {wid: payload for wid, payload in rows}


async def _upsert_snapshot(source: str, payload: dict[str, Any]) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.models import StatisticsSnapshot
    from db.session import AsyncSessionLocal

    import json

    snap_source = _snap_source(source)
    webinar_id = payload.get("webinarId") or payload.get("id")
    if not webinar_id:
        return
    # Guarantee a JSON-native payload (defends against a stray Decimal/datetime
    # in a metric value so the JSONB insert can't fail mid-recompute).
    clean = json.loads(json.dumps(payload, default=str))
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        stmt = pg_insert(StatisticsSnapshot).values(
            source=snap_source,
            webinar_id=webinar_id,
            webinar_number=payload.get("number"),
            variant_label=payload.get("variantLabel"),
            payload=clean,
            computed_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "webinar_id"],
            set_={
                "webinar_number": stmt.excluded.webinar_number,
                "variant_label": stmt.excluded.variant_label,
                "payload": stmt.excluded.payload,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        await db.execute(stmt)
        await db.commit()


async def _prune_snapshots(source: str, keep_webinar_ids: set[str]) -> int:
    """Delete snapshots for webinars no longer in the passed set (e.g. removed
    in Planning). Only used by the full recompute. Returns rows deleted."""
    from sqlalchemy import delete, select
    from db.models import StatisticsSnapshot
    from db.session import AsyncSessionLocal

    snap_source = _snap_source(source)
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(StatisticsSnapshot.webinar_id).where(
                StatisticsSnapshot.source == snap_source
            )
        )).scalars().all()
        stale = [wid for wid in existing if wid not in keep_webinar_ids]
        if not stale:
            return 0
        await db.execute(
            delete(StatisticsSnapshot).where(
                StatisticsSnapshot.source == snap_source,
                StatisticsSnapshot.webinar_id.in_(stale),
            )
        )
        await db.commit()
    return len(stale)


# ---------------------------------------------------------------------------
# Recompute status (process-local for live progress; DB for "last updated")
# ---------------------------------------------------------------------------

_status: dict[str, Any] = {
    "running": False,
    "scope": None,          # "all" | "partial"
    "started_at": None,     # ISO string
    "finished_at": None,    # ISO string
    "total": 0,
    "done": 0,
    "errors": 0,
    "last_error": None,
}


async def get_status(source: str = "auto") -> dict[str, Any]:
    from sqlalchemy import func, select
    from db.models import StatisticsSnapshot
    from db.session import AsyncSessionLocal

    last_computed_at = None
    snapshot_count = 0
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.max(StatisticsSnapshot.computed_at), func.count())
                .where(StatisticsSnapshot.source == _snap_source(source))
            )
            row = r.one()
            last_computed_at = row[0].isoformat() if row[0] else None
            snapshot_count = int(row[1] or 0)
    except Exception as exc:  # table missing before migration, etc.
        logger.warning("get_status: snapshot read failed: %s", exc)

    return {
        **_status,
        "last_computed_at": last_computed_at,
        "snapshot_count": snapshot_count,
    }


# ---------------------------------------------------------------------------
# Recompute worker
# ---------------------------------------------------------------------------

_recompute_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()
# Scope tokens already queued/running, so duplicate schedules coalesce. The
# sentinel "*" means a full ("all") recompute.
_pending_scopes: set[str] = set()
_ALL = "*"


async def recompute(webinar_ids: list[str] | None = None, source: str = "auto") -> dict[str, Any]:
    """Compute fresh statistics payloads and upsert them. webinar_ids=None
    rebuilds every passed webinar (and prunes snapshots for webinars that no
    longer pass). Serialized: only one recompute runs at a time."""
    from services import statistics as stats

    async with _recompute_lock:
        is_full = webinar_ids is None
        if is_full:
            summaries = await stats.get_statistics_webinar_list(source=source)
            targets = [
                s["webinarId"] for s in summaries
                if s.get("webinarId") and stats._is_passed_webinar(s.get("date"), s.get("status"))
            ]
        else:
            targets = list(dict.fromkeys(webinar_ids))  # de-dupe, preserve order

        _status.update({
            "running": True,
            "scope": "all" if is_full else "partial",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "total": len(targets),
            "done": 0,
            "errors": 0,
            "last_error": None,
        })

        try:
            for wid in targets:
                try:
                    payload = await stats.get_statistics_webinar_one(
                        source=source, webinar_id=wid, force=True,
                    )
                    if payload is not None:
                        await _upsert_snapshot(source, payload)
                except Exception as exc:
                    _status["errors"] += 1
                    _status["last_error"] = str(exc)[:300]
                    logger.warning("recompute: webinar %s failed: %s", wid, exc)
                finally:
                    _status["done"] += 1

            if is_full:
                try:
                    await _prune_snapshots(source, set(targets))
                except Exception as exc:
                    logger.warning("recompute: prune failed: %s", exc)
        finally:
            _status["running"] = False
            _status["finished_at"] = datetime.now(timezone.utc).isoformat()

    return await get_status(source)


# ---------------------------------------------------------------------------
# Fire-and-forget scheduling (used by sync choke-points + manual trigger)
# ---------------------------------------------------------------------------

def _spawn(scope_token: str, coro_factory) -> None:
    """Schedule a recompute as a background task, coalescing duplicate scopes.
    No-ops cleanly if there's no running event loop."""
    if scope_token in _pending_scopes:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("schedule_recompute: no running loop; skipping %s", scope_token)
        return

    _pending_scopes.add(scope_token)

    async def _runner():
        try:
            await coro_factory()
        except Exception as exc:
            logger.warning("recompute task %s failed: %s", scope_token, exc)
        finally:
            _pending_scopes.discard(scope_token)

    task = loop.create_task(_runner())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def schedule_recompute(webinar_ids: list[str] | None = None, source: str = "auto") -> None:
    """Background full ("all") recompute, or a partial set. Identical full
    requests already in flight are coalesced."""
    if webinar_ids is None:
        # Optimistically flip status to running so the manual-trigger endpoint's
        # immediate get_status() (and the control's poll) see the run before the
        # background task has actually started. recompute() overwrites this with
        # real progress on start and clears it on finish. Guarded so we don't
        # stomp the live progress of an already-running recompute.
        if not _status["running"]:
            _status.update({
                "running": True, "scope": "all",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None, "total": 0, "done": 0,
                "errors": 0, "last_error": None,
            })
        _spawn(f"{_ALL}:{_snap_source(source)}", lambda: recompute(None, source=source))
    else:
        ids = [w for w in webinar_ids if w]
        if not ids:
            return
        token = f"ids:{_snap_source(source)}:" + ",".join(sorted(ids))
        _spawn(token, lambda: recompute(ids, source=source))


def schedule_recompute_for_webinar(webinar_id: str | None, source: str = "auto") -> None:
    if webinar_id:
        schedule_recompute([webinar_id], source=source)


def schedule_recompute_for_number(webinar_number: int, source: str = "auto") -> None:
    """Recompute every variant of a webinar number (resolved off the DB)."""
    async def _run():
        from sqlalchemy import select
        from db.models import Webinar
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(
                select(Webinar.id).where(Webinar.number == webinar_number)
            )).scalars().all()
        if ids:
            await recompute(list(ids), source=source)

    _spawn(f"num:{_snap_source(source)}:{webinar_number}", _run)


def schedule_recompute_for_broadcast(broadcast_id: str | None, source: str = "auto") -> None:
    """Recompute every webinar linked to a WebinarGeek broadcast id."""
    if not broadcast_id:
        return

    async def _run():
        from sqlalchemy import select
        from db.models import Webinar
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(
                select(Webinar.id).where(Webinar.broadcast_id == broadcast_id)
            )).scalars().all()
        if ids:
            await recompute(list(ids), source=source)

    _spawn(f"bcast:{_snap_source(source)}:{broadcast_id}", _run)
