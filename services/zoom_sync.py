"""Zoom webinar registrant + attendance sync — background runner.

Sibling of services/wg_sync.py and deliberately the same shape: runs inside the
existing `_sync_run` context manager so Zoom syncs show up on the Sync page,
can be cancelled, and get swept/recovered exactly like GHL and WebinarGeek
runs. Writes into the SAME shared tables (webinar_broadcasts /
webinar_registrants, provider='zoom'), so nothing downstream needs to know Zoom
exists.

Where it necessarily differs from WebinarGeek:

- Two endpoints, not one. WebinarGeek's /subscriptions returns registration and
  attendance together. Zoom splits them: /webinars/{id}/registrants says who
  signed up, /report/webinars/{uuid}/participants says who attended and for how
  long. They are merged per lowercased email.
- The participants report does not exist until after the webinar ends, and lags.
  A sync that runs too early must NOT write zeros over good data — see
  `report_unavailable` in _sync_one().
- Attendance columns are written as explicit FALSE/0 rather than NULL, because
  the statistics predicate is `(watched_live = TRUE OR minutes_viewing > 0)`
  and NULL would make that expression NULL and drop the row.
- No blocklist writes. Zoom has no unsubscribe concept reachable from the API,
  so unsubscribed_at stays NULL and the 'wg_unsub' path has no Zoom analogue.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from sqlalchemy import select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import ConnectorCredential, GHLSyncRun, Webinar, WebinarBroadcast, WebinarRegistrant
from db.session import AsyncSessionLocal
from integrations import zoom_client as zc
from services.ghl_sync import _heartbeat, _sync_run

logger = logging.getLogger(__name__)

SyncTrigger = Literal["scheduled", "manual"]

PROVIDER = "zoom"
ID_PREFIX = "zoom:"

# Attendance columns. Excluded from the upsert's SET list when the participants
# report was unavailable, so a premature run cannot erase a good earlier sync.
_ATTENDANCE_COLS = (
    "watched_live", "watched_replay", "start_time", "end_time", "minutes_viewing",
    "viewing_device",
)

# How long after a webinar's scheduled END to auto-sync it. WebinarGeek uses a
# flat 2h after START; that does not fit Zoom, whose participant report is only
# generated once the session ends and then lags (tens of minutes; longer for
# large sessions).
AUTO_SYNC_AFTER_END = timedelta(minutes=45)

# Stop chasing a webinar that never produced a report — a scheduled session that
# was cancelled outright would otherwise be polled forever.
AUTO_SYNC_GIVE_UP = timedelta(days=7)

# Assumed length when Zoom gives no duration, for the "has it ended?" estimate.
DEFAULT_DURATION_SECONDS = 3600

# How far from a cached start time an instance may be and still be considered
# the same occurrence. Generous because Zoom records the ACTUAL start and hosts
# routinely begin late.
INSTANCE_MATCH_WINDOW = timedelta(hours=12)

_broadcast_locks: dict[str, asyncio.Lock] = {}
_sync_all_lock = asyncio.Lock()


def _lock_for(broadcast_id: str) -> asyncio.Lock:
    lock = _broadcast_locks.get(broadcast_id)
    if lock is None:
        lock = asyncio.Lock()
        _broadcast_locks[broadcast_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def make_broadcast_id(webinar_id: str | int, occurrence_id: Optional[str] = None) -> str:
    """`zoom:<webinar_id>` or `zoom:<webinar_id>:<occurrence_id>`.

    The prefix is what makes sharing a table with WebinarGeek provably safe:
    WebinarGeek broadcast ids are bare numerics, so no existing
    broadcast-scoped query can ever match a Zoom row.

    The per-instance UUID is deliberately NOT part of this — it does not exist
    until the webinar airs, and the New Webinar modal links future webinars.
    """
    base = f"{ID_PREFIX}{webinar_id}"
    return f"{base}:{occurrence_id}" if occurrence_id else base


def parse_broadcast_id(broadcast_id: str) -> tuple[str, Optional[str]]:
    """Inverse of make_broadcast_id -> (webinar_id, occurrence_id)."""
    rest = broadcast_id[len(ID_PREFIX):] if broadcast_id.startswith(ID_PREFIX) else broadcast_id
    webinar_id, _, occurrence_id = rest.partition(":")
    return webinar_id, (occurrence_id or None)


def is_zoom(broadcast_id: Optional[str]) -> bool:
    return bool(broadcast_id) and broadcast_id.startswith(ID_PREFIX)


def _sync_type(broadcast_id: str) -> str:
    # removeprefix avoids "zoom:zoom:123"; ghl_sync_run.sync_type was widened
    # to TEXT in migration 083 so the occurrence form fits.
    return f"{ID_PREFIX}{broadcast_id.removeprefix(ID_PREFIX)}"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
async def _load_session(db) -> zc.ZoomSession:
    row = (await db.execute(
        select(ConnectorCredential).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()
    if not row or not row.api_key or not row.account_id or not row.client_id:
        raise RuntimeError("Zoom is not connected — add the credentials on the Connectors page")
    return zc.ZoomSession(row.account_id, row.client_id, row.api_key)


async def _credential_id(db) -> Optional[str]:
    return (await db.execute(
        select(ConnectorCredential.id).where(
            ConnectorCredential.provider == PROVIDER,
            ConnectorCredential.name == "default",
        )
    )).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------
def _clean_email(value: Any) -> str:
    """Lowercase and strip.

    Non-negotiable: webinar_registrants carries
    `CHECK (email = lower(email)) NOT VALID` from migration 069. NOT VALID skips
    the backfill scan but still enforces on every INSERT, so one mixed-case
    address would abort the whole sync run.
    """
    return (value or "").strip().lower()


def _split_name(full: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    parts = (full or "").strip().split(" ", 1)
    if not parts or not parts[0]:
        return None, None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def _company_from(reg: dict[str, Any]) -> Optional[str]:
    if reg.get("org"):
        return reg["org"]
    for q in reg.get("custom_questions") or []:
        title = (q.get("title") or "").lower()
        if "compan" in title or "organi" in title:
            return q.get("value") or None
    return None


def _registrant_row(broadcast_id: str, reg: dict[str, Any]) -> dict[str, Any]:
    """A registration with no attendance yet — explicit FALSE/0, never NULL."""
    return {
        "broadcast_id": broadcast_id,
        "provider": PROVIDER,
        "subscriber_id": str(reg["id"]) if reg.get("id") is not None else None,
        "email": _clean_email(reg.get("email")),
        "first_name": reg.get("first_name") or None,
        "last_name": reg.get("last_name") or None,
        "company": _company_from(reg),
        "job_title": reg.get("job_title") or None,
        "phone": reg.get("phone") or None,
        "city": reg.get("city") or None,
        "country": reg.get("country") or None,
        "timezone": None,
        "registration_source": "zoom_registrant",
        "subscribed_at": zc.parse_dt(reg.get("create_time")),
        "unsubscribed_at": None,
        "unsubscribe_source": None,
        "watched_live": False,
        "watched_replay": None,
        "start_time": None,
        "end_time": None,
        "minutes_viewing": 0,
        "viewing_country": None,
        "viewing_device": None,
        "watch_link": reg.get("join_url") or None,
        "raw": {"provider": PROVIDER, "registrant": reg, "participants": []},
        "synced_at": datetime.now(timezone.utc),
    }


def _participant_only_row(broadcast_id: str, email: str, agg: dict[str, Any]) -> dict[str, Any]:
    """Someone who attended without a registrant record (panelist, or registered
    outside the tracked occurrence). Counted, but with no registration time."""
    first, last = _split_name(agg.get("name"))
    return {
        "broadcast_id": broadcast_id,
        "provider": PROVIDER,
        "subscriber_id": f"p:{agg['participant_id']}" if agg.get("participant_id") else None,
        "email": email,
        "first_name": first,
        "last_name": last,
        "company": None,
        "job_title": None,
        "phone": None,
        "city": None,
        "country": None,
        "timezone": None,
        "registration_source": "zoom_participant_only",
        "subscribed_at": None,
        "unsubscribed_at": None,
        "unsubscribe_source": None,
        "watched_live": False,
        "watched_replay": None,
        "start_time": None,
        "end_time": None,
        "minutes_viewing": 0,
        "viewing_country": None,
        "viewing_device": None,
        "watch_link": None,
        "raw": {"provider": PROVIDER, "registrant": None, "participants": []},
        "synced_at": datetime.now(timezone.utc),
    }


def aggregate_participants(
    participants: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Collapse the participant report into one entry per email.

    Zoom emits one row per JOIN, so a person who drops and rejoins — or joins on
    two devices — appears several times. Returns
    (by_email, emailless_attendee_count, unit_mismatch_count).

    Rows with no email cannot be tied to a contact and are excluded from the
    per-person result, but ARE counted: they still watched, and the broadcast's
    live_viewers_count is what Statistics reads for the webinar total.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    emailless: set[str] = set()
    emailless_rows = 0

    for p in participants:
        email = _clean_email(p.get("user_email"))
        if not email:
            marker = str(p.get("participant_user_id") or p.get("id") or "")
            if marker:
                emailless.add(marker)
            else:
                emailless_rows += 1
            continue
        grouped.setdefault(email, []).append(p)

    by_email: dict[str, dict[str, Any]] = {}
    unit_mismatches = 0
    for email, rows in grouped.items():
        segments: list[tuple[datetime, datetime]] = []
        reported_total = 0
        for r in rows:
            join, leave, reported = zc.participant_segment(r)
            reported_total += reported
            if join and leave:
                segments.append((join, leave))

        measured = zc.merge_watch_seconds(segments)
        # Fall back to Zoom's own duration when join/leave are missing entirely;
        # otherwise prefer the measured value, which cannot double-count.
        seconds = measured if segments else reported_total
        if zc.duration_units_suspect(reported_total, measured):
            unit_mismatches += 1

        starts = [s for s, _ in segments]
        ends = [e for _, e in segments]
        first = rows[0]
        by_email[email] = {
            "seconds": seconds,
            "start_time": min(starts) if starts else None,
            "end_time": max(ends) if ends else None,
            "name": first.get("name"),
            "participant_id": first.get("id") or first.get("participant_user_id"),
            "registrant_id": next((r.get("registrant_id") for r in rows if r.get("registrant_id")), None),
            "device": first.get("device") or None,
            "rows": rows,
        }

    attended_emailless = len(emailless) + emailless_rows
    return by_email, attended_emailless, unit_mismatches


def build_rows(
    broadcast_id: str,
    regs: list[dict[str, Any]],
    by_email: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge registrants and aggregated attendance into one row per person.

    Matching order matters. An attendee is tied to a registration by
    `registrant_id` first and only then by email, because people routinely
    register with a work address and then join signed into Zoom with a personal
    one. Matching on email alone would file that person twice — a phantom
    no-show against the registration, plus an orphan attendee row — inflating
    registrations and hiding the attendance.
    """
    rows: dict[str, dict[str, Any]] = {}
    by_registrant_id: dict[str, str] = {}

    for reg in regs:
        email = _clean_email(reg.get("email"))
        if not email:
            continue
        rows[email] = _registrant_row(broadcast_id, reg)
        if reg.get("id") is not None:
            by_registrant_id[str(reg["id"])] = email

    for email, agg in by_email.items():
        target = email
        if email not in rows:
            rid = agg.get("registrant_id")
            mapped = by_registrant_id.get(str(rid)) if rid is not None else None
            if mapped:
                target = mapped
            else:
                rows[email] = _participant_only_row(broadcast_id, email, agg)
        _overlay_attendance(rows[target], agg)

    return rows


