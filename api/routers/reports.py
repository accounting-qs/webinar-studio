"""Weekly report router — schedule settings + manual test send."""
from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from db.models import ReportSettings
from db.session import get_db
from services import ghl_scheduler
from services.weekly_report import VALID_DAYS, send_weekly_report

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ReportSettingsResponse(BaseModel):
    enabled: bool
    day_of_week: str
    hour_local: int
    minute_local: int
    timezone: str
    recipients: list[str]
    from_address: str
    last_sent_at: str | None = None
    last_error: str | None = None


class ReportSettingsUpdate(BaseModel):
    enabled: bool | None = None
    day_of_week: str | None = None
    hour_local: int | None = None
    minute_local: int | None = None
    timezone: str | None = None
    recipients: list[str] | None = None
    from_address: str | None = None


class TestSendRequest(BaseModel):
    webinar_id: str | None = None


class TestSendResponse(BaseModel):
    ok: bool
    message_id: str | None = None
    error: str | None = None
    webinar_number: int | None = None
    narrative_included: bool | None = None


def _settings_response(s: ReportSettings) -> dict:
    return {
        "enabled": s.enabled,
        "day_of_week": s.day_of_week,
        "hour_local": s.hour_local,
        "minute_local": s.minute_local,
        "timezone": s.timezone,
        "recipients": list(s.recipients or []),
        "from_address": s.from_address,
        "last_sent_at": s.last_sent_at.isoformat() if s.last_sent_at else None,
        "last_error": s.last_error,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/settings", response_model=ReportSettingsResponse)
async def get_settings(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ReportSettings).where(ReportSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        # Seed singleton
        s = ReportSettings(id=1)
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return _settings_response(s)


@router.patch("/settings", response_model=ReportSettingsResponse)
async def update_settings(
    payload: ReportSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    values: dict = {}
    if payload.enabled is not None:
        values["enabled"] = payload.enabled
    if payload.day_of_week is not None:
        day = payload.day_of_week.lower()
        if day not in VALID_DAYS:
            raise HTTPException(status_code=422, detail=f"day_of_week must be one of {sorted(VALID_DAYS)}")
        values["day_of_week"] = day
    if payload.hour_local is not None:
        if not (0 <= payload.hour_local <= 23):
            raise HTTPException(status_code=422, detail="hour must be 0..23")
        values["hour_local"] = payload.hour_local
    if payload.minute_local is not None:
        if not (0 <= payload.minute_local <= 59):
            raise HTTPException(status_code=422, detail="minute must be 0..59")
        values["minute_local"] = payload.minute_local
    if payload.timezone is not None:
        try:
            ZoneInfo(payload.timezone)
        except Exception:
            raise HTTPException(status_code=422, detail=f"Unknown timezone '{payload.timezone}'")
        values["timezone"] = payload.timezone
    if payload.recipients is not None:
        cleaned: list[str] = []
        for r in payload.recipients:
            email = r.strip().lower()
            if not email:
                continue
            if not _EMAIL_RE.match(email):
                raise HTTPException(status_code=422, detail=f"Invalid recipient email '{r}'")
            if email not in cleaned:
                cleaned.append(email)
        if not cleaned:
            raise HTTPException(status_code=422, detail="recipients must contain at least one email")
        values["recipients"] = cleaned
    if payload.from_address is not None:
        from_addr = payload.from_address.strip()
        # Allow "Name <addr@domain>" or a bare address.
        bare = from_addr
        m = re.match(r"^.*<([^>]+)>$", from_addr)
        if m:
            bare = m.group(1).strip()
        if not _EMAIL_RE.match(bare):
            raise HTTPException(status_code=422, detail=f"Invalid from_address '{payload.from_address}'")
        values["from_address"] = from_addr

    # Ensure the singleton exists before updating (PATCH may arrive first).
    result = await db.execute(select(ReportSettings).where(ReportSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        s = ReportSettings(id=1)
        db.add(s)
        await db.commit()

    if values:
        await db.execute(update(ReportSettings).where(ReportSettings.id == 1).values(**values))
        await db.commit()

    # Reload schedules to pick up new settings
    try:
        await ghl_scheduler.reload_schedules()
    except Exception as exc:
        logger.warning("Failed to reload scheduler: %s", exc)

    result = await db.execute(select(ReportSettings).where(ReportSettings.id == 1))
    s = result.scalar_one()
    return _settings_response(s)


@router.post("/send-test", response_model=TestSendResponse)
async def send_test(payload: TestSendRequest | None = None):
    """Generate + send the report now (ignores the enabled toggle). Returns
    200 with ok=false on failure so the UI can show the error inline."""
    webinar_id = payload.webinar_id if payload else None
    result = await send_weekly_report(test=True, webinar_id=webinar_id)
    return TestSendResponse(
        ok=bool(result.get("ok")),
        message_id=result.get("message_id"),
        error=result.get("error"),
        webinar_number=result.get("webinar_number"),
        narrative_included=result.get("narrative_included"),
    )
