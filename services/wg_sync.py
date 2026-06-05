"""WebinarGeek broadcast subscriber sync — background runner.

Mirrors the GHL sync lifecycle so per-broadcast WG syncs show up on the
Sync page alongside GHL runs, can be cancelled, and survive page navigation.

Design:
- Writes into the existing `ghl_sync_run` table with sync_type prefix
  ``wg:<broadcast_id>`` (or ``wg:all`` for the sync-all umbrella). Reuses
  the `_sync_run` context manager so heartbeats / cancel / sweeper /
  orphan recovery all work without duplication.
- `contacts_synced` column is repurposed to carry the synced subscriber
  count for these rows. The sync_type prefix tells the UI it's a WG run.
- Independent per-broadcast asyncio locks (no global lock with GHL).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import BlocklistEntry, ConnectorCredential, Webinar, WebinarGeekSubscriber, WebinarGeekWebinar
from db.session import AsyncSessionLocal
from integrations import webinargeek_client as wg
from services.ghl_sync import _heartbeat, _sync_run

logger = logging.getLogger(__name__)

SyncTrigger = Literal["scheduled", "manual"]

PROVIDER = "webinargeek"

# Per-broadcast locks: prevent two concurrent syncs of the same broadcast
# without blocking unrelated GHL syncs or other broadcasts.
_broadcast_locks: dict[str, asyncio.Lock] = {}
_sync_all_lock = asyncio.Lock()


def _lock_for(broadcast_id: str) -> asyncio.Lock:
    lock = _broadcast_locks.get(broadcast_id)
    if lock is None:
        lock = asyncio.Lock()
        _broadcast_locks[broadcast_id] = lock
    return lock


async def _get_api_key_default(db) -> str | None:
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()
    return row.api_key if row else None


async def _resolve_api_key_for_broadcast(db, broadcast_id: str) -> str:
    # Authoritative: the credential that surfaced this broadcast on the
    # last refresh (stamped on webinargeek_webinars.credential_id).
    wb_cred = (await db.execute(
        select(ConnectorCredential)
        .join(WebinarGeekWebinar, WebinarGeekWebinar.credential_id == ConnectorCredential.id)
        .where(
            WebinarGeekWebinar.broadcast_id == broadcast_id,
            ConnectorCredential.provider == PROVIDER,
        )
        .limit(1)
    )).scalar_one_or_none()
    if wb_cred:
        return wb_cred.api_key

    # Legacy fallback: variant mapping on the Webinar row (for broadcasts
    # cached before migration 042, before credential_id was stamped).
    cred_row = (await db.execute(
        select(ConnectorCredential)
        .join(Webinar, Webinar.webinargeek_credential_id == ConnectorCredential.id)
        .where(
            Webinar.broadcast_id == broadcast_id,
            ConnectorCredential.provider == PROVIDER,
        )
        .limit(1)
    )).scalar_one_or_none()
    if cred_row:
        return cred_row.api_key
    key = await _get_api_key_default(db)
    if not key:
        raise RuntimeError("WebinarGeek API key not configured")
    return key


def _subscriber_values(broadcast_id: str, s: dict) -> dict:
    wd = s.get("watch_duration")
    minutes = int(wd // 60) if isinstance(wd, (int, float)) else None
    return {
        "broadcast_id": broadcast_id,
        "subscriber_id": str(s.get("id")) if s.get("id") is not None else None,
        "email": (s.get("email") or "").strip(),
        "first_name": s.get("firstname"),
        "last_name": s.get("surname"),
        "company": s.get("company"),
        "job_title": s.get("job_title"),
        "phone": s.get("phone"),
        "city": s.get("city"),
        "country": s.get("country"),
        "timezone": s.get("time_zone"),
        "registration_source": s.get("registration_source"),
        "subscribed_at": wg.unix_to_dt(s.get("created_at")),
        "unsubscribed_at": wg.unix_to_dt(s.get("unsubscribed_at")),
        "unsubscribe_source": s.get("unsubscription_source"),
        "watched_live": s.get("watched_live"),
        "watched_replay": s.get("watched_replay"),
        "start_time": wg.unix_to_dt(s.get("watch_start")),
        "end_time": wg.unix_to_dt(s.get("watch_end")),
        "minutes_viewing": minutes,
        "viewing_country": s.get("viewing_country"),
        "viewing_device": s.get("viewing_device"),
        "watch_link": s.get("watch_link"),
        "raw": s,
        "synced_at": datetime.now(timezone.utc),
    }


async def _upsert_one_broadcast(db, api_key: str, broadcast_id: str) -> int:
    subs = await wg.list_subscriptions(api_key, broadcast_id)
    blocklist_rows: list[dict] = []
    for s in subs:
        values = _subscriber_values(broadcast_id, s)
        if not values["email"]:
            continue
        stmt = pg_insert(WebinarGeekSubscriber).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["broadcast_id", "email"],
            set_={k: v for k, v in values.items() if k not in ("broadcast_id", "email")},
        )
        await db.execute(stmt)
        if values.get("unsubscribed_at"):
            blocklist_rows.append({
                "user_id": LLOYD_USER_ID,
                "email": values["email"].lower(),
                "source": "wg_unsub",
                "reason": values.get("unsubscribe_source") or "WebinarGeek unsubscribed",
                "source_ref": values.get("subscriber_id"),
            })
    if blocklist_rows:
        seen: set[str] = set()
        deduped = []
        for r in blocklist_rows:
            if r["email"] in seen:
                continue
            seen.add(r["email"])
            deduped.append(r)
        bl_stmt = pg_insert(BlocklistEntry).values(deduped).on_conflict_do_nothing(
            index_elements=["user_id", "email"]
        )
        try:
            await db.execute(bl_stmt)
        except Exception as exc:
            logger.warning("Failed to upsert blocklist from WG broadcast %s: %s", broadcast_id, exc)
    return len(subs)


async def run_broadcast_sync(broadcast_id: str, trigger: SyncTrigger = "manual") -> str:
    """Run a single-broadcast WG subscriber sync. Returns sync_run id."""
    lock = _lock_for(broadcast_id)
    if lock.locked():
        raise RuntimeError(f"Broadcast {broadcast_id} is already syncing")

    async with lock:
        async with _sync_run(f"wg:{broadcast_id}", trigger) as state:
            async with AsyncSessionLocal() as db:
                api_key = await _resolve_api_key_for_broadcast(db, broadcast_id)
                wb = (await db.execute(
                    select(WebinarGeekWebinar).where(WebinarGeekWebinar.broadcast_id == broadcast_id)
                )).scalar_one_or_none()
                if not wb:
                    raise RuntimeError(f"Broadcast {broadcast_id} not cached — refresh first")

                try:
                    count = await _upsert_one_broadcast(db, api_key, broadcast_id)
                except wg.WebinarGeekError as e:
                    raise RuntimeError(f"WebinarGeek API error: {e}") from e

                wb.last_synced_at = datetime.now(timezone.utc)
                await db.commit()

            state.contacts_synced = count
            await _heartbeat(state)
            return state.run_id


# How long after a broadcast's start time to auto-sync its subscribers, once.
AUTO_SYNC_DELAY = timedelta(hours=2)


async def run_due_broadcast_autosyncs() -> int:
    """Auto-sync subscribers for any planned webinar whose linked WebinarGeek
    broadcast started >= AUTO_SYNC_DELAY ago and hasn't been auto-synced yet.

    Keys off the broadcast's actual start time (webinargeek_webinars.starts_at),
    not the planned Webinar.date. Fires exactly once per webinar — stamps
    Webinar.broadcast_auto_synced_at only on success, so a transient failure
    (API error, or "already syncing" from a concurrent manual run) is retried on
    the next scheduler tick. Idempotent; safe to call repeatedly.

    Returns the number of webinars auto-synced this pass.
    """
    cutoff = datetime.now(timezone.utc) - AUTO_SYNC_DELAY
    async with AsyncSessionLocal() as db:
        due = (await db.execute(
            select(Webinar.id, Webinar.broadcast_id)
            .join(WebinarGeekWebinar, WebinarGeekWebinar.broadcast_id == Webinar.broadcast_id)
            .where(
                Webinar.broadcast_id.isnot(None),
                Webinar.broadcast_auto_synced_at.is_(None),
                WebinarGeekWebinar.starts_at.isnot(None),
                WebinarGeekWebinar.starts_at <= cutoff,
            )
        )).all()

    synced = 0
    for webinar_id, broadcast_id in due:
        try:
            await run_broadcast_sync(broadcast_id, trigger="scheduled")
        except Exception as exc:
            logger.warning(
                "auto-sync: broadcast %s (webinar %s) failed, will retry: %s",
                broadcast_id, webinar_id, exc,
            )
            continue
        # Stamp only after a successful sync → one-shot.
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Webinar)
                .where(Webinar.id == webinar_id)
                .values(broadcast_auto_synced_at=datetime.now(timezone.utc))
            )
            await db.commit()
        synced += 1
        logger.info("auto-sync: synced broadcast %s for webinar %s", broadcast_id, webinar_id)
    return synced


async def run_sync_all(trigger: SyncTrigger = "manual") -> str:
    """Sync subscribers for every cached broadcast, sequentially. Each
    broadcast gets its own per-broadcast sync_run row; this umbrella row
    tracks aggregate progress (contacts_synced = total subscribers across
    all broadcasts; expected_total = broadcast count).
    """
    if _sync_all_lock.locked():
        raise RuntimeError("A WG sync-all is already running")

    async with _sync_all_lock:
        async with _sync_run("wg:all", trigger) as state:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(WebinarGeekWebinar))).scalars().all()
            state.expected_total = len(rows)
            await _heartbeat(state)

            # Umbrella row's contacts_synced tracks broadcasts processed,
            # not subscriber totals — subscriber counts live on each child run.
            for r in rows:
                try:
                    await run_broadcast_sync(r.broadcast_id, trigger=trigger)
                except Exception as exc:
                    state.errors.append({
                        "type": "broadcast", "broadcast_id": r.broadcast_id, "error": str(exc)[:500]
                    })
                    logger.warning("sync-all: broadcast %s failed: %s", r.broadcast_id, exc)
                state.contacts_synced += 1
                await _heartbeat(state)

            return state.run_id