def _overlay_attendance(row: dict[str, Any], agg: dict[str, Any]) -> None:
    seconds = int(agg.get("seconds") or 0)
    row["watched_live"] = seconds > 0
    row["minutes_viewing"] = seconds // 60
    row["start_time"] = agg.get("start_time")
    row["end_time"] = agg.get("end_time")
    row["viewing_device"] = agg.get("device")
    raw = row.get("raw") or {}
    raw["participants"] = agg.get("rows") or []
    row["raw"] = raw


# ---------------------------------------------------------------------------
# Instance resolution
# ---------------------------------------------------------------------------
async def resolve_instance_uuid(
    session: zc.ZoomSession, webinar_id: str, starts_at: Optional[datetime]
) -> Optional[str]:
    """Find the past-instance UUID for a cached broadcast.

    /past_webinars/{id}/instances returns uuid + start_time but does NOT
    reliably return occurrence_id, so this is a nearest-start_time match rather
    than a lookup. Hosts start late, hence the wide window.
    """
    instances = await zc.list_past_instances(session, webinar_id)
    if not instances:
        return None
    if not starts_at or len(instances) == 1:
        return instances[0].get("uuid")

    scored: list[tuple[float, str]] = []
    for inst in instances:
        when = zc.parse_dt(inst.get("start_time"))
        uuid = inst.get("uuid")
        if not when or not uuid:
            continue
        delta = abs((when - starts_at).total_seconds())
        if delta <= INSTANCE_MATCH_WINDOW.total_seconds():
            scored.append((delta, uuid))
    if not scored:
        return None
    scored.sort(key=lambda p: p[0])
    if len(scored) > 1:
        logger.warning(
            "Zoom webinar %s has %d instances within the match window of %s; "
            "taking the nearest. A same-day re-run may need a human look.",
            webinar_id, len(scored), starts_at,
        )
    return scored[0][1]


