"""
Connectors router — webinar platform integrations (WebinarGeek + Zoom) and the
other third-party credentials.

Both webinar platforms cache into the same shared tables (webinar_broadcasts /
webinar_registrants, discriminated by `provider`), so every WebinarGeek-facing
query here must stay provider-scoped or it will start picking up Zoom rows.

Endpoints:
  Credentials:
    GET    /connectors/webinargeek
    PUT    /connectors/webinargeek
    DELETE /connectors/webinargeek

  Broadcasts (cached):
    GET    /connectors/webinargeek/webinars?limit=&offset=&q=
    POST   /connectors/webinargeek/webinars/refresh
    POST   /connectors/webinargeek/webinars/sync-all   (sync subscribers for all)
    POST   /connectors/webinargeek/webinars/{broadcast_id}/sync

  Subscribers (cached):
    GET    /connectors/webinargeek/subscribers?broadcast_id=&q=&limit=&offset=
    GET    /connectors/webinargeek/subscribers/export?broadcast_id=   (CSV)

  Zoom (Server-to-Server OAuth, single account):
    GET    /connectors/zoom                            (status + required scopes)
    PUT    /connectors/zoom                            (verifies before saving)
    DELETE /connectors/zoom
    GET    /connectors/zoom/webinars?limit=&offset=&q=
    POST   /connectors/zoom/webinars/refresh
    POST   /connectors/zoom/webinars/sync-all
    POST   /connectors/zoom/webinars/{broadcast_id}/sync
    GET    /connectors/zoom/registrants?broadcast_id=&q=&limit=&offset=
    GET    /connectors/zoom/registrants/export?broadcast_id=          (CSV)
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, delete, or_, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from db.models import (
    ConnectorCredential, GHLSyncRun, WebinarBroadcast, WebinarRegistrant,
)
from db.session import AsyncSessionLocal, get_db
from integrations import webinargeek_client as wg
from integrations import openai_client as oai
from integrations import ghl_client as ghl
from integrations import zoom_client as zc
from services import wg_sync, zoom_sync

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_auth)])

PROVIDER = "webinargeek"
OPENAI_PROVIDER = "openai"
GHL_PROVIDER = "ghl"
ZOOM_PROVIDER = "zoom"
ANTHROPIC_PROVIDER = "anthropic"
RESEND_PROVIDER = "resend"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CredentialStatus(BaseModel):
    configured: bool
    api_key_masked: Optional[str] = None


class SetCredentialRequest(BaseModel):
    api_key: str


class GhlCredentialStatus(BaseModel):
    configured: bool
    api_key_masked: Optional[str] = None
    location_id: Optional[str] = None
    pipeline_id: Optional[str] = None
    source: str  # "db" | "env" | "none"


class SetGhlCredentialRequest(BaseModel):
    api_key: str
    location_id: str
    pipeline_id: Optional[str] = None


class BroadcastOut(BaseModel):
    broadcast_id: str
    name: str
    internal_title: Optional[str] = None
    starts_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    subscriptions_count: int = 0
    live_viewers_count: int = 0
    replay_viewers_count: int = 0
    has_ended: bool = False
    cancelled: bool = False
    last_synced_at: Optional[datetime] = None
    synced_subscriber_count: int = 0
    credential_id: Optional[str] = None
    credential_name: Optional[str] = None


class BroadcastListResponse(BaseModel):
    broadcasts: list[BroadcastOut]
    total: int


class RefreshResponse(BaseModel):
    count: int


class SyncResponse(BaseModel):
    """Returned when a single-broadcast sync is queued in the background.

    Frontend should redirect users to the Sync page (or just toast a
    "started" message) — final counts are tracked on the sync_run row.
    """
    broadcast_id: str
    run_id: str
    status: str


class SyncAllResponse(BaseModel):
    """Returned when sync-all is queued. Each broadcast gets its own
    sync_run row in addition to this umbrella row; the UI sees all of them
    on the Sync page.
    """
    run_id: str
    status: str
    broadcasts_queued: int


class SubscriberOut(BaseModel):
    id: str
    broadcast_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    registration_source: Optional[str] = None
    subscribed_at: Optional[datetime] = None
    watched_live: Optional[bool] = None
    watched_replay: Optional[bool] = None
    minutes_viewing: Optional[int] = None
    viewing_device: Optional[str] = None
    viewing_country: Optional[str] = None


class SubscriberListResponse(BaseModel):
    subscribers: list[SubscriberOut]
    total: int


# The scopes the Zoom Server-to-Server OAuth app needs. Surfaced by the status
# endpoint so the Connectors page can show them verbatim — a missing scope is
# the most common setup failure, and Zoom's console offers no hint about which
# ones an integration actually requires.
ZOOM_SCOPES: list[dict[str, str]] = [
    {"scope": "webinar:read:list_webinars:admin", "classic": "webinar:read:admin",
     "why": "List the account's webinars for the picker"},
    {"scope": "webinar:read:webinar:admin", "classic": "webinar:read:admin",
     "why": "Webinar title, start time, duration and occurrences"},
    {"scope": "webinar:read:list_registrants:admin", "classic": "webinar:read:admin",
     "why": "Who registered, plus their company and job title"},
    {"scope": "webinar:read:list_past_instances:admin", "classic": "webinar:read:admin",
     "why": "Resolve which past session a recurring occurrence was"},
    {"scope": "webinar:read:list_absentees:admin", "classic": "webinar:read:admin",
     "why": "No-show cross-check — not called by the sync today, add it so it is there if needed"},
    {"scope": "report:read:list_webinar_participants:admin", "classic": "report:read:admin",
     "why": "Attendance and watch duration — the 10 and 30 minute metrics"},
    {"scope": "report:read:webinar:admin", "classic": "report:read:admin",
     "why": "Webinar-level totals — not called by the sync today, comes with report:read:admin anyway"},
    {"scope": "user:read:list_users:admin", "classic": "user:read:admin",
     "why": "Find every host on the account, not just the app owner — also what Test connection checks"},
]


class ZoomCheck(BaseModel):
    name: str
    endpoint: str
    ok: bool
    missing_scopes: list[str] = []
    error: Optional[str] = None


class ZoomCredentialStatus(BaseModel):
    configured: bool
    account_id: Optional[str] = None
    client_id: Optional[str] = None
    client_secret_masked: Optional[str] = None
    # Which Zoom account the credentials actually resolve to, so the page can
    # show more than "connected".
    account_email: Optional[str] = None
    scopes: list[dict[str, str]] = []

    # Credentials and scopes are reported separately: minting a token exercises
    # all three secrets at once, so a good mint proves that half of the setup
    # regardless of whether any scope is missing.
    credentials_ok: Optional[bool] = None
    credential_error: Optional[str] = None
    checks: list[ZoomCheck] = []
    missing_scopes: list[str] = []
    # What Zoom actually granted, read off the token response — lets the setup
    # page mark every scope done/missing without waiting for a call to fail.
    granted_scopes: list[str] = []
    tested: bool = False


class SetZoomCredentialRequest(BaseModel):
    account_id: str
    client_id: str
    client_secret: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


async def _get_api_key(db: AsyncSession, name: str = "default") -> str:
    """Look up a WebinarGeek API key by credential name.

    Defaults to the 'default' row, which preserves single-credential
    behavior. Variants pass their own name (resolved from
    Webinar.webinargeek_credential_id → ConnectorCredential.name).
    """
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == name,
        )
    )).scalar_one_or_none()
    if not row:
        if name == "default":
            raise HTTPException(status_code=400, detail="WebinarGeek API key not configured")
        # Fall back to default if a named credential is missing — the
        # operator might have renamed/removed the row referenced by a
        # Webinar. Better to sync against default than fail.
        return await _get_api_key(db, "default")
    return row.api_key


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@router.get("/webinargeek", response_model=CredentialStatus)
async def get_credential_status(db: AsyncSession = Depends(get_db)):
    """Status of the legacy single-credential ('default' name).

    Kept for back-compat with the old single-credential UI/API. New code
    should use /webinargeek/credentials.
    """
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()
    if not row:
        return CredentialStatus(configured=False)
    return CredentialStatus(configured=True, api_key_masked=_mask(row.api_key))


@router.put("/webinargeek", response_model=CredentialStatus)
async def set_credential(body: SetCredentialRequest, db: AsyncSession = Depends(get_db)):
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    try:
        ok = await wg.verify_api_key(api_key)
    except wg.WebinarGeekError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid WebinarGeek API key")

    stmt = pg_insert(ConnectorCredential).values(
        provider=PROVIDER,
        name="default",
        api_key=api_key,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "name"],
        set_={"api_key": api_key, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)
    return CredentialStatus(configured=True, api_key_masked=_mask(api_key))


@router.delete("/webinargeek")
async def delete_credential(db: AsyncSession = Depends(get_db)):
    """Delete the legacy 'default' WG credential."""
    await db.execute(
        delete(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == "default",
        )
    )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# WebinarGeek credentials — multi-account
# ---------------------------------------------------------------------------
class WgCredentialOut(BaseModel):
    id: str
    name: str
    api_key_masked: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class WgCredentialListResponse(BaseModel):
    credentials: list[WgCredentialOut]


class WgCredentialCreate(BaseModel):
    name: str
    api_key: str


class WgCredentialUpdate(BaseModel):
    name: Optional[str] = None
    api_key: Optional[str] = None


def _wg_cred_out(row: ConnectorCredential) -> WgCredentialOut:
    return WgCredentialOut(
        id=row.id,
        name=row.name,
        api_key_masked=_mask(row.api_key),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/webinargeek/credentials", response_model=WgCredentialListResponse)
async def list_wg_credentials(db: AsyncSession = Depends(get_db)):
    """List all WebinarGeek credentials (the 'default' row plus any named
    extras for variants). API keys are returned masked."""
    rows = (await db.execute(
        select(ConnectorCredential)
        .where(ConnectorCredential.provider == PROVIDER)
        .order_by(ConnectorCredential.name)
    )).scalars().all()
    return WgCredentialListResponse(credentials=[_wg_cred_out(r) for r in rows])


@router.post("/webinargeek/credentials", response_model=WgCredentialOut, status_code=201)
async def create_wg_credential(body: WgCredentialCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    api_key = body.api_key.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")

    try:
        ok = await wg.verify_api_key(api_key)
    except wg.WebinarGeekError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid WebinarGeek API key")

    existing = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == name,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Credential named '{name}' already exists")

    row = ConnectorCredential(provider=PROVIDER, name=name, api_key=api_key)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return _wg_cred_out(row)


@router.put("/webinargeek/credentials/{credential_id}", response_model=WgCredentialOut)
async def update_wg_credential(
    credential_id: str,
    body: WgCredentialUpdate,
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.id == credential_id,
            ConnectorCredential.provider == PROVIDER,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found")

    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if new_name != row.name:
            clash = (await db.execute(
                select(ConnectorCredential).where(
                    ConnectorCredential.provider == PROVIDER,
                    ConnectorCredential.name == new_name,
                )
            )).scalar_one_or_none()
            if clash:
                raise HTTPException(status_code=409, detail=f"Credential named '{new_name}' already exists")
            row.name = new_name

    if body.api_key is not None:
        new_key = body.api_key.strip()
        if not new_key:
            raise HTTPException(status_code=400, detail="api_key cannot be empty")
        try:
            ok = await wg.verify_api_key(new_key)
        except wg.WebinarGeekError as e:
            raise HTTPException(status_code=502, detail=str(e))
        if not ok:
            raise HTTPException(status_code=400, detail="Invalid WebinarGeek API key")
        row.api_key = new_key

    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(row)
    return _wg_cred_out(row)


@router.delete("/webinargeek/credentials/{credential_id}")
async def delete_wg_credential(credential_id: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.id == credential_id,
            ConnectorCredential.provider == PROVIDER,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Credential not found")
    if row.name == "default":
        raise HTTPException(
            status_code=400,
            detail="The 'default' credential cannot be deleted — use PUT to clear or replace it.",
        )
    # Webinar.webinargeek_credential_id has ON DELETE SET NULL, so dependent
    # variants will fall back to the default credential automatically.
    await db.delete(row)
    return {"deleted": True}


# ---------------------------------------------------------------------------
# OpenAI credentials (used by case-study URL importer)
# ---------------------------------------------------------------------------
@router.get("/openai", response_model=CredentialStatus)
async def get_openai_status(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(ConnectorCredential).where(ConnectorCredential.provider == OPENAI_PROVIDER)
    )).scalar_one_or_none()
    if not row:
        return CredentialStatus(configured=False)
    return CredentialStatus(configured=True, api_key_masked=_mask(row.api_key))


@router.put("/openai", response_model=CredentialStatus)
async def set_openai_credential(body: SetCredentialRequest, db: AsyncSession = Depends(get_db)):
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    try:
        ok = await oai.verify_api_key(api_key)
    except oai.OpenAIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid OpenAI API key")

    stmt = pg_insert(ConnectorCredential).values(provider=OPENAI_PROVIDER, api_key=api_key)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider"],
        set_={"api_key": api_key, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)
    return CredentialStatus(configured=True, api_key_masked=_mask(api_key))


@router.delete("/openai")
async def delete_openai_credential(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ConnectorCredential).where(ConnectorCredential.provider == OPENAI_PROVIDER))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Anthropic credentials (used by the Statistics chat assistant)
# ---------------------------------------------------------------------------
@router.get("/anthropic", response_model=CredentialStatus)
async def get_anthropic_status(db: AsyncSession = Depends(get_db)):
    """Status of the Anthropic credential. Single 'default' row — matches
    the OpenAI pattern. Used by the chat assistant on the Statistics page."""
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == ANTHROPIC_PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()
    if not row:
        return CredentialStatus(configured=False)
    return CredentialStatus(configured=True, api_key_masked=_mask(row.api_key))


@router.put("/anthropic", response_model=CredentialStatus)
async def set_anthropic_credential(body: SetCredentialRequest, db: AsyncSession = Depends(get_db)):
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    # Validate by making the cheapest possible Claude call. A 401/403 means
    # the key is bad; any other error is a transient issue and we let it
    # bubble so the user can retry rather than store a key we can't confirm.
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        await client.models.list(limit=1)
    except anthropic.AuthenticationError:
        raise HTTPException(status_code=400, detail="Invalid Anthropic API key")
    except anthropic.PermissionDeniedError:
        raise HTTPException(status_code=400, detail="Anthropic API key lacks permission")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to verify key: {exc}")

    stmt = pg_insert(ConnectorCredential).values(
        provider=ANTHROPIC_PROVIDER,
        name="default",
        api_key=api_key,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "name"],
        set_={"api_key": api_key, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)
    return CredentialStatus(configured=True, api_key_masked=_mask(api_key))


@router.delete("/anthropic")
async def delete_anthropic_credential(db: AsyncSession = Depends(get_db)):
    await db.execute(
        delete(ConnectorCredential).where(
            ConnectorCredential.provider == ANTHROPIC_PROVIDER,
            ConnectorCredential.name == "default",
        )
    )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Resend credentials (used by the weekly report emailer)
# ---------------------------------------------------------------------------
@router.get("/resend", response_model=CredentialStatus)
async def get_resend_status(db: AsyncSession = Depends(get_db)):
    """Status of the Resend credential. Single 'default' row — matches
    the Anthropic pattern. Used by the weekly webinar report sender."""
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == RESEND_PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()
    if not row:
        return CredentialStatus(configured=False)
    return CredentialStatus(configured=True, api_key_masked=_mask(row.api_key))


@router.put("/resend", response_model=CredentialStatus)
async def set_resend_credential(body: SetCredentialRequest, db: AsyncSession = Depends(get_db)):
    api_key = body.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    # Validate with the cheapest Resend call (list domains). 401/403 means the
    # key is bad; other errors are transient and bubble as 502 so the user can
    # retry rather than store a key we can't confirm.
    from integrations import resend_client
    try:
        valid = await resend_client.verify_key(api_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to verify key: {exc}")
    if not valid:
        raise HTTPException(status_code=400, detail="Invalid Resend API key")

    stmt = pg_insert(ConnectorCredential).values(
        provider=RESEND_PROVIDER,
        name="default",
        api_key=api_key,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "name"],
        set_={"api_key": api_key, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)
    return CredentialStatus(configured=True, api_key_masked=_mask(api_key))


@router.delete("/resend")
async def delete_resend_credential(db: AsyncSession = Depends(get_db)):
    await db.execute(
        delete(ConnectorCredential).where(
            ConnectorCredential.provider == RESEND_PROVIDER,
            ConnectorCredential.name == "default",
        )
    )
    return {"deleted": True}


# ---------------------------------------------------------------------------
# GoHighLevel credentials (used by the GHL sync engine + statistics)
# ---------------------------------------------------------------------------
@router.get("/ghl", response_model=GhlCredentialStatus)
async def get_ghl_status(db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(ConnectorCredential).where(ConnectorCredential.provider == GHL_PROVIDER)
    )).scalar_one_or_none()
    from config import settings as _settings
    if row and row.api_key and row.location_id:
        return GhlCredentialStatus(
            configured=True,
            api_key_masked=_mask(row.api_key),
            location_id=row.location_id,
            pipeline_id=row.pipeline_id or _settings.GHL_PIPELINE_ID,
            source="db",
        )
    # Env fallback — keeps the UI honest about where the key is coming from
    if _settings.GHL_API_KEY and _settings.GHL_LOCATION_ID:
        return GhlCredentialStatus(
            configured=True,
            api_key_masked=_mask(_settings.GHL_API_KEY),
            location_id=_settings.GHL_LOCATION_ID,
            pipeline_id=_settings.GHL_PIPELINE_ID,
            source="env",
        )
    return GhlCredentialStatus(configured=False, source="none")


@router.put("/ghl", response_model=GhlCredentialStatus)
async def set_ghl_credential(body: SetGhlCredentialRequest, db: AsyncSession = Depends(get_db)):
    api_key = body.api_key.strip()
    location_id = body.location_id.strip()
    pipeline_id = body.pipeline_id.strip() if body.pipeline_id else None
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    if not location_id:
        raise HTTPException(status_code=400, detail="location_id is required")

    ok, err = await ghl.verify_credentials(api_key, location_id)
    if not ok:
        # 400 for bad creds, 502 for upstream/network issues. We don't have
        # a clean way to tell them apart from verify_credentials' return,
        # so use 400 for any verified failure — caller shows the message.
        raise HTTPException(status_code=400, detail=err or "Failed to verify GHL credentials")

    stmt = pg_insert(ConnectorCredential).values(
        provider=GHL_PROVIDER,
        api_key=api_key,
        location_id=location_id,
        pipeline_id=pipeline_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider"],
        set_={
            "api_key": api_key,
            "location_id": location_id,
            "pipeline_id": pipeline_id,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await db.execute(stmt)
    return GhlCredentialStatus(
        configured=True,
        api_key_masked=_mask(api_key),
        location_id=location_id,
        pipeline_id=pipeline_id,
        source="db",
    )


@router.delete("/ghl")
async def delete_ghl_credential(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ConnectorCredential).where(ConnectorCredential.provider == GHL_PROVIDER))
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Broadcasts
# ---------------------------------------------------------------------------
@router.get("/webinargeek/webinars", response_model=BroadcastListResponse)
async def list_broadcasts(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: Optional[str] = None,
    credential_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    # webinar_broadcasts is shared with Zoom — scope every WebinarGeek-facing
    # read to this provider or the picker starts offering Zoom broadcasts.
    base = select(WebinarBroadcast).where(WebinarBroadcast.provider == PROVIDER)
    count_base = (
        select(func.count()).select_from(WebinarBroadcast)
        .where(WebinarBroadcast.provider == PROVIDER)
    )
    if q:
        like = f"%{q}%"
        base = base.where(or_(
            WebinarBroadcast.name.ilike(like),
            WebinarBroadcast.internal_title.ilike(like),
            WebinarBroadcast.broadcast_id.ilike(like),
        ))
        count_base = count_base.where(or_(
            WebinarBroadcast.name.ilike(like),
            WebinarBroadcast.internal_title.ilike(like),
            WebinarBroadcast.broadcast_id.ilike(like),
        ))
    if credential_id:
        base = base.where(WebinarBroadcast.credential_id == credential_id)
        count_base = count_base.where(WebinarBroadcast.credential_id == credential_id)

    total = (await db.execute(count_base)).scalar_one()

    rows = (await db.execute(
        base.order_by(WebinarBroadcast.starts_at.desc().nullslast())
            .limit(limit).offset(offset)
    )).scalars().all()

    synced_counts = dict((await db.execute(
        select(WebinarRegistrant.broadcast_id, func.count())
        .where(WebinarRegistrant.provider == PROVIDER)
        .group_by(WebinarRegistrant.broadcast_id)
    )).all())

    cred_names = dict((await db.execute(
        select(ConnectorCredential.id, ConnectorCredential.name)
        .where(ConnectorCredential.provider == PROVIDER)
    )).all())

    return BroadcastListResponse(
        broadcasts=[
            BroadcastOut(
                broadcast_id=r.broadcast_id,
                name=r.name,
                internal_title=r.internal_title,
                starts_at=r.starts_at,
                duration_seconds=r.duration_seconds,
                subscriptions_count=r.subscriptions_count,
                live_viewers_count=r.live_viewers_count,
                replay_viewers_count=r.replay_viewers_count,
                has_ended=r.has_ended,
                cancelled=r.cancelled,
                last_synced_at=r.last_synced_at,
                synced_subscriber_count=synced_counts.get(r.broadcast_id, 0),
                credential_id=r.credential_id,
                credential_name=cred_names.get(r.credential_id) if r.credential_id else None,
            )
            for r in rows
        ],
        total=total,
    )


@router.post("/webinargeek/webinars/refresh", response_model=RefreshResponse)
async def refresh_broadcasts(db: AsyncSession = Depends(get_db)):
    """
    Refresh strategy (multi-credential):
      For each WebinarGeek credential row,
        1) GET /broadcasts (nested_resources=episode,webinar) → flat list
           with all stats AND the parent webinar embedded per broadcast.
        2) GET /webinars → {broadcast_id → webinar meta} map, kept only as
           a fallback for broadcasts missing the embedded webinar.
        3) Enrich each broadcast with webinar meta (embedded first), upsert.
    Broadcasts are keyed by id; if two accounts somehow surface the same
    id (rare), the last-written value wins. Errors from one credential
    don't block the others. On conflict, a known internal_title / webinar_id
    / name is never overwritten by a NULL or "Broadcast {id}" placeholder.
    """
    creds = (await db.execute(
        select(ConnectorCredential)
        .where(ConnectorCredential.provider == PROVIDER)
        .order_by(ConnectorCredential.name)
    )).scalars().all()
    if not creds:
        raise HTTPException(status_code=400, detail="No WebinarGeek credentials configured")

    # Tuple per broadcast so we know which credential surfaced it.
    cred_broadcasts: list[tuple[str, dict]] = []
    all_webinars: list = []
    cred_errors: list[str] = []
    for cred in creds:
        try:
            webinars = await wg.list_webinars(cred.api_key)
            broadcasts = await wg.list_broadcasts(cred.api_key)
            all_webinars.extend(webinars)
            cred_broadcasts.extend((cred.id, b) for b in broadcasts)
        except wg.WebinarGeekError as e:
            cred_errors.append(f"{cred.name}: {e}")
            logger.warning("refresh: WG credential %s failed: %s", cred.name, e)

    if not cred_broadcasts and cred_errors:
        # All credentials failed — surface the error so the UI doesn't show
        # an empty success.
        raise HTTPException(status_code=502, detail="; ".join(cred_errors))

    meta = wg.build_broadcast_meta(all_webinars)
    unknown_meta = {"webinar_id": None, "webinar_title": "", "internal_title": ""}

    total = 0
    for cred_id, b in cred_broadcasts:
        broadcast_id = str(b.get("id") or "")
        if not broadcast_id:
            continue
        # Prefer the webinar embedded on the broadcast (nested_resources);
        # fall back to the /webinars map for any broadcast missing it.
        m = wg.webinar_meta_from_broadcast(b) or meta.get(broadcast_id, unknown_meta)
        placeholder_name = f"Broadcast {broadcast_id}"
        values = {
            "broadcast_id": broadcast_id,
            "provider": PROVIDER,
            "credential_id": cred_id,
            "webinar_id": str(m["webinar_id"]) if m["webinar_id"] is not None else None,
            "name": m["webinar_title"] or placeholder_name,
            "internal_title": m["internal_title"] or None,
            "starts_at": wg.unix_to_dt(b.get("date")),
            "duration_seconds": b.get("duration"),
            "subscriptions_count": b.get("subscriptions_count") or 0,
            "live_viewers_count": b.get("live_viewers_count") or 0,
            "replay_viewers_count": b.get("replay_viewers_count") or 0,
            "has_ended": bool(b.get("has_ended")),
            "cancelled": bool(b.get("cancelled")),
            "raw": b,
            "updated_at": datetime.now(timezone.utc),
        }
        stmt = pg_insert(WebinarBroadcast).values(**values)
        set_cols = {k: v for k, v in values.items() if k != "broadcast_id"}
        # A later refresh that loses the webinar link must not wipe values we
        # already captured: keep the prior internal_title / webinar_id, and the
        # prior name unless we now have a real (non-placeholder) title.
        set_cols["internal_title"] = func.coalesce(
            stmt.excluded.internal_title, WebinarBroadcast.internal_title
        )
        set_cols["webinar_id"] = func.coalesce(
            stmt.excluded.webinar_id, WebinarBroadcast.webinar_id
        )
        set_cols["name"] = func.coalesce(
            func.nullif(stmt.excluded.name, placeholder_name), WebinarBroadcast.name
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["broadcast_id"],
            set_=set_cols,
        )
        await db.execute(stmt)
        total += 1

    return RefreshResponse(count=total)


@router.post("/webinargeek/webinars/{broadcast_id}/sync", response_model=SyncResponse, status_code=202)
async def sync_broadcast_subscribers(broadcast_id: str, db: AsyncSession = Depends(get_db)):
    """Queue a background subscriber sync for one broadcast.

    Returns immediately with the sync_run id so the UI can route the user
    to the Sync page. The actual fetch + upsert keeps running even if
    the user navigates away.
    """
    wb = (await db.execute(
        select(WebinarBroadcast).where(
            WebinarBroadcast.broadcast_id == broadcast_id,
            # Refuse a Zoom id here: it would be posted to the WebinarGeek API
            # with a WebinarGeek key and fail confusingly.
            WebinarBroadcast.provider == PROVIDER,
        )
    )).scalar_one_or_none()
    if not wb:
        raise HTTPException(status_code=404, detail="Broadcast not cached — refresh first")

    task = asyncio.create_task(wg_sync.run_broadcast_sync(broadcast_id, trigger="manual"))

    # Brief wait so the sync_run row exists by the time we read it back.
    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GHLSyncRun)
            .where(GHLSyncRun.sync_type == f"wg:{broadcast_id}")
            .order_by(GHLSyncRun.started_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()

    if run is None:
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start broadcast sync")

    return SyncResponse(broadcast_id=broadcast_id, run_id=run.id, status=run.status)


@router.post("/webinargeek/webinars/sync-all", response_model=SyncAllResponse, status_code=202)
async def sync_all_broadcasts(db: AsyncSession = Depends(get_db)):
    """Queue a background sync-all. One umbrella sync_run row tracks
    overall progress; each per-broadcast sync also gets its own row.
    """
    count = (await db.execute(
        select(func.count()).select_from(WebinarBroadcast)
        .where(WebinarBroadcast.provider == PROVIDER)
    )).scalar_one()

    task = asyncio.create_task(wg_sync.run_sync_all(trigger="manual"))

    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(GHLSyncRun)
            .where(GHLSyncRun.sync_type == "wg:all")
            .order_by(GHLSyncRun.started_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()

    if run is None:
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start sync-all")

    return SyncAllResponse(run_id=run.id, status=run.status, broadcasts_queued=count)


# ---------------------------------------------------------------------------
# Subscribers
# ---------------------------------------------------------------------------
def _subscriber_query(broadcast_id: Optional[str], q: Optional[str]):
    # webinar_registrants is shared with Zoom — scope to this provider or the
    # WebinarGeek subscribers tab and its CSV export start listing Zoom people.
    stmt = select(WebinarRegistrant).where(WebinarRegistrant.provider == PROVIDER)
    count_stmt = (
        select(func.count()).select_from(WebinarRegistrant)
        .where(WebinarRegistrant.provider == PROVIDER)
    )
    if broadcast_id:
        stmt = stmt.where(WebinarRegistrant.broadcast_id == broadcast_id)
        count_stmt = count_stmt.where(WebinarRegistrant.broadcast_id == broadcast_id)
    if q:
        like = f"%{q}%"
        cond = or_(
            WebinarRegistrant.email.ilike(like),
            WebinarRegistrant.first_name.ilike(like),
            WebinarRegistrant.last_name.ilike(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    return stmt, count_stmt


@router.get("/webinargeek/subscribers", response_model=SubscriberListResponse)
async def list_subscribers(
    broadcast_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt, count_stmt = _subscriber_query(broadcast_id, q)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(
        stmt.order_by(WebinarRegistrant.subscribed_at.desc().nullslast())
            .limit(limit).offset(offset)
    )).scalars().all()
    return SubscriberListResponse(
        subscribers=[
            SubscriberOut(
                id=r.id,
                broadcast_id=r.broadcast_id,
                email=r.email,
                first_name=r.first_name,
                last_name=r.last_name,
                registration_source=r.registration_source,
                subscribed_at=r.subscribed_at,
                watched_live=r.watched_live,
                watched_replay=r.watched_replay,
                minutes_viewing=r.minutes_viewing,
                viewing_device=r.viewing_device,
                viewing_country=r.viewing_country,
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/webinargeek/subscribers/export")
async def export_subscribers(
    broadcast_id: Optional[str] = None,
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt, _ = _subscriber_query(broadcast_id, q)
    rows = (await db.execute(
        stmt.order_by(WebinarRegistrant.subscribed_at.desc().nullslast())
    )).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "email", "first_name", "last_name", "broadcast_id",
        "registered_at", "source", "watched_live", "watched_replay",
        "minutes_viewing", "device", "country", "timezone", "company", "job_title",
    ])
    for r in rows:
        w.writerow([
            r.email, r.first_name or "", r.last_name or "", r.broadcast_id,
            r.subscribed_at.isoformat() if r.subscribed_at else "",
            r.registration_source or "",
            "" if r.watched_live is None else ("yes" if r.watched_live else "no"),
            "" if r.watched_replay is None else ("yes" if r.watched_replay else "no"),
            r.minutes_viewing if r.minutes_viewing is not None else "",
            r.viewing_device or "", r.viewing_country or "",
            r.timezone or "", r.company or "", r.job_title or "",
        ])

    buf.seek(0)
    fn = f"webinar_registrants_{broadcast_id or 'all'}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------
# Server-to-Server OAuth, one account (provider='zoom', name='default'). The
# client secret lives in api_key so masking and deletion stay provider-agnostic;
# account_id / client_id are the non-secret half and have their own columns.
def _zoom_status(row: Optional[ConnectorCredential], account_email: Optional[str] = None) -> ZoomCredentialStatus:
    if not row or not row.api_key or not row.account_id or not row.client_id:
        return ZoomCredentialStatus(configured=False, scopes=ZOOM_SCOPES)
    return ZoomCredentialStatus(
        configured=True,
        account_id=row.account_id,
        client_id=row.client_id,
        client_secret_masked=_mask(row.api_key),
        account_email=account_email,
        scopes=ZOOM_SCOPES,
    )


async def _zoom_credential(db: AsyncSession) -> Optional[ConnectorCredential]:
    return (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == ZOOM_PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()


@router.get("/zoom", response_model=ZoomCredentialStatus)
async def get_zoom_status(db: AsyncSession = Depends(get_db)):
    """Current Zoom connection plus the scope list the setup page renders.

    Deliberately does NOT call Zoom: this is polled by the page and a live
    round-trip per poll would burn rate limit for nothing.
    """
    return _zoom_status(await _zoom_credential(db))


def _zoom_status_from_check(row: Optional[ConnectorCredential], check: dict) -> ZoomCredentialStatus:
    st = _zoom_status(row, account_email=check.get("account_email"))
    st.tested = True
    st.credentials_ok = check.get("credentials_ok")
    st.credential_error = check.get("credential_error")
    st.missing_scopes = check.get("missing_scopes") or []
    st.granted_scopes = check.get("granted_scopes") or []
    st.checks = [ZoomCheck(**c) for c in (check.get("checks") or [])]
    return st


@router.put("/zoom", response_model=ZoomCredentialStatus)
async def set_zoom_credential(body: SetZoomCredentialRequest, db: AsyncSession = Depends(get_db)):
    """Verify the credentials against Zoom, then store them.

    A valid-but-under-scoped app still gets SAVED. Minting a token proves all
    three secrets are right, and refusing to store them would force the user to
    re-paste the secret after every scope fix in the Zoom console. The response
    reports the scope gap instead, and Test connection re-checks without
    re-typing anything.
    """
    account_id = body.account_id.strip()
    client_id = body.client_id.strip()
    client_secret = body.client_secret.strip()
    if not account_id or not client_id or not client_secret:
        raise HTTPException(
            status_code=400,
            detail="Account ID, Client ID and Client Secret are all required",
        )

    check = await zc.check_connection(account_id, client_id, client_secret)
    if not check["credentials_ok"]:
        # The token did not mint: one of the three values is wrong, or the app
        # was never activated. Nothing worth storing.
        raise HTTPException(
            status_code=400,
            detail=check.get("credential_error")
            or "Zoom rejected the credentials. Check the three values, and that the app is Activated.",
        )

    stmt = pg_insert(ConnectorCredential).values(
        provider=ZOOM_PROVIDER,
        name="default",
        api_key=client_secret,
        account_id=account_id,
        client_id=client_id,
    ).on_conflict_do_update(
        index_elements=["provider", "name"],
        set_={
            "api_key": client_secret,
            "account_id": account_id,
            "client_id": client_id,
            "updated_at": datetime.now(timezone.utc),
        },
    )
    await db.execute(stmt)
    await db.commit()

    row = await _zoom_credential(db)
    return _zoom_status_from_check(row, check)


@router.post("/zoom/test", response_model=ZoomCredentialStatus)
async def test_zoom_connection(db: AsyncSession = Depends(get_db)):
    """Re-check the stored credentials against Zoom without re-entering them.

    The point of this existing separately from PUT: fixing a scope happens in
    the Zoom console, not here, so the user needs a way to re-verify that does
    not make them paste the client secret again (which the UI cannot show them
    back, since only a masked form is ever returned).
    """
    row = await _zoom_credential(db)
    if not row or not row.api_key or not row.account_id or not row.client_id:
        raise HTTPException(status_code=400, detail="Zoom is not connected yet")

    # Drop any cached bearer so a scope added seconds ago in the Zoom console is
    # actually picked up — a cached token carries the OLD scope set for up to an
    # hour, which would make a correct fix look like it had not worked.
    zc.invalidate_token(row.account_id, row.client_id)

    check = await zc.check_connection(row.account_id, row.client_id, row.api_key)
    return _zoom_status_from_check(row, check)


@router.delete("/zoom")
async def delete_zoom_credential(db: AsyncSession = Depends(get_db)):
    row = await _zoom_credential(db)
    if row:
        # Drop any cached bearer token too, so a re-add cannot resurrect access
        # through a token minted under the old secret.
        if row.account_id and row.client_id:
            zc.invalidate_token(row.account_id, row.client_id)
        await db.execute(delete(ConnectorCredential).where(ConnectorCredential.id == row.id))
        await db.commit()
    return {"deleted": bool(row)}


@router.get("/zoom/webinars", response_model=BroadcastListResponse)
async def list_zoom_webinars(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    base = select(WebinarBroadcast).where(WebinarBroadcast.provider == ZOOM_PROVIDER)
    count_base = (
        select(func.count()).select_from(WebinarBroadcast)
        .where(WebinarBroadcast.provider == ZOOM_PROVIDER)
    )
    if q:
        like = f"%{q}%"
        cond = or_(
            WebinarBroadcast.name.ilike(like),
            WebinarBroadcast.internal_title.ilike(like),
            WebinarBroadcast.broadcast_id.ilike(like),
        )
        base = base.where(cond)
        count_base = count_base.where(cond)

    total = (await db.execute(count_base)).scalar_one()
    rows = (await db.execute(
        base.order_by(WebinarBroadcast.starts_at.desc().nullslast()).limit(limit).offset(offset)
    )).scalars().all()

    synced_counts = dict((await db.execute(
        select(WebinarRegistrant.broadcast_id, func.count())
        .where(WebinarRegistrant.provider == ZOOM_PROVIDER)
        .group_by(WebinarRegistrant.broadcast_id)
    )).all())

    return BroadcastListResponse(
        broadcasts=[
            BroadcastOut(
                broadcast_id=r.broadcast_id,
                name=r.name,
                internal_title=r.internal_title,
                starts_at=r.starts_at,
                duration_seconds=r.duration_seconds,
                subscriptions_count=r.subscriptions_count,
                live_viewers_count=r.live_viewers_count,
                # Zoom exposes no recording-view analytics, so replay is never
                # counted rather than reported as a misleading zero.
                replay_viewers_count=0,
                has_ended=r.has_ended,
                cancelled=r.cancelled,
                last_synced_at=r.last_synced_at,
                synced_subscriber_count=synced_counts.get(r.broadcast_id, 0),
                credential_id=r.credential_id,
                credential_name="Zoom" if r.credential_id else None,
            )
            for r in rows
        ],
        total=total,
    )


@router.post("/zoom/webinars/refresh", response_model=RefreshResponse)
async def refresh_zoom_webinars(db: AsyncSession = Depends(get_db)):
    """Pull the account's webinars into the broadcast cache.

    Walks every active host (Server-to-Server OAuth has no "me" in the user
    sense) and expands recurring webinars into one row per occurrence.
    """
    row = await _zoom_credential(db)
    if not row:
        raise HTTPException(status_code=400, detail="Zoom is not connected")
    try:
        count = await zoom_sync.refresh_webinars()
    except zc.ZoomScopeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except zc.ZoomError as e:
        raise HTTPException(status_code=502, detail=f"Zoom API error: {e}") from e
    return RefreshResponse(count=count)


@router.post("/zoom/webinars/{broadcast_id}/sync", response_model=SyncResponse, status_code=202)
async def sync_zoom_webinar(broadcast_id: str, db: AsyncSession = Depends(get_db)):
    """Queue a background registrant + attendance sync for one Zoom webinar.

    The id is `zoom:<webinar_id>[:<occurrence_id>]` — colons are legal inside a
    path segment, so the default matcher is enough once the caller encodes it.
    """
    bc = (await db.execute(
        select(WebinarBroadcast).where(
            WebinarBroadcast.broadcast_id == broadcast_id,
            WebinarBroadcast.provider == ZOOM_PROVIDER,
        )
    )).scalar_one_or_none()
    if not bc:
        raise HTTPException(status_code=404, detail="Zoom webinar not cached — refresh first")

    task = asyncio.create_task(zoom_sync.run_broadcast_sync(broadcast_id, trigger="manual"))
    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    async with AsyncSessionLocal() as session:
        run = (await session.execute(
            select(GHLSyncRun)
            .where(GHLSyncRun.sync_type == zoom_sync._sync_type(broadcast_id))
            .order_by(GHLSyncRun.started_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    if run is None:
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start Zoom sync")

    return SyncResponse(broadcast_id=broadcast_id, run_id=run.id, status=run.status)


@router.post("/zoom/webinars/sync-all", response_model=SyncAllResponse, status_code=202)
async def sync_all_zoom_webinars(db: AsyncSession = Depends(get_db)):
    """Queue a sync of every cached Zoom webinar."""
    count = (await db.execute(
        select(func.count()).select_from(WebinarBroadcast)
        .where(WebinarBroadcast.provider == ZOOM_PROVIDER)
    )).scalar_one()

    task = asyncio.create_task(zoom_sync.run_sync_all(trigger="manual"))
    try:
        await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise

    async with AsyncSessionLocal() as session:
        run = (await session.execute(
            select(GHLSyncRun)
            .where(GHLSyncRun.sync_type == "zoom:all")
            .order_by(GHLSyncRun.started_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    if run is None:
        if task.done() and task.exception():
            raise HTTPException(status_code=409, detail=str(task.exception()))
        raise HTTPException(status_code=500, detail="Failed to start Zoom sync-all")

    return SyncAllResponse(run_id=run.id, status=run.status, broadcasts_queued=count)


@router.get("/zoom/registrants", response_model=SubscriberListResponse)
async def list_zoom_registrants(
    broadcast_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(WebinarRegistrant).where(WebinarRegistrant.provider == ZOOM_PROVIDER)
    count_stmt = (
        select(func.count()).select_from(WebinarRegistrant)
        .where(WebinarRegistrant.provider == ZOOM_PROVIDER)
    )
    if broadcast_id:
        stmt = stmt.where(WebinarRegistrant.broadcast_id == broadcast_id)
        count_stmt = count_stmt.where(WebinarRegistrant.broadcast_id == broadcast_id)
    if q:
        like = f"%{q}%"
        cond = or_(
            WebinarRegistrant.email.ilike(like),
            WebinarRegistrant.first_name.ilike(like),
            WebinarRegistrant.last_name.ilike(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (await db.execute(
        stmt.order_by(WebinarRegistrant.subscribed_at.desc().nullslast()).limit(limit).offset(offset)
    )).scalars().all()

    return SubscriberListResponse(
        subscribers=[
            SubscriberOut(
                id=r.id,
                broadcast_id=r.broadcast_id,
                email=r.email,
                first_name=r.first_name,
                last_name=r.last_name,
                registration_source=r.registration_source,
                subscribed_at=r.subscribed_at,
                watched_live=r.watched_live,
                watched_replay=r.watched_replay,
                minutes_viewing=r.minutes_viewing,
                viewing_device=r.viewing_device,
                viewing_country=r.viewing_country,
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/zoom/registrants/export")
async def export_zoom_registrants(
    broadcast_id: Optional[str] = None,
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """CSV of synced Zoom registrants — the counterpart to the WebinarGeek export.

    `watched_replay` is omitted rather than emitted as an empty column: Zoom has
    no recording-view analytics, so the value is always unknown and a blank
    column would read as "nobody watched the replay".
    """
    stmt = select(WebinarRegistrant).where(WebinarRegistrant.provider == ZOOM_PROVIDER)
    if broadcast_id:
        stmt = stmt.where(WebinarRegistrant.broadcast_id == broadcast_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            WebinarRegistrant.email.ilike(like),
            WebinarRegistrant.first_name.ilike(like),
            WebinarRegistrant.last_name.ilike(like),
        ))

    rows = (await db.execute(
        stmt.order_by(WebinarRegistrant.subscribed_at.desc().nullslast())
    )).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "email", "first_name", "last_name", "broadcast_id",
        "registered_at", "source", "attended", "minutes_viewing",
        "joined_at", "left_at", "device", "country", "company", "job_title",
    ])
    for r in rows:
        w.writerow([
            r.email, r.first_name or "", r.last_name or "", r.broadcast_id,
            r.subscribed_at.isoformat() if r.subscribed_at else "",
            r.registration_source or "",
            "" if r.watched_live is None else ("yes" if r.watched_live else "no"),
            r.minutes_viewing if r.minutes_viewing is not None else "",
            r.start_time.isoformat() if r.start_time else "",
            r.end_time.isoformat() if r.end_time else "",
            r.viewing_device or "", r.country or "",
            r.company or "", r.job_title or "",
        ])

    buf.seek(0)
    fn = f"zoom_registrants_{broadcast_id or 'all'}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )
