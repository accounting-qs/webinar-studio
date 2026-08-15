"""APScheduler wiring for recurring GHL syncs.

Reads schedule from ghl_sync_settings (singleton row, id=1). Exposes
start/stop helpers + a reload_schedules() hook called when settings change
via PATCH /ghl-sync/settings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from services.ghl_sync import (
    get_sync_settings,
    recover_orphaned_runs,
    run_opportunities_sync,
    run_sync,
    sweep_stale_runs,
)

logger = logging.getLogger(__name__)

INCREMENTAL_JOB_ID = "ghl_incremental_sync"
WEEKLY_JOB_ID = "ghl_weekly_full_sync"
DAILY_SALES_JOB_ID = "ghl_daily_sales_sync"
STALE_SWEEPER_JOB_ID = "ghl_stale_sweeper"
WEEKLY_REPORT_JOB_ID = "weekly_report_send"
# Report prep runs 15 minutes before the send so the per-webinar report
# artifact (2–4 min of heavy SQL + AI insights) is ready when the email goes out.
WEEKLY_REPORT_PREP_JOB_ID = "weekly_report_prep"
WEEKLY_REPORT_PREP_LEAD_MINUTES = 15
# Durable report-request sweep: retries per-webinar report generations whose
# in-process task was killed by a deploy/restart (see webinar_report_request).
REPORT_SWEEP_JOB_ID = "webinar_report_sweep"
REPORT_SWEEP_INTERVAL_MINUTES = 2

# How often to scan for sync runs with stale heartbeats and reap them.
# Cheap query (one indexed scan over status='running') so 2 minutes is fine.
STALE_SWEEP_INTERVAL_MINUTES = 2

# WebinarGeek broadcast auto-sync: scan for planned webinars whose linked
# broadcast started >=2h ago and sync their subscribers once. Cheap partial-
# index scan; 15 min keeps the fire reasonably close to the 2h mark.
WG_AUTO_SYNC_JOB_ID = "wg_auto_sync"
WG_AUTO_SYNC_INTERVAL_MINUTES = 15

# Correctness backstop for the sync-scoped snapshot recompute. Syncs rebuild
# only the webinars their rows attribute to (see
# statistics_snapshot.schedule_recompute_since); this rebuilds everything once
# a night so anything that attribution misses self-heals within 24h rather
# than serving a stale number indefinitely. 03:00 UTC — off-peak for a
# US-Central audience, and clear of the Wed 14:00 Chicago report window.
SNAPSHOT_FULL_REBUILD_JOB_ID = "statistics_snapshot_full_rebuild"
SNAPSHOT_FULL_REBUILD_HOUR_UTC = 3

# Fixed anchor for interval-based jobs.
#
# IntervalTrigger with no start_date defaults to "now + interval", and jobs are
# re-registered by _apply_settings() on every process start. With a 24h
# interval that means each restart pushes the next run a full day out, so a
# service that restarts more than once a day never syncs at all — which is
# exactly what happened: the incremental sync last completed 2026-08-11 20:16
# and did not fire again across ~3 days of OOM restarts and deploys, while the
# Aug 13 opportunities run died mid-flight ("orphaned / process_restart_or_crash").
#
# Anchoring to a fixed past instant makes fire times a pure function of the
# wall clock (anchor + k*interval), so restarts cannot defer them.
_INTERVAL_ANCHOR = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Generous grace so a run that came due while the process was down still
# executes once it returns, instead of being silently skipped. The 60s default
# meant any restart near a scheduled fire dropped that run entirely.
INTERVAL_MISFIRE_GRACE_SECONDS = 3600

_scheduler: AsyncIOScheduler | None = None


async def _incremental_job() -> None:
    try:
        await run_sync("incremental", trigger="scheduled")
    except Exception as exc:
        logger.error("Scheduled incremental sync failed: %s", exc)


async def _weekly_job() -> None:
    try:
        await run_sync("full", trigger="scheduled")
    except Exception as exc:
        logger.error("Scheduled weekly full sync failed: %s", exc)


async def _daily_sales_job() -> None:
    try:
        await run_opportunities_sync(trigger="scheduled")
    except Exception as exc:
        logger.error("Scheduled daily sales sync failed: %s", exc)


async def _stale_sweeper_job() -> None:
    try:
        await sweep_stale_runs()
    except Exception as exc:
        logger.error("Stale sync sweeper failed: %s", exc)


async def _snapshot_full_rebuild_job() -> None:
    try:
        # Lazy import — keeps the scheduler free of a statistics import cycle.
        from services.statistics_snapshot import recompute
        result = await recompute(None)
        logger.info(
            "Nightly snapshot rebuild finished: %s done, %s error(s)",
            result.get("done"), result.get("errors"),
        )
    except Exception as exc:
        logger.error("Nightly snapshot rebuild failed: %s", exc)


async def _weekly_report_job() -> None:
    try:
        # Lazy import — avoids a scheduler ↔ report-service import cycle.
        from services.weekly_report import send_weekly_report
        result = await send_weekly_report()
        if not result.get("ok"):
            logger.warning("Scheduled weekly report skipped/failed: %s", result.get("error"))
    except Exception as exc:
        logger.error("Scheduled weekly report failed: %s", exc)


async def _weekly_report_prep_job() -> None:
    try:
        # Lazy import — avoids a scheduler ↔ report-service import cycle.
        from services import webinar_report
        wid = await webinar_report.resolve_latest_passed_webinar_id()
        if not wid:
            logger.info("Weekly report prep: no passed webinar to report on")
            return
        result = await webinar_report.generate_report(wid)
        if result is None:
            logger.warning("Weekly report prep: generation failed for %s", wid)
        else:
            logger.info("Weekly report prep: report ready for %s", wid)
    except Exception as exc:
        logger.error("Weekly report prep failed: %s", exc)


async def _wg_auto_sync_job() -> None:
    try:
        from services import wg_sync
        n = await wg_sync.run_due_broadcast_autosyncs()
        if n:
            logger.info("WG broadcast auto-sync: synced %d due broadcast(s)", n)
    except Exception as exc:
        logger.error("WG broadcast auto-sync failed: %s", exc)


async def _report_sweep_job() -> None:
    try:
        # Lazy import — avoids a scheduler ↔ report-service import cycle.
        from services import webinar_report
        n = await webinar_report.run_pending_requests()
        if n:
            logger.info("Report sweep: generated %d pending report(s)", n)
    except Exception as exc:
        logger.error("Report sweep failed: %s", exc)


async def start() -> AsyncIOScheduler:
    """Start the scheduler and register jobs from current DB settings.

    Also runs a one-shot orphan recovery so any 'running' rows left over
    from the previous process (deploy, crash) are marked failed before the
    UI sees them.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    try:
        recovered = await recover_orphaned_runs()
        if recovered:
            logger.warning("Startup recovery: marked %d orphaned sync run(s) as failed", recovered)
    except Exception as exc:
        logger.error("Startup orphan recovery failed: %s", exc)

    _scheduler = AsyncIOScheduler(timezone="UTC")
    await _apply_settings(_scheduler)
    _scheduler.start()
    logger.info("GHL scheduler started")
    return _scheduler