# ---------------------------------------------------------------------------
# The sync
# ---------------------------------------------------------------------------
# Rows between progress heartbeats during the upsert loop. Each row is its own
# round trip, so a big webinar spends minutes in here; without heartbeats the
# Sync page shows 0 the whole time and the stale-run sweeper has nothing to
# distinguish "working" from "died".
_HEARTBEAT_EVERY = 250


async def _sync_one(
    db, session: zc.ZoomSession, bc: WebinarBroadcast, state=None
) -> tuple[int, bool]:
    """Sync one cached Zoom broadcast. Returns (rows_written, report_seen)."""
    webinar_id, occurrence_id = parse_broadcast_id(bc.broadcast_id)

    regs = await zc.list_registrants(session, webinar_id, occurrence_id)

    instance_uuid = bc.platform_instance_id
    if not instance_uuid:
        instance_uuid = await resolve_instance_uuid(session, webinar_id, bc.starts_at)
        if instance_uuid:
            bc.platform_instance_id = instance_uuid
            bc.has_ended = True

    participants: Optional[list[dict[str, Any]]] = None
    if instance_uuid:
        participants = await zc.list_participants(session, instance_uuid)

    # None means "Zoom has not generated the report yet", which is different
    # from "nobody came". Only the latter may write zeros.
    report_seen = participants is not None
    by_email, emailless, unit_mismatches = aggregate_participants(participants or [])

    if unit_mismatches:
        logger.error(
            "Zoom broadcast %s: %d participant(s) whose reported `duration` disagrees "
            "with join/leave by >2x. Suspect a seconds-vs-minutes change; "
            "minutes_viewing may be wrong.",
            bc.broadcast_id, unit_mismatches,
        )

    rows = build_rows(bc.broadcast_id, regs, by_email)

    if state is not None:
        state.expected_total = len(rows)
        await _heartbeat(state)

    for i, row in enumerate(rows.values(), 1):
        stmt = pg_insert(WebinarRegistrant).values(**row)
        set_cols = {
            k: v for k, v in row.items()
            if k not in ("broadcast_id", "email")
            and not (not report_seen and k in _ATTENDANCE_COLS)
        }
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["broadcast_id", "email"], set_=set_cols,
        ))
        if state is not None and i % _HEARTBEAT_EVERY == 0:
            state.contacts_synced = i
            # Also the cancellation point: _heartbeat raises CancelledError when
            # the run has been cancelled, so a long sync can be stopped.
            await _heartbeat(state)

    bc.subscriptions_count = len(regs)
    if report_seen:
        # Includes attendees with no email: they are dropped from the per-person
        # table but Statistics reads THIS number for the webinar total (see
        # ghl_statistics_source._fetch_wg_broadcast_totals), so omitting them
        # would under-report attendance.
        bc.live_viewers_count = sum(1 for a in by_email.values() if a["seconds"] > 0) + emailless
    bc.replay_viewers_count = 0
    bc.last_synced_at = datetime.now(timezone.utc)

    return len(rows), report_seen


