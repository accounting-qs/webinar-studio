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

# Held around ONE webinar's compute+upsert, not around a whole run. A full
# sweep re-queues on it between webinars, so a targeted recompute — the kind a
# CSV import or a sync schedules — waits for the webinar in flight and then goes
# next instead of behind the entire sweep. On prod a single webinar takes
# 25-40 min (the non-joiner pool query dominates), so a full sweep runs for
# hours; holding this for the whole run left a freshly-uploaded webinar's
# snapshot stale for that long.
_recompute_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()
# Scope tokens already queued/running, so duplicate schedules coalesce. The
# sentinel "*" means a full ("all") recompute.
_pending_scopes: set[str] = set()
_ALL = "*"
# Identity of the run currently reporting into `_status`. A full sweep always
# takes it over; a partial that has been superseded stops writing, so it can't
# clear "running" out from under a sweep that started after it.
_status_owner: object | None = None


async def recompute(webinar_ids: list[str] | None = None, source: str = "auto") -> dict[str, Any]:
    """Compute fresh statistics payloads and upsert them. webinar_ids=None
    rebuilds every passed webinar (and prunes snapshots for webinars that no
    longer pass). Serialized per webinar, not per run: a full sweep yields
    between webinars so a targeted recompute doesn't queue behind all of it."""
    from services import statistics as stats

    global _status_owner

    is_full = webinar_ids is None
    if is_full:
        summaries = await stats.get_statistics_webinar_list(source=source)
        targets = [
            s["webinarId"] for s in summaries
            if s.get("webinarId") and stats._is_passed_webinar(s.get("date"), s.get("status"))
        ]
    else:
        targets = list(dict.fromkeys(webinar_ids))  # de-dupe, preserve order

    # A partial run interleaves with a full sweep, so it must not stomp the
    # sweep's progress in the status the UI polls. The sweep owns it; a partial
    # only reports when no sweep is in flight.
    token = object()
    if is_full or not (_status["running"] and _status["scope"] == "all"):
        _status_owner = token
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

    logger.info("recompute: %s run over %d webinar(s)", "full" if is_full else "partial", len(targets))
    try:
        for wid in targets:
            try:
                # Re-acquired per webinar so anything waiting gets the next slot.
                async with _recompute_lock:
                    payload = await stats.get_statistics_webinar_one(
                        source=source, webinar_id=wid, force=True,
                    )
                    if payload is not None:
                        await _upsert_snapshot(source, payload)
            except Exception as exc:
                if _status_owner is token:
                    _status["errors"] += 1
                    _status["last_error"] = str(exc)[:300]
                logger.warning("recompute: webinar %s failed: %s", wid, exc)
            finally:
                if _status_owner is token:
                    _status["done"] += 1

        if is_full:
            try:
                await _prune_snapshots(source, set(targets))
            except Exception as exc:
                logger.warning("recompute: prune failed: %s", exc)
    finally:
        if _status_owner is token:
            _status["running"] = False
            _status["finished_at"] = datetime.now(timezone.utc).isoformat()
            _status_owner = None
        logger.info("recompute: %s run finished", "full" if is_full else "partial")

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
    """Recompute every webinar linked to a WebinarGeek broadcast id — plus the
    webinars that read this one as a non-joiner source.

    A broadcast's registrations/attendance feed the non-joiner pool of the next
    NONJOINER_WINDOW webinars (services/nonjoiners.py), so re-syncing W150 also
    changes W151-W156's Nonjoiners rows. Without the successor sweep those
    snapshots keep stale pool sizes until something else happens to touch them.
    """
    if not broadcast_id:
        return

    async def _run():
        from sqlalchemy import select
        from db.models import Webinar
        from db.session import AsyncSessionLocal
        from services.nonjoiners import NONJOINER_WINDOW
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Webinar.id, Webinar.user_id, Webinar.number)
                .where(Webinar.broadcast_id == broadcast_id)
            )).all()
            ids = [r[0] for r in rows]
            for _id, uid, num in rows:
                if num is None:
                    continue
                ids.extend((await db.execute(
                    select(Webinar.id).where(
                        Webinar.user_id == uid,
                        Webinar.number > num,
                        Webinar.number <= num + NONJOINER_WINDOW,
                    )
                )).scalars().all())
        if ids:
            await recompute(list(dict.fromkeys(ids)), source=source)

    _spawn(f"bcast:{_snap_source(source)}:{broadcast_id}", _run)


