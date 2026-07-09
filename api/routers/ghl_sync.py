"""GHL sync router — status, history, manual trigger, schedule settings."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from db.models import GHLSyncRun, GHLSyncSettings
from db.session import get_db
from services import ghl_scheduler
from services.ghl_sync import (
    recover_orphaned_runs,
    request_cancel,
    run_opportunities_sync,
    run_sync,
    run_webinar_sync,
    run_webinar_sync_full,
    sweep_stale_runs,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SyncRunResponse(BaseModel):
    id: str
    sync_type: str
    trigger: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: int | None = None
    contacts_synced: int
    opportunities_synced: int
    expected_total: int | None = None
    errors_count: int
    error_details: list | None = None
    cancel_requested: bool = False
    last_heartbeat_at: datetime | None = None


class SweepResponse(BaseModel):
    recovered: int
    swept: int


class SyncStatusResponse(BaseModel):
    latest: SyncRunResponse | None
    is_running: bool


class SyncHistoryResponse(BaseModel):
    runs: list[SyncRunResponse]


class SyncTriggerResponse(BaseModel):
    run_id: str
    sync_type: str
    status: str


class SyncSettingsResponse(BaseModel):
    incremental_enabled: bool
    incremental_interval_hours: int
    weekly_full_enabled: bool
    weekly_full_day_of_week: str
    weekly_full_hour_local: int
    weekly_full_timezone: str
    daily_sales_enabled: bool
    daily_sales_hour_local: int
    daily_sales_timezone: str
    updated_at: datetime | None = None


class SyncSettingsUpdate(BaseModel):
    incremental_enabled: bool | None = None
    incremental_interval_hours: int | None = None
    weekly_full_enabled: bool | None = None
    weekly_full_day_of_week: str | None = None
    weekly_full_hour_local: int | None = None
    weekly_full_timezone: str | None = None
    daily_sales_enabled: bool | None = None
    daily_sales_hour_local: int | None = None
    daily_sales_timezone: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_to_dict(r: GHLSyncRun) -> dict:
    return {
        "id": r.id,
        "sync_type": r.sync_type,
        "trigger": r.trigger,
        "status": r.status,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "duration_seconds": r.duration_seconds,
        "contacts_synced": r.contacts_synced,
        "opportunities_synced": r.opportunities_synced,
        "expected_total": r.expected_total,
        "errors_count": r.errors_count,
        "error_details": r.error_details,
        "cancel_requested": r.cancel_requested,
        "last_heartbeat_at": r.last_heartbeat_at,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/status", response_model=SyncStatusResponse)
async def sync_status(db: AsyncSession = Depends(get_db)):
    """Return the most recent sync run plus an is_running flag."""
    result = await db.execute(
        select(GHLSyncRun).order_by(GHLSyncRun.started_at.desc()).limit(1)
    )
    latest = result.scalar_one_or_none()
    is_running = latest is not None and latest.status == "running"
    return {
        "latest": _run_to_dict(latest) if latest else None,
        "is_running": is_running,
    }


@router.get("/history", response_model=SyncHistoryResponse)
async def sync_history(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Return the N most recent sync runs (newest first)."""
    result = await db.execute(
        select(GHLSyncRun).order_by(GHLSyncRun.started_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return {"runs": [_run_to_dict(r) for r in runs]}


@router.post("/trigger", response_model=SyncTriggerResponse, status_code=202)
async def trigger_sync(
    sync_type: str = Query("incremental", pattern="^(full|incremental|opportunities)$"),
):
    """Kick off a sync as a background task. Returns immediately with run_id.

    - full / incremental: contacts + opportunities + appointments.
    - opportunities: all opportunities + their calendar appointments only
      (skips the contacts pull) — the focused Sales + Quality refresh.
    """
    # Reject if another sync is already running
    # (Race-free check happens inside the sync via asyncio.Lock)
    if sync_type == "opportunities":
        task = asyncio.create_task(run_opportunities_sync(trigger="manual"))
    else:
        task = asyncio.create_task(run_sync(sync_type, trigger="manual"))  # type: ignore[arg-type]

    # Wait briefly for the run row to be created so we can return its id
    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    # Look up the run row we just created
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GHLSyncRun).order_by(GHLSyncRun.started_at.desc()).limit(1)
        )
        run = result.scalar_one_or_none()

    if run is None:
        # Task may have errored before inserting the run row
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start sync")

    return {"run_id": run.id, "sync_type": sync_type, "status": run.status}


@router.post("/trigger-webinar", response_model=SyncTriggerResponse, status_code=202)
async def trigger_webinar_sync(
    n: int = Query(..., ge=1, le=9999, description="Webinar number to sync"),
    phase: str = Query(
        "full",
        pattern="^(narrow|deep|full)$",
        description="narrow = fast (~2 min, ~1.5k contacts), deep = GCal invited base (~3h, ~200k), full = narrow then deep",
    ),
):
    """Kick off a per-webinar sync.

    - phase=narrow (fast, default for quick stats)
    - phase=deep (slow, backfills the 200k-row GCal-invited base)
    - phase=full (narrow then deep, sequential; stats usable after narrow)

    Returns immediately with the run_id of the phase that just started.
    """
    if phase == "full":
        task = asyncio.create_task(run_webinar_sync_full(n, trigger="manual"))  # type: ignore[arg-type]
    elif phase == "deep":
        task = asyncio.create_task(run_webinar_sync(n, trigger="manual", deep=True))  # type: ignore[arg-type]
    else:
        task = asyncio.create_task(run_webinar_sync(n, trigger="manual", deep=False))  # type: ignore[arg-type]

    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GHLSyncRun).order_by(GHLSyncRun.started_at.desc()).limit(1)
        )
        run = result.scalar_one_or_none()

    if run is None:
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start webinar sync")

    return {"run_id": run.id, "sync_type": run.sync_type, "status": run.status}