async def run_broadcast_sync(broadcast_id: str, trigger: SyncTrigger = "manual") -> str:
    """Sync one Zoom broadcast. Returns the sync_run id."""
    lock = _lock_for(broadcast_id)
    if lock.locked():
        raise RuntimeError(f"Zoom webinar {broadcast_id} is already syncing")

    async with lock:
        async with _sync_run(_sync_type(broadcast_id), trigger) as state:
            async with AsyncSessionLocal() as db:
                session = await _load_session(db)
                bc = (await db.execute(
                    select(WebinarBroadcast).where(
                        WebinarBroadcast.broadcast_id == broadcast_id,
                        WebinarBroadcast.provider == PROVIDER,
                    )
                )).scalar_one_or_none()
                if not bc:
                    raise RuntimeError(f"Zoom webinar {broadcast_id} is not cached — refresh first")

                try:
                    count, _ = await _sync_one(db, session, bc, state)
                except zc.ZoomError as e:
                    raise RuntimeError(f"Zoom API error: {e}") from e

                await db.commit()

            state.contacts_synced = count
            await _heartbeat(state)

            from services.statistics import invalidate_stats_cache
            from services.statistics_snapshot import schedule_recompute_for_broadcast
            invalidate_stats_cache()
            schedule_recompute_for_broadcast(broadcast_id)

            return state.run_id


async def run_sync_all(trigger: SyncTrigger = "manual") -> str:
    """Sync every cached Zoom broadcast, sequentially.

    Sequential and paced on purpose: the participants report is a "Heavy" tier
    endpoint with a daily allowance, and firing them concurrently is the fastest
    way to burn it on a single click.
    """
    if _sync_all_lock.locked():
        raise RuntimeError("A Zoom sync-all is already running")

    async with _sync_all_lock:
        async with _sync_run(f"{ID_PREFIX}all", trigger) as state:
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(
                    select(WebinarBroadcast).where(WebinarBroadcast.provider == PROVIDER)
                )).scalars().all()
            state.expected_total = len(rows)
            await _heartbeat(state)

            for r in rows:
                try:
                    await run_broadcast_sync(r.broadcast_id, trigger=trigger)
                except Exception as exc:
                    state.errors.append({
                        "type": "broadcast", "broadcast_id": r.broadcast_id, "error": str(exc)[:500]
                    })
                    logger.warning("Zoom sync-all: %s failed: %s", r.broadcast_id, exc)
                state.contacts_synced += 1
                await _heartbeat(state)
                await asyncio.sleep(1.0)

            return state.run_id


# ---------------------------------------------------------------------------
# Refresh (webinar list)
# ---------------------------------------------------------------------------
def _broadcast_values(
    webinar: dict[str, Any],
    credential_id: Optional[str],
    occurrence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    wid = str(webinar.get("id"))
    occ_id = str(occurrence.get("occurrence_id")) if occurrence else None
    starts_at = zc.parse_dt(
        (occurrence or {}).get("start_time") or webinar.get("start_time")
    )
    duration_minutes = (occurrence or {}).get("duration") or webinar.get("duration")
    try:
        duration_seconds = int(duration_minutes) * 60 if duration_minutes else None
    except (TypeError, ValueError):
        duration_seconds = None

    return {
        "broadcast_id": make_broadcast_id(wid, occ_id),
        "provider": PROVIDER,
        "credential_id": credential_id,
        "webinar_id": wid,
        "occurrence_id": occ_id,
        "name": webinar.get("topic") or f"Zoom webinar {wid}",
        "internal_title": webinar.get("agenda") or None,
        "starts_at": starts_at,
        "duration_seconds": duration_seconds,
        "raw": webinar,
        "updated_at": datetime.now(timezone.utc),
    }


async def refresh_webinars() -> int:
    """Cache the account's webinars (scheduled + past) into webinar_broadcasts.

    Server-to-Server OAuth has no "me" in the user sense — /users/me is the app
    owner, but webinars can be hosted by anyone on the account — so this walks
    the host list. Recurring webinars are expanded into one row per occurrence,
    which is what lets a planning webinar link a single occurrence.
    """
    async with AsyncSessionLocal() as db:
        session = await _load_session(db)
        cred_id = await _credential_id(db)

        users = await zc.list_users(session)
        seen: list[dict[str, Any]] = []
        for u in users:
            uid = u.get("id")
            if not uid:
                continue
            for kind in ("scheduled", "past"):
                seen.extend(await zc.list_webinars(session, uid, kind))

        # De-dupe: a webinar can surface under more than one listing.
        by_id: dict[str, dict[str, Any]] = {}
        for w in seen:
            if w.get("id") is not None:
                by_id[str(w["id"])] = w

        total = 0
        for wid, w in by_id.items():
            rows: list[dict[str, Any]] = []
            # Types 6 and 9 are recurring; only those need the extra detail call.
            if w.get("type") in (6, 9):
                detail = await zc.get_webinar(session, wid)
                occurrences = (detail or {}).get("occurrences") or []
                base = detail or w
                for occ in occurrences:
                    rows.append(_broadcast_values(base, cred_id, occ))
                if not rows:
                    rows.append(_broadcast_values(base, cred_id))
            else:
                rows.append(_broadcast_values(w, cred_id))

            for values in rows:
                stmt = pg_insert(WebinarBroadcast).values(**values)
                set_cols = {k: v for k, v in values.items() if k != "broadcast_id"}
                # Never let a later refresh blank out something already known.
                set_cols["internal_title"] = sa_text(
                    "COALESCE(EXCLUDED.internal_title, webinar_broadcasts.internal_title)"
                )
                set_cols["starts_at"] = sa_text(
                    "COALESCE(EXCLUDED.starts_at, webinar_broadcasts.starts_at)"
                )
                await db.execute(stmt.on_conflict_do_update(
                    index_elements=["broadcast_id"], set_=set_cols,
                ))
                total += 1

        await db.commit()
        return total


# ---------------------------------------------------------------------------
# Scheduled auto-sync
# ---------------------------------------------------------------------------
async def _run_succeeded(run_id: str) -> bool:
    """Did this sync_run finish clean?

    `_sync_run` catches exceptions, marks the run failed and does NOT re-raise,
    so a caller that only uses try/except cannot tell. Stamping the one-shot
    marker on a failed run would stop the webinar being retried, ever.
    """
    async with AsyncSessionLocal() as db:
        status = (await db.execute(
            select(GHLSyncRun.status).where(GHLSyncRun.id == run_id)
        )).scalar_one_or_none()
    return status == "completed"


async def run_due_broadcast_autosyncs() -> int:
    """Auto-sync Zoom-linked webinars whose session has ended.

    Keys off the estimated END time, not the start: the participants report only
    exists once the session finishes, and then lags. `broadcast_auto_synced_at`
    is stamped only when a report was actually retrieved, so a lagging report is
    retried on the next tick instead of being written off with zeros.
    """
    async with AsyncSessionLocal() as db:
        due = (await db.execute(sa_text(
            """
            SELECT w.id, w.broadcast_id
            FROM webinars w
            JOIN webinar_broadcasts b ON b.broadcast_id = w.broadcast_id
            WHERE w.broadcast_auto_synced_at IS NULL
              AND b.provider = :provider
              AND b.starts_at IS NOT NULL
              AND b.starts_at + make_interval(
                    secs => COALESCE(b.duration_seconds, :default_secs)
                  ) <= now() - make_interval(secs => :grace_secs)
              AND b.starts_at > now() - make_interval(secs => :give_up_secs)
            """
        ).bindparams(
            provider=PROVIDER,
            default_secs=DEFAULT_DURATION_SECONDS,
            grace_secs=int(AUTO_SYNC_AFTER_END.total_seconds()),
            give_up_secs=int(AUTO_SYNC_GIVE_UP.total_seconds()),
        ))).all()

    synced = 0
    for webinar_id, broadcast_id in due:
        try:
            run_id = await run_broadcast_sync(broadcast_id, trigger="scheduled")
        except Exception as exc:
            logger.warning(
                "Zoom auto-sync: %s (webinar %s) failed, will retry: %s",
                broadcast_id, webinar_id, exc,
            )
            continue

        # `_sync_run` records a failure on the row but does not re-raise, so
        # returning normally is not proof of success — read the row back.
        if not await _run_succeeded(run_id):
            logger.warning(
                "Zoom auto-sync: %s run %s did not complete; not stamping",
                broadcast_id, run_id,
            )
            continue

        # Only a retrieved report means the attendance numbers are real; without
        # one, leave the stamp NULL so the next tick tries again.
        async with AsyncSessionLocal() as db:
            has_report = (await db.execute(
                select(WebinarBroadcast.platform_instance_id)
                .where(WebinarBroadcast.broadcast_id == broadcast_id)
            )).scalar_one_or_none()
            if not has_report:
                continue
            await db.execute(
                update(Webinar)
                .where(Webinar.id == webinar_id)
                .values(broadcast_auto_synced_at=datetime.now(timezone.utc))
            )
            await db.commit()
        synced += 1
        logger.info("Zoom auto-sync: synced %s for webinar %s", broadcast_id, webinar_id)
    return synced