# ---------------------------------------------------------------------------
# Sync-scoped recompute
# ---------------------------------------------------------------------------

# Past this many touched contacts, deriving the exact affected set costs more
# than it saves — and a sync that large is a full sync, which wants a full
# rebuild anyway.
_SCOPE_ROW_LIMIT = 50_000

# How each source column attributes to a webinar. Kept next to the queries
# below so a new metric reading a new column is an obvious edit here too.
#
#   ghl_contact.calendar_webinar_series_history      -> e{N} token  (number)
#   ghl_contact.calendar_webinar_series_non_joiners  -> e{N} token  (number)
#   ghl_contact.booked_call_webinar_series           -> N           (number)
#   ghl_contact.webinar_registration_in_form_date    -> (prev, current] window
#   ghl_contact.cold_calendar_unsubscribe_date       -> (prev, current] window
#   ghl_opportunity.webinar_source_number            -> N           (number)
#   ghl_appointment -> its contact's opportunity     -> N           (number)
#
# Contacts are scoped on synced_at: the incremental contact stream is already
# filtered server-side by GHL, so a written row is a changed row (measured:
# 50–7,000 rows per incremental, vs 147k on a full sync, which trips the cap
# below and falls back to a full rebuild — the right outcome).
_TOUCHED_CONTACTS_SQL = """
WITH touched AS (
    SELECT calendar_webinar_series_history      AS hist,
           calendar_webinar_series_non_joiners  AS nj,
           booked_call_webinar_series           AS booked,
           webinar_registration_in_form_date    AS form_date,
           cold_calendar_unsubscribe_date       AS unsub_date
    FROM ghl_contact
    WHERE synced_at >= :since
    LIMIT :cap
)
SELECT
    (SELECT count(*) FROM touched) AS n,
    (SELECT array_agg(DISTINCT m[1]::int)
       FROM touched t,
            LATERAL regexp_matches(
                lower(coalesce(t.hist, '') || ',' || coalesce(t.nj, '')),
                'e([0-9]+)', 'g'
            ) AS m
    ) AS token_numbers,
    (SELECT array_agg(DISTINCT booked) FROM touched WHERE booked IS NOT NULL) AS booked_numbers,
    (SELECT array_agg(DISTINCT d)
       FROM (SELECT form_date AS d FROM touched
             UNION SELECT unsub_date FROM touched) x
      WHERE d IS NOT NULL
    ) AS touched_dates
"""

# Opportunities are scoped on updated_at_ghl, NOT synced_at: every sync
# re-upserts the whole opportunity table, so synced_at marks ~3,671 of 4,005
# rows across 138 webinar numbers (i.e. everything) while updated_at_ghl marks
# the ~59 rows across 17 numbers that GHL actually changed.
_TOUCHED_OPPS_SQL = """
SELECT DISTINCT webinar_source_number
FROM ghl_opportunity
WHERE updated_at_ghl >= :since AND webinar_source_number IS NOT NULL
"""

# Appointment-derived call1/call2 columns are written straight onto
# ghl_opportunity by sync_appointments_and_derive without touching
# updated_at_ghl, so those changes are invisible to the query above. Catch them
# from the appointment side instead: a freshly-synced appointment moves the
# Sales metrics of whatever webinar its contact's opportunity belongs to.
_TOUCHED_APPOINTMENT_OPPS_SQL = """
SELECT DISTINCT o.webinar_source_number
FROM ghl_appointment a
JOIN ghl_opportunity o ON o.ghl_contact_id = a.ghl_contact_id
WHERE a.synced_at >= :since AND o.webinar_source_number IS NOT NULL
"""


