"""Outreach sub-router: "Create Draft Campaigns in Skarpe" bulk action.

One Skarpe draft campaign per selected assigned list, in one chosen Skarpe
workspace (connector credential). Two-step flow:

  POST /outreach/skarpe/prepare    → everything the modal needs in one round
                                     trip (workspace info, sending accounts,
                                     contact policy, per-list prefills)
  POST /outreach/skarpe/campaigns  → starts the background job, returns job_id
  GET  /outreach/skarpe/jobs/{id}  → poll

The job registry is the in-memory mark-used pattern (webinars.py): progress
is ephemeral, the skarpe_campaigns rows are the durable state. A restart
mid-batch loses nothing — Skarpe's external_ref (we send the assignment id)
makes draft creation idempotent and re-pushed contacts are skipped silently,
so re-running the action converges.

Contact pushing requires the user to have confirmed Skarpe's contact policy
in the modal. The policy statements come live from get_contact_policy in
/prepare; /campaigns refuses a push without confirmed=true + confirmed_by.
That confirmation is the user's legal declaration — it is never defaulted.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import httpx

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import (
    Contact, ConnectorCredential, SkarpeCampaign, Webinar, WebinarBroadcast,
    WebinarContactMembership, WebinarListAssignment,
)
from db.session import AsyncSessionLocal, get_db
from integrations import skarpe_client as skarpe

logger = logging.getLogger(__name__)

router = APIRouter()

SKARPE_PROVIDER = "skarpe"

# job_id → progress dict. In-memory on purpose (see module docstring).
_SKARPE_JOBS: dict[str, dict] = {}
_active_skarpe_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SkarpePrepareRequest(BaseModel):
    credential_id: str
    assignment_ids: list[str]


class SkarpeCampaignItem(BaseModel):
    assignment_id: str
    title: str
    event_title: Optional[str] = None
    event_description: Optional[str] = None
    event_location: Optional[str] = None
    # Naive wall-clock "YYYY-MM-DDTHH:MM" in the workspace timezone;
    # event_timezone is deliberately never sent so the workspace zone applies.
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    webinar_number: Optional[int] = None


class SkarpePolicyConfirmation(BaseModel):
    policy_version: str
    policy_hash: str
    confirmed: bool = False
    confirmed_by: Optional[str] = None


class SkarpeCampaignsRequest(BaseModel):
    credential_id: str
    items: list[SkarpeCampaignItem]
    account_ids: list[str] = []
    daily_limit: Optional[int] = None
    push_contacts: bool = False
    policy: Optional[SkarpePolicyConfirmation] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _load_credential(db: AsyncSession, credential_id: str) -> ConnectorCredential:
    cred = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.id == credential_id,
            ConnectorCredential.provider == SKARPE_PROVIDER,
        )
    )).scalar_one_or_none()
    if not cred or not cred.base_url:
        raise HTTPException(404, "Skarpe credential not found")
    return cred


async def _load_assignments(
    db: AsyncSession, assignment_ids: list[str]
) -> list[WebinarListAssignment]:
    rows = (await db.execute(
        select(WebinarListAssignment)
        .where(
            WebinarListAssignment.id.in_(assignment_ids),
            WebinarListAssignment.user_id == LLOYD_USER_ID,
        )
        .options(
            selectinload(WebinarListAssignment.webinar),
            selectinload(WebinarListAssignment.title_copy),
            selectinload(WebinarListAssignment.desc_copy),
        )
    )).scalars().all()
    by_id = {r.id: r for r in rows}
    missing = [aid for aid in assignment_ids if aid not in by_id]
    if missing:
        raise HTTPException(404, f"Assignments not found: {', '.join(missing[:3])}")
    # Preserve the caller's (selection) order.
    return [by_id[aid] for aid in assignment_ids]


def _match_skarpe_webinar(
    broadcast: Optional[WebinarBroadcast],
    skarpe_webinars: list[dict],
) -> Optional[dict]:
    """Match our platform identity against Skarpe's.

    Zoom: our webinar_broadcasts.webinar_id is the numeric Zoom webinar id →
    Skarpe platform.zoom_webinar_id. WebinarGeek: a Skarpe campaign targets a
    broadcast, so our broadcast_id (the PK) → platform.webinargeek_broadcast_id.
    String compare on both sides.
    """
    if not broadcast:
        return None
    for sw in skarpe_webinars:
        platform = sw.get("platform") or {}
        if broadcast.provider == "zoom":
            ours = broadcast.webinar_id
            theirs = platform.get("zoom_webinar_id")
        else:
            ours = broadcast.broadcast_id
            theirs = platform.get("webinargeek_broadcast_id")
        if ours and theirs and str(ours) == str(theirs):
            return sw
    return None


def _wall_clock(dt: datetime, tz_name: Optional[str]) -> str:
    """UTC instant → naive wall-clock string in the workspace timezone."""
    if tz_name:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except (KeyError, ValueError):
            pass
    return dt.strftime("%Y-%m-%dT%H:%M")


def _shift_hour(start: str, hours: int = 1) -> Optional[str]:
    """start ("YYYY-MM-DDTHH:MM") + N hours, still naive wall-clock."""
    try:
        from datetime import timedelta
        return (datetime.fromisoformat(start) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


async def _assigned_contact_counts(
    db: AsyncSession, assignment_ids: list[str]
) -> dict[str, int]:
    """Pushable contacts per assignment: status='assigned', not blocklisted —
    the same predicates as get_assignment_contacts."""
    m = WebinarContactMembership
    rows = (await db.execute(
        select(m.assignment_id, sa_func.count())
        .join(Contact, Contact.id == m.contact_id)
        .where(
            m.assignment_id.in_(assignment_ids),
            m.status == "assigned",
            Contact.user_id == LLOYD_USER_ID,
            ~Contact.is_blocklisted,
        )
        .group_by(m.assignment_id)
    )).all()
    return {str(aid): n for aid, n in rows}


# ---------------------------------------------------------------------------
# Prepare — one round trip for the modal
# ---------------------------------------------------------------------------
@router.post("/skarpe/prepare")
async def prepare_skarpe_campaigns(
    body: SkarpePrepareRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if not body.assignment_ids:
        raise HTTPException(400, "assignment_ids is required")
    cred = await _load_credential(db, body.credential_id)
    assignments = await _load_assignments(db, body.assignment_ids)

    try:
        who, accounts, skarpe_webinars, policy = await asyncio.gather(
            skarpe.whoami(cred.base_url, cred.api_key),
            skarpe.list_sending_accounts(cred.base_url, cred.api_key),
            skarpe.list_webinars(cred.base_url, cred.api_key),
            skarpe.get_contact_policy(cred.base_url, cred.api_key),
        )
    except skarpe.SkarpeAuthError:
        raise HTTPException(400, "Invalid Skarpe API key — update it in Connectors")
    except skarpe.SkarpeError as e:
        raise HTTPException(502, f"Skarpe unreachable: {e}")

    tz_name = who.get("timezone")

    # Platform identity for each webinar's broadcast, one query.
    broadcast_ids = {a.webinar.broadcast_id for a in assignments if a.webinar and a.webinar.broadcast_id}
    broadcasts: dict[str, WebinarBroadcast] = {}
    if broadcast_ids:
        for b in (await db.execute(
            select(WebinarBroadcast).where(WebinarBroadcast.broadcast_id.in_(broadcast_ids))
        )).scalars().all():
            broadcasts[b.broadcast_id] = b

    counts = await _assigned_contact_counts(db, body.assignment_ids)

    existing_rows = (await db.execute(
        select(SkarpeCampaign).where(
            SkarpeCampaign.credential_id == cred.id,
            SkarpeCampaign.assignment_id.in_(body.assignment_ids),
        )
    )).scalars().all()
    existing = {r.assignment_id: r for r in existing_rows}

    items = []
    for a in assignments:
        webinar: Optional[Webinar] = a.webinar
        broadcast = broadcasts.get(webinar.broadcast_id) if webinar and webinar.broadcast_id else None
        matched = _match_skarpe_webinar(broadcast, skarpe_webinars)

        warnings: list[str] = []
        if broadcast and broadcast.starts_at:
            event_start = _wall_clock(broadcast.starts_at, tz_name)
        elif webinar and webinar.date:
            # Date-only fallback: the user picks the real time in the modal.
            event_start = f"{webinar.date.isoformat()}T12:00"
            warnings.append("No broadcast start time — date from the webinar, time defaulted to 12:00")
        else:
            event_start = None
            warnings.append("No webinar date — set the event start manually")
        if not (a.title_copy and a.title_copy.text):
            warnings.append("No title copy picked — event title will be empty")
        if not (a.desc_copy and a.desc_copy.text):
            warnings.append("No description copy picked — event description will be empty")
        if not (webinar and webinar.registration_link):
            warnings.append("No registration link on the webinar — event location will be empty")
        if not matched:
            warnings.append("No matching Skarpe webinar — campaign will not be grouped")

        ex = existing.get(a.id)
        items.append({
            "assignment_id": a.id,
            "list_name": a.list_name or a.description or "",
            "event_title": a.title_copy.text if a.title_copy else None,
            "event_description": a.desc_copy.text if a.desc_copy else None,
            "event_location": webinar.registration_link if webinar else None,
            "event_start": event_start,
            "event_end": _shift_hour(event_start) if event_start else None,
            "matched_webinar": (
                {"webinar_number": matched.get("webinar_number"), "title": matched.get("title")}
                if matched else None
            ),
            "assigned_contacts": counts.get(a.id, 0),
            "existing_campaign": (
                {
                    "skarpe_campaign_id": ex.skarpe_campaign_id,
                    "status": ex.status,
                    "contacts_pushed": ex.contacts_pushed,
                    "app_url": ex.app_url,
                }
                if ex else None
            ),
            "warnings": warnings,
        })

    return {
        "workspace": {
            "key_name": who.get("key_name"),
            "permissions": who.get("permissions") or [],
            "can_launch_campaigns": bool(who.get("can_launch_campaigns")),
            "timezone": tz_name,
        },
        "sending_accounts": accounts,
        "policy": policy,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Create campaigns — background job
# ---------------------------------------------------------------------------
async def _upsert_campaign_row(
    db: AsyncSession,
    *,
    credential_id: str,
    assignment_id: str,
    values: dict,
) -> None:
    stmt = pg_insert(SkarpeCampaign).values(
        id=str(uuid4()),
        user_id=LLOYD_USER_ID,
        credential_id=credential_id,
        assignment_id=assignment_id,
        external_ref=assignment_id,
        **values,
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_skarpe_campaigns_credential_assignment",
        set_={**values, "updated_at": datetime.now(timezone.utc)},
    )
    await db.execute(stmt)


async def _update_campaign_row(
    credential_id: str, assignment_id: str, values: dict
) -> None:
    """One committed transaction per progress step — the durable state."""
    async with AsyncSessionLocal() as db:
        await _upsert_campaign_row(
            db, credential_id=credential_id, assignment_id=assignment_id, values=values,
        )
        await db.commit()


async def _read_pushable_contacts(assignment_id: str) -> list[dict]:
    """(email, first/last name) for every pushable contact of one list.
    Same predicates as the prepare count. Narrow columns, indexed by
    ix_wcm_assignment — one statement is fine at list sizes (≤50k)."""
    m = WebinarContactMembership
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Contact.email, Contact.first_name, Contact.last_name)
            .join(m, m.contact_id == Contact.id)
            .where(
                m.assignment_id == assignment_id,
                m.status == "assigned",
                Contact.user_id == LLOYD_USER_ID,
                ~Contact.is_blocklisted,
                Contact.email.is_not(None),
            )
        )).all()
    contacts = []
    for email, first, last in rows:
        c: dict = {"email": email}
        if first:
            c["first_name"] = first
        if last:
            c["last_name"] = last
        if first or last:
            c["name"] = " ".join(p for p in (first, last) if p)
        contacts.append(c)
    return contacts


async def _run_skarpe_job(
    job_id: str,
    cred_id: str,
    base_url: str,
    api_key: str,
    req: SkarpeCampaignsRequest,
) -> None:
    job = _SKARPE_JOBS[job_id]
    now = datetime.now(timezone.utc)
    policy = req.policy
    async with httpx.AsyncClient() as http:
        # Sequential per list: N is small and Skarpe needn't be hammered.
        for item in req.items:
            j = job["items"][item.assignment_id]
            try:
                j["status"] = "creating"
                draft = await skarpe.create_campaign_draft(
                    base_url, api_key,
                    title=item.title,
                    webinar_number=item.webinar_number,
                    external_ref=item.assignment_id,
                    event_title=item.event_title,
                    event_description=item.event_description,
                    event_location=item.event_location,
                    event_start=item.event_start,
                    event_end=item.event_end,
                    client=http,
                )
                campaign_id = draft.get("campaign_id") or draft.get("id")
                if not campaign_id:
                    raise skarpe.SkarpeError(f"Draft created but no campaign_id in response: {draft}")
                j["campaign_id"] = campaign_id
                j["app_url"] = draft.get("app_url")
                await _update_campaign_row(cred_id, item.assignment_id, {
                    "skarpe_campaign_id": campaign_id,
                    "app_url": draft.get("app_url"),
                    "title": item.title,
                    "webinar_number": item.webinar_number,
                    "status": "draft_created",
                    "error_message": None,
                })

                if req.account_ids:
                    j["status"] = "attaching"
                    await skarpe.attach_sending_accounts(
                        base_url, api_key, campaign_id,
                        req.account_ids, req.daily_limit, client=http,
                    )
                    await _update_campaign_row(cred_id, item.assignment_id, {
                        "skarpe_campaign_id": campaign_id,
                        "accounts_attached": len(req.account_ids),
                        "status": "accounts_attached",
                    })

                if req.push_contacts and policy:
                    j["status"] = "pushing"
                    contacts = await _read_pushable_contacts(item.assignment_id)
                    j["contacts_total"] = len(contacts)
                    await _update_campaign_row(cred_id, item.assignment_id, {
                        "skarpe_campaign_id": campaign_id,
                        "contacts_total": len(contacts),
                        "contacts_pushed": 0,
                        "status": "pushing_contacts",
                        "policy_version": policy.policy_version,
                        "policy_hash": policy.policy_hash,
                        "policy_confirmed_by": policy.confirmed_by,
                        "policy_confirmed_at": now,
                    })
                    pushed = 0
                    for i in range(0, len(contacts), skarpe.CONTACT_CHUNK):
                        chunk = contacts[i : i + skarpe.CONTACT_CHUNK]
                        await skarpe.add_campaign_contacts(
                            base_url, api_key, campaign_id, chunk,
                            policy_version=policy.policy_version,
                            policy_hash=policy.policy_hash,
                            confirmed_by=policy.confirmed_by or "",
                            client=http,
                        )
                        pushed += len(chunk)
                        j["contacts_pushed"] = pushed
                        await _update_campaign_row(cred_id, item.assignment_id, {
                            "skarpe_campaign_id": campaign_id,
                            "contacts_pushed": pushed,
                        })

                j["status"] = "done"
                await _update_campaign_row(cred_id, item.assignment_id, {
                    "skarpe_campaign_id": campaign_id,
                    "status": "completed",
                    "error_message": None,
                })
            except Exception as exc:
                # Per-list failure: record it and keep going with the rest.
                logger.exception("Skarpe job %s failed on assignment %s", job_id, item.assignment_id)
                j["status"] = "error"
                j["error"] = str(exc)[:300]
                if j.get("campaign_id"):
                    try:
                        await _update_campaign_row(cred_id, item.assignment_id, {
                            "skarpe_campaign_id": j["campaign_id"],
                            "status": "failed",
                            "error_message": str(exc)[:500],
                        })
                    except Exception:
                        logger.exception("Failed to record Skarpe error state for %s", item.assignment_id)
            finally:
                job["done"] += 1

    job["status"] = "failed" if all(
        j["status"] == "error" for j in job["items"].values()
    ) else "done"
    job["_ts"] = datetime.now(timezone.utc).timestamp()
    _active_skarpe_tasks.pop(job_id, None)


@router.post("/skarpe/campaigns")
async def create_skarpe_campaigns(
    body: SkarpeCampaignsRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    if not body.items:
        raise HTTPException(400, "items is required")
    for item in body.items:
        if not item.title.strip():
            raise HTTPException(400, "Every campaign needs a title")
    if body.push_contacts:
        p = body.policy
        # The confirmation is the user's own declaration — refuse anything
        # short of an explicit confirmed=true with a name attached.
        if not p or not p.confirmed or not (p.confirmed_by or "").strip():
            raise HTTPException(
                400,
                "Pushing contacts requires confirming the Skarpe contact policy "
                "(confirmed + confirmed_by)",
            )
        if not p.policy_version or not p.policy_hash:
            raise HTTPException(400, "Missing policy_version/policy_hash from prepare")

    cred = await _load_credential(db, body.credential_id)
    # Validate the assignments exist before spawning anything.
    await _load_assignments(db, [i.assignment_id for i in body.items])

    now_ts = datetime.now(timezone.utc).timestamp()
    for jid in [
        jid for jid, j in _SKARPE_JOBS.items()
        if j["status"] != "running" and j["_ts"] < now_ts - 3600
    ]:
        _SKARPE_JOBS.pop(jid, None)

    job_id = str(uuid4())
    job = {
        "id": job_id,
        "status": "running",
        "total": len(body.items),
        "done": 0,
        "items": {
            i.assignment_id: {
                "title": i.title,
                "status": "pending",
                "campaign_id": None,
                "app_url": None,
                "contacts_total": 0,
                "contacts_pushed": 0,
                "error": None,
            }
            for i in body.items
        },
        "_ts": now_ts,
    }
    _SKARPE_JOBS[job_id] = job
    _active_skarpe_tasks[job_id] = asyncio.create_task(
        _run_skarpe_job(job_id, cred.id, cred.base_url, cred.api_key, body)
    )
    return {"job_id": job_id, "total": len(body.items)}


@router.get("/skarpe/jobs/{job_id}")
async def get_skarpe_job(job_id: str, _: str = Depends(require_auth)):
    job = _SKARPE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Skarpe job not found")
    return {k: v for k, v in job.items() if not k.startswith("_")}