@router.post("/runs/{run_id}/cancel", response_model=SyncRunResponse)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    """Cooperatively cancel a running sync.

    Sets cancel_requested=true and (best-effort) cancels the asyncio task
    in this process. The sync loop catches asyncio.CancelledError at the
    next batch boundary and finalizes the row as 'cancelled'. Returns
    the updated row immediately — the status flips to 'cancelled' a
    moment later, picked up by the UI's poll.
    """
    accepted = await request_cancel(run_id)
    if not accepted:
        result = await db.execute(select(GHLSyncRun).where(GHLSyncRun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="Sync run not found")
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel run with status '{run.status}'",
        )
    result = await db.execute(select(GHLSyncRun).where(GHLSyncRun.id == run_id))
    run = result.scalar_one()
    return _run_to_dict(run)


@router.post("/admin/recover-stale", response_model=SweepResponse)
async def admin_recover_stale():
    """Manually trigger orphan recovery + stale-heartbeat sweep. Useful for
    cleaning up rows left over from a crash before the periodic sweeper
    runs its next pass.
    """
    recovered = await recover_orphaned_runs()
    swept = await sweep_stale_runs()
    return {"recovered": recovered, "swept": swept}


@router.get("/settings", response_model=SyncSettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(GHLSyncSettings).where(GHLSyncSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        # Seed singleton
        s = GHLSyncSettings(id=1)
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return {
        "incremental_enabled": s.incremental_enabled,
        "incremental_interval_hours": s.incremental_interval_hours,
        "weekly_full_enabled": s.weekly_full_enabled,
        "weekly_full_day_of_week": s.weekly_full_day_of_week,
        "weekly_full_hour_local": s.weekly_full_hour_local,
        "weekly_full_timezone": s.weekly_full_timezone,
        "daily_sales_enabled": s.daily_sales_enabled,
        "daily_sales_hour_local": s.daily_sales_hour_local,
        "daily_sales_timezone": s.daily_sales_timezone,
        "updated_at": s.updated_at,
    }


VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


@router.patch("/settings", response_model=SyncSettingsResponse)
async def update_settings(
    payload: SyncSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    values: dict = {}
    if payload.incremental_enabled is not None:
        values["incremental_enabled"] = payload.incremental_enabled
    if payload.incremental_interval_hours is not None:
        if not (1 <= payload.incremental_interval_hours <= 24):
            raise HTTPException(status_code=422, detail="interval_hours must be 1..24")
        values["incremental_interval_hours"] = payload.incremental_interval_hours
    if payload.weekly_full_enabled is not None:
        values["weekly_full_enabled"] = payload.weekly_full_enabled
    if payload.weekly_full_day_of_week is not None:
        day = payload.weekly_full_day_of_week.lower()
        if day not in VALID_DAYS:
            raise HTTPException(status_code=422, detail=f"day_of_week must be one of {sorted(VALID_DAYS)}")
        values["weekly_full_day_of_week"] = day
    if payload.weekly_full_hour_local is not None:
        if not (0 <= payload.weekly_full_hour_local <= 23):
            raise HTTPException(status_code=422, detail="hour must be 0..23")
        values["weekly_full_hour_local"] = payload.weekly_full_hour_local
    if payload.weekly_full_timezone is not None:
        values["weekly_full_timezone"] = payload.weekly_full_timezone
    if payload.daily_sales_enabled is not None:
        values["daily_sales_enabled"] = payload.daily_sales_enabled
    if payload.daily_sales_hour_local is not None:
        if not (0 <= payload.daily_sales_hour_local <= 23):
            raise HTTPException(status_code=422, detail="hour must be 0..23")
        values["daily_sales_hour_local"] = payload.daily_sales_hour_local
    if payload.daily_sales_timezone is not None:
        values["daily_sales_timezone"] = payload.daily_sales_timezone

    if values:
        await db.execute(update(GHLSyncSettings).where(GHLSyncSettings.id == 1).values(**values))
        await db.commit()

    # Reload schedules to pick up new settings
    try:
        await ghl_scheduler.reload_schedules()
    except Exception as exc:
        logger.warning("Failed to reload scheduler: %s", exc)

    # Return fresh state
    result = await db.execute(select(GHLSyncSettings).where(GHLSyncSettings.id == 1))
    s = result.scalar_one()
    return {
        "incremental_enabled": s.incremental_enabled,
        "incremental_interval_hours": s.incremental_interval_hours,
        "weekly_full_enabled": s.weekly_full_enabled,
        "weekly_full_day_of_week": s.weekly_full_day_of_week,
        "weekly_full_hour_local": s.weekly_full_hour_local,
        "weekly_full_timezone": s.weekly_full_timezone,
        "daily_sales_enabled": s.daily_sales_enabled,
        "daily_sales_hour_local": s.daily_sales_hour_local,
        "daily_sales_timezone": s.daily_sales_timezone,
        "updated_at": s.updated_at,
    }