async def stop() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("GHL scheduler stopped")
    _scheduler = None


async def reload_schedules() -> None:
    """Re-read settings and re-register jobs. Called after settings change."""
    global _scheduler
    if _scheduler is None or not _scheduler.running:
        await start()
        return
    await _apply_settings(_scheduler)
    logger.info("GHL scheduler reloaded")


async def _apply_settings(scheduler: AsyncIOScheduler) -> None:
    """Remove existing GHL jobs and re-add based on current settings."""
    for job_id in (INCREMENTAL_JOB_ID, WEEKLY_JOB_ID, DAILY_SALES_JOB_ID, STALE_SWEEPER_JOB_ID, WG_AUTO_SYNC_JOB_ID, WEEKLY_REPORT_JOB_ID, WEEKLY_REPORT_PREP_JOB_ID, SNAPSHOT_FULL_REBUILD_JOB_ID, REPORT_SWEEP_JOB_ID):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    # Stale-run sweeper is unconditional — keeps the sync_run table honest
    # even if all scheduled syncs are disabled.
    scheduler.add_job(
        _stale_sweeper_job,
        trigger=IntervalTrigger(minutes=STALE_SWEEP_INTERVAL_MINUTES),
        id=STALE_SWEEPER_JOB_ID,
        max_instances=1,
        misfire_grace_time=60,
        replace_existing=True,
    )

    # WebinarGeek broadcast auto-sync is unconditional too — it self-gates on
    # broadcast start time + the one-shot stamp, so it's a no-op when nothing
    # is due.
    scheduler.add_job(
        _wg_auto_sync_job,
        trigger=IntervalTrigger(minutes=WG_AUTO_SYNC_INTERVAL_MINUTES),
        id=WG_AUTO_SYNC_JOB_ID,
        max_instances=1,
        misfire_grace_time=300,
        replace_existing=True,
    )

    # Report-request sweep is unconditional — cheap query on an almost-always-
    # empty table; picks up per-webinar report generations that a deploy or
    # restart killed mid-run.
    scheduler.add_job(
        _report_sweep_job,
        trigger=IntervalTrigger(minutes=REPORT_SWEEP_INTERVAL_MINUTES),
        id=REPORT_SWEEP_JOB_ID,
        max_instances=1,
        misfire_grace_time=60,
        replace_existing=True,
    )

    # Nightly full snapshot rebuild — unconditional, and the backstop that lets
    # the post-sync recompute stay scoped. misfire_grace_time is generous: if
    # the process was down at 03:00 we still want the rebuild once it returns.
    scheduler.add_job(
        _snapshot_full_rebuild_job,
        trigger=CronTrigger(hour=SNAPSHOT_FULL_REBUILD_HOUR_UTC, minute=0, timezone="UTC"),
        id=SNAPSHOT_FULL_REBUILD_JOB_ID,
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # Weekly report job — separate settings + its own try/except so a reports
    # failure never blocks the GHL sync jobs (and vice versa).
    try:
        from services.weekly_report import get_report_settings
        rs = await get_report_settings()
        if rs["enabled"]:
            scheduler.add_job(
                _weekly_report_job,
                trigger=CronTrigger(
                    day_of_week=rs["day_of_week"],
                    hour=int(rs["hour_local"]),
                    minute=int(rs["minute_local"]),
                    timezone=rs["timezone"],
                ),
                id=WEEKLY_REPORT_JOB_ID,
                max_instances=1,
                misfire_grace_time=3600,
                replace_existing=True,
            )
            logger.info(
                "Registered weekly report %s %02d:%02d %s",
                rs["day_of_week"], rs["hour_local"], rs["minute_local"], rs["timezone"],
            )

            # Prep job: generate the per-webinar report artifact 15 minutes
            # before the send (borrow across the hour/day boundary as needed).
            prep_minute = int(rs["minute_local"]) - WEEKLY_REPORT_PREP_LEAD_MINUTES
            prep_hour = int(rs["hour_local"])
            prep_day = rs["day_of_week"]
            if prep_minute < 0:
                prep_minute += 60
                prep_hour -= 1
                if prep_hour < 0:
                    prep_hour = 23
                    _days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
                    prep_day = _days[(_days.index(prep_day) - 1) % 7]
            scheduler.add_job(
                _weekly_report_prep_job,
                trigger=CronTrigger(
                    day_of_week=prep_day,
                    hour=prep_hour,
                    minute=prep_minute,
                    timezone=rs["timezone"],
                ),
                id=WEEKLY_REPORT_PREP_JOB_ID,
                max_instances=1,
                misfire_grace_time=3600,
                replace_existing=True,
            )
            logger.info(
                "Registered weekly report prep %s %02d:%02d %s",
                prep_day, prep_hour, prep_minute, rs["timezone"],
            )
    except Exception as exc:
        logger.warning("Could not load report settings (skipping weekly report): %s", exc)

    try:
        s = await get_sync_settings()
    except Exception as exc:
        logger.warning("Could not load sync settings (skipping schedule): %s", exc)
        return

    if s["incremental_enabled"]:
        hours = max(1, int(s["incremental_interval_hours"]))
        scheduler.add_job(
            _incremental_job,
            trigger=IntervalTrigger(hours=hours, start_date=_INTERVAL_ANCHOR),
            id=INCREMENTAL_JOB_ID,
            max_instances=1,
            misfire_grace_time=INTERVAL_MISFIRE_GRACE_SECONDS,
            replace_existing=True,
        )
        logger.info("Registered incremental sync every %dh (anchored)", hours)

    if s["weekly_full_enabled"]:
        scheduler.add_job(
            _weekly_job,
            trigger=CronTrigger(
                day_of_week=s["weekly_full_day_of_week"],
                hour=int(s["weekly_full_hour_local"]),
                minute=0,
                timezone=s["weekly_full_timezone"],
            ),
            id=WEEKLY_JOB_ID,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )
        logger.info(
            "Registered weekly full sync %s %02d:00 %s",
            s["weekly_full_day_of_week"], s["weekly_full_hour_local"], s["weekly_full_timezone"],
        )

    if s["daily_sales_enabled"]:
        scheduler.add_job(
            _daily_sales_job,
            trigger=CronTrigger(
                hour=int(s["daily_sales_hour_local"]),
                minute=0,
                timezone=s["daily_sales_timezone"],
            ),
            id=DAILY_SALES_JOB_ID,
            max_instances=1,
            misfire_grace_time=300,
            replace_existing=True,
        )
        logger.info(
            "Registered daily sales sync %02d:00 %s",
            s["daily_sales_hour_local"], s["daily_sales_timezone"],
        )