async def affected_webinar_ids_since(since) -> list[str] | None:
    """Webinar ids whose statistics could have moved since `since`.

    A webinar is affected when a contact or opportunity touched by the sync
    attributes to it — either by number (an e{N} token in the series history,
    a booked_call_webinar_series, an opportunity's webinar_source_number) or by
    falling inside its (prev_date, current_date] window (self-registration and
    unsubscribe are date-driven, not number-tagged).

    Returns None when the set can't be bounded — too many touched rows, or a
    derivation query failed. The caller must then fall back to a full recompute
    rather than assume "nothing changed"; an empty list means genuinely nothing.
    """
    import bisect

    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                sa_text(_TOUCHED_CONTACTS_SQL),
                {"since": since, "cap": _SCOPE_ROW_LIMIT + 1},
            )).mappings().one()

            if row["n"] > _SCOPE_ROW_LIMIT:
                logger.info(
                    "recompute scope: %d+ touched contacts — falling back to full rebuild",
                    _SCOPE_ROW_LIMIT,
                )
                return None

            opp_numbers = (await db.execute(
                sa_text(_TOUCHED_OPPS_SQL), {"since": since},
            )).scalars().all()
            appt_numbers = (await db.execute(
                sa_text(_TOUCHED_APPOINTMENT_OPPS_SQL), {"since": since},
            )).scalars().all()

        numbers: set[int] = set()
        for group in (row["token_numbers"], row["booked_numbers"], opp_numbers, appt_numbers):
            numbers.update(int(n) for n in (group or []) if n is not None)

        from services.ghl_statistics_source import (
            _get_cached_webinars,
            GoHighLevelStatisticsSource,
        )
        webinars = await _get_cached_webinars()
        windows = GoHighLevelStatisticsSource._date_windows(webinars)

        # Sorted touched dates + bisect, so the date-window test is exact rather
        # than a min/max range. A range would be useless here: touched form
        # dates span years, so "does the window overlap [min, max]" matches
        # nearly every webinar and scoping degenerates to a full rebuild.
        touched_dates = sorted(d for d in (row["touched_dates"] or []) if d is not None)

        def _window_has_touched_date(prev_date, current_date) -> bool:
            """Any touched date in (prev_date, current_date] — the same
            half-open window the self-reg and unsubscribe metrics use."""
            i = bisect.bisect_right(touched_dates, prev_date)
            return i < len(touched_dates) and touched_dates[i] <= current_date

        ids: set[str] = set()
        for w in webinars:
            if w.number in numbers:
                ids.add(w.id)
                continue
            prev_date, current_date = windows.get(w.id, (None, None))
            if prev_date is None or current_date is None or not touched_dates:
                continue
            if _window_has_touched_date(prev_date, current_date):
                ids.add(w.id)

        return sorted(ids)
    except Exception as exc:
        logger.warning("recompute scope: derivation failed (%s) — full rebuild", exc)
        return None


def schedule_recompute_since(since, source: str = "auto") -> None:
    """Recompute only the webinars a sync since `since` could have moved.

    Falls back to a full recompute whenever the affected set can't be derived,
    so a scoping bug degrades to "slow but correct", never "fast but stale".
    """
    async def _run():
        ids = await affected_webinar_ids_since(since)
        if ids is None:
            await recompute(None, source=source)
        elif ids:
            logger.info("recompute scope: %d webinar(s) affected since %s", len(ids), since)
            await recompute(ids, source=source)
        else:
            logger.info("recompute scope: no webinars affected since %s — skipping", since)

    _spawn(f"since:{_snap_source(source)}:{since}", _run)
