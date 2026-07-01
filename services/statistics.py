"""
Statistics service — loads workbook fixture, computes derived metrics, aggregates parents.

v1 uses a static JSON fixture (WorkbookMockStatisticsSource).
Later GoHighLevel integration swaps only the source behind the same interface.
"""
from __future__ import annotations

import json
import time
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Source adapter protocol
# ---------------------------------------------------------------------------

class StatisticsSource(Protocol):
    async def get_raw_webinars(self) -> list[dict[str, Any]]: ...


class WorkbookMockStatisticsSource:
    """Loads from api/data/statistics_workbook_snapshot.json (cached in memory)."""

    _cache: list[dict[str, Any]] | None = None

    async def get_raw_webinars(self) -> list[dict[str, Any]]:
        if self._cache is None:
            fixture_path = (
                Path(__file__).resolve().parent.parent
                / "api"
                / "data"
                / "statistics_workbook_snapshot.json"
            )
            with open(fixture_path) as f:
                data = json.load(f)
            self._cache = data["webinars"]
        return self._cache


# ---------------------------------------------------------------------------
# Derived metric computation
# ---------------------------------------------------------------------------

def _safe_div(a: float | None, b: float | None) -> float | None:
    """a / b, returning None on null inputs or zero denominator."""
    if a is None or b is None or b == 0:
        return None
    return a / b


def _safe_per1k(a: float | None, b: float | None) -> float | None:
    """a / (b / 1000), returning None on null inputs or zero denominator."""
    if a is None or b is None or b == 0:
        return None
    return a / (b / 1000)


def compute_derived_metrics(
    m: dict[str, float | None],
) -> tuple[dict[str, float | None], bool]:
    """Compute all derived fields from raw metrics. Zero-division → None.

    Rate-metric denominator is `actuallyUsed` (live count of contacts marked
    sent) so released contacts are excluded. Falls back to `invited` (planned
    volume) when `actuallyUsed` is None or 0 — covers historical webinars
    where contacts were never explicitly marked used and synthetic rows
    (nonjoiners, no-list-data) that have no Planning attribution. The second
    return value is True iff the fallback was used, so the UI can flag the
    row.
    """
    actually_used = m.get("actuallyUsed")
    planned_invited = m.get("invited")
    used_fallback = actually_used is None or actually_used == 0
    inv = planned_invited if used_fallback else actually_used

    derived: dict[str, float | None] = {
        # Pass through all raw fields
        **m,
        # Delivery
        "unsubPercent": _safe_div(m.get("unsubscribes"), inv),
        # Yes
        "yesPer1kInv": _safe_per1k(m.get("yesMarked"), inv),
        "yesPercent": _safe_div(m.get("yesMarked"), inv),
        "yesAttendPercent": _safe_div(m.get("yesAttended"), m.get("yesMarked")),
        "yesStay10MinPercent": _safe_div(m.get("yes10MinPlus"), m.get("yesAttended")),
        "yesAttendBySmsClickPercent": _safe_div(
            m.get("yesAttendBySmsClick"), m.get("yesAttended")
        ),
        "yesBookingsPer1kInv": _safe_per1k(m.get("yesBookings"), inv),
        # Maybe
        "maybePer1kInv": _safe_per1k(m.get("maybeMarked"), inv),
        "maybeAttendPercent": _safe_div(m.get("maybeAttended"), m.get("maybeMarked")),
        "maybeStay10MinPercent": _safe_div(
            m.get("maybe10MinPlus"), m.get("maybeAttended")
        ),
        "maybeAttendBySmsClickPercent": _safe_div(
            m.get("maybeAttendBySmsClick"), m.get("maybeAttended")
        ),
        "maybeBookingsPer1kInv": _safe_per1k(m.get("maybeBookings"), inv),
        # Self Reg
        "selfRegPer1kInv": _safe_per1k(m.get("selfRegMarked"), inv),
        "selfRegAttendPercent": _safe_div(
            m.get("selfRegAttended"), m.get("selfRegMarked")
        ),
        "selfRegStay10MinPercent": _safe_div(
            m.get("selfReg10MinPlus"), m.get("selfRegAttended")
        ),
        "selfRegBookingsPer1kInv": _safe_per1k(m.get("selfRegBookings"), inv),
        # Attendance
        "invitedToRegPercent": _safe_div(m.get("totalRegs"), inv),
        "totalRegsPer1kInv": _safe_per1k(m.get("totalRegs"), inv),
        "regToAttendPercent": _safe_div(
            m.get("totalAttended"), m.get("totalRegs")
        ),
        "invitedToAttendPercent": _safe_div(m.get("totalAttended"), inv),
        "totalAttendedPer1kInv": _safe_per1k(m.get("totalAttended"), inv),
        "attendBySmsReminderPercent": _safe_div(
            m.get("attendBySmsReminder"), m.get("totalAttended")
        ),
        "total10MinPlusPer1kInv": _safe_per1k(m.get("total10MinPlus"), inv),
        "attend10MinPercent": _safe_div(
            m.get("total10MinPlus"), m.get("totalAttended")
        ),
        "total30MinPlusPer1kInv": _safe_per1k(m.get("total30MinPlus"), inv),
        "attend30MinPercent": _safe_div(
            m.get("total30MinPlus"), m.get("totalAttended")
        ),
        # Sales
        "bookingsPerAttended": _safe_div(
            m.get("totalBookings"), m.get("totalAttended")
        ),
        "bookingsPerPast10Min": _safe_div(
            m.get("totalBookings"), m.get("total10MinPlus")
        ),
        "totalBookingsPer1kInv": _safe_per1k(m.get("totalBookings"), inv),
        "showPercent": _safe_div(m.get("shows"), m.get("totalBookings")),
        "closeRatePercent": _safe_div(m.get("won"), m.get("shows")),
        "qualPercent": _safe_div(m.get("qualified"), m.get("shows")),
    }
    return derived, used_fallback


# ---------------------------------------------------------------------------
# Parent aggregation
# ---------------------------------------------------------------------------

# Keys that should be summed across children
_SUM_KEYS = [
    "accountsNeeded",
    "invited", "actuallyUsed", "unsubscribes", "lpRegs",
    "yesMarked", "yesAttended", "yes10MinPlus", "yesAttendBySmsClick", "yesBookings",
    "maybeMarked", "maybeAttended", "maybe10MinPlus", "maybeAttendBySmsClick", "maybeBookings",
    "selfRegMarked", "selfRegAttended", "selfReg10MinPlus", "selfRegBookings",
    "totalRegs", "totalAttended", "attendBySmsReminder",
    "total10MinPlus", "total30MinPlus", "totalBookings",
    "totalCallsDatePassed", "confirmed", "shows", "noShows", "canceled",
    "won", "disqualified", "qualified",
    "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
]


def _sum_or_none(values: list[float | None]) -> float | None:
    """Sum non-None values. Returns None if all inputs are None."""
    nums = [v for v in values if v is not None]
    return sum(nums) if nums else None


def _avg_or_none(values: list[float | None]) -> float | None:
    """Average non-None values. Returns None if all inputs are None."""
    nums = [v for v in values if v is not None]
    return sum(nums) / len(nums) if nums else None


def aggregate_parent_summary(
    child_metrics_list: list[dict[str, float | None]],
) -> dict[str, float | None]:
    """
    Aggregate child raw metrics into a parent summary.

    Rules:
    - Most raw metrics: SUM across all children (including Nonjoiners + NO LIST DATA)
    - avgProjectedDealSize: AVERAGE of non-null child values
    - avgClosedDealValue: SUM of non-null child values
    - accountsNeeded: SUM (source-fed, not recomputed)
    """
    if not child_metrics_list:
        return {}

    agg: dict[str, float | None] = {}

    # Sum keys
    for key in _SUM_KEYS:
        agg[key] = _sum_or_none([m.get(key) for m in child_metrics_list])

    # Special aggregation rules
    agg["avgProjectedDealSize"] = _avg_or_none(
        [m.get("avgProjectedDealSize") for m in child_metrics_list]
    )
    agg["avgClosedDealValue"] = _sum_or_none(
        [m.get("avgClosedDealValue") for m in child_metrics_list]
    )

    return agg


# ---------------------------------------------------------------------------
# Segment name builder
# ---------------------------------------------------------------------------

def _build_segment_name(row: dict[str, Any]) -> str | None:
    """
    segmentName = format(createdDate, 'yyyy mmm dd') + ', ' + industry +
                  ', ' + employeeRange + ' employees, ' + country
    Returns None if any input is missing.
    """
    created = row.get("createdDate")
    industry = row.get("industry")
    emp_range = row.get("employeeRange")
    country = row.get("country")

    if not all([created, industry, emp_range, country]):
        return None

    try:
        dt = datetime.strptime(created, "%Y-%m-%d")
        date_str = dt.strftime("%Y %b %d")
    except (ValueError, TypeError):
        return None

    return f"{date_str}, {industry}, {emp_range} employees, {country}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_workbook_source: StatisticsSource = WorkbookMockStatisticsSource()


def _get_source(use_ghl: bool) -> StatisticsSource:
    if use_ghl:
        # Imported lazily so the workbook source still works if GHL deps missing
        from services.ghl_statistics_source import GoHighLevelStatisticsSource
        return GoHighLevelStatisticsSource()
    return _workbook_source


async def _has_ghl_data() -> bool:
    """Return True if at least one completed GHL sync has landed data in the DB."""
    try:
        from sqlalchemy import func, select
        from db.models import GHLSyncRun
        from db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(func.count(GHLSyncRun.id)).where(GHLSyncRun.status == "completed")
            )
            return int(r.scalar() or 0) > 0
    except Exception:
        return False


def _process_raw_webinar(w: dict[str, Any], source_label: str) -> dict[str, Any]:
    """Apply derived-metric computation to a single raw webinar dict."""
    processed_rows: list[dict[str, Any]] = []
    raw_metrics_for_agg: list[dict[str, float | None]] = []

    for row in w["rows"]:
        raw_m = row["metrics"]
        raw_metrics_for_agg.append(raw_m)
        derived, row_fallback = compute_derived_metrics(raw_m)
        processed_rows.append(
            {
                **{k: v for k, v in row.items() if k != "metrics"},
                "metrics": derived,
                "usedFallback": row_fallback,
                "segmentName": _build_segment_name(row),
            }
        )

    if "summary" in w:
        summary, summary_fallback = compute_derived_metrics(w["summary"])
    else:
        agg_raw = aggregate_parent_summary(raw_metrics_for_agg)
        summary, summary_fallback = compute_derived_metrics(agg_raw)

    # Variant-aware synthetic id + identity fields — must match the lightweight
    # list (get_raw_webinar_list) so the frontend's progressive-load merge
    # (replace summary row by id) keeps webinarId/variantLabel and drill-downs
    # can pass webinar_id (A/B variants share a number, so number alone is
    # ambiguous and 500s the contacts endpoint).
    variant_label = w.get("variantLabel")
    syn_id = f"stat-w{w['number']}" + (f"-{variant_label}" if variant_label else "")
    return {
        "id": syn_id,
        "webinarId": w.get("webinarId"),
        "number": w["number"],
        "variantLabel": variant_label,
        "hasSiblingVariants": w.get("hasSiblingVariants", False),
        "date": w.get("date"),
        "title": w.get("title"),
        "workbookRow": w.get("workbookRow", 0),
        "source": source_label,
        "summary": summary,
        "usedFallback": summary_fallback,
        "rows": [
            {
                "id": f"{syn_id}-r{r.get('workbookRow', i)}",
                "webinarNumber": w["number"],
                **r,
            }
            for i, r in enumerate(processed_rows)
        ],
    }


async def get_statistics_webinars(source: str = "auto") -> list[dict[str, Any]]:
    """Return fully processed statistics webinars with derived metrics.

    source: "auto" (default = DB-backed: Planning + WebinarGeek + synced GHL),
            "workbook" (dev-only legacy fixture).
    """
    use_ghl = source != "workbook"
    src = _get_source(use_ghl)
    raw_webinars = await src.get_raw_webinars()
    source_label = "ghl" if use_ghl else "workbook_mock"
    return [_process_raw_webinar(w, source_label) for w in raw_webinars]


async def get_statistics_webinar_list(source: str = "auto") -> list[dict[str, Any]]:
    """Lightweight identity-only list (no metrics). Powers progressive load."""
    use_ghl = source != "workbook"
    if use_ghl:
        from services.ghl_statistics_source import GoHighLevelStatisticsSource
        src = GoHighLevelStatisticsSource()
        if not hasattr(src, "get_raw_webinar_list"):
            # Defensive: should always exist; fall back to full list if not.
            full = await src.get_raw_webinars()
            return [
                {"id": f"stat-w{w['number']}", "number": w["number"], "date": w.get("date"),
                 "title": w.get("title"), "status": w.get("status"),
                 "listCount": sum(1 for r in w.get("rows", []) if r.get("kind") == "list")}
                for w in full
            ]
        return await src.get_raw_webinar_list()
    # Workbook source — derive from the cached fixture.
    raw = await _workbook_source.get_raw_webinars()
    return [
        {
            "id": f"stat-w{w['number']}",
            "number": w["number"],
            "date": w.get("date"),
            "title": w.get("title"),
            "status": (w.get("rows") or [{}])[0].get("status"),
            "listCount": sum(1 for r in w.get("rows", []) if r.get("kind") == "list"),
        }
        for w in raw
    ]


# ---------------------------------------------------------------------------
# Per-webinar response cache
# ---------------------------------------------------------------------------
# The per-webinar fetch is dominated by a hash join between contacts (1M rows)
# and ghl_contact (720k rows) on LOWER(email), which costs ~25-30s end-to-end.
# The result only changes when a sync runs, so caching the assembled response
# for a few minutes turns repeat visits / page refreshes / per-row retries from
# 30s into a memory lookup. invalidate_stats_cache() is called from
# run_webinar_sync after it finishes upserting fresh contact/opportunity data.
#
# Caveat: process-local. Render typically runs one uvicorn worker per service,
# so this is fine; if you ever scale to multiple workers, hits on a different
# worker won't benefit until that worker's first compute populates its own
# cache. Multi-worker invalidation is a TODO if it ever comes up.

_STATS_CACHE_TTL_SECONDS = 600.0  # 10 minutes
_stats_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def invalidate_stats_cache() -> None:
    """Drop every cached per-webinar response. Call after a sync run finishes
    so the next read sees the new numbers instead of waiting for the TTL.
    No-op if the cache is already empty."""
    _stats_cache.clear()


async def get_statistics_webinar_one(
    source: str,
    webinar_id: str,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Fully-processed single webinar by webinar_id, or None if missing.

    Variant-aware: each A/B variant has its own UUID, so callers can
    address them unambiguously.

    Read order (unless force): in-memory cache → persisted snapshot → live
    compute. The snapshot store (populated by services.statistics_snapshot
    .recompute() after any source change) is the steady-state path — an
    instant DB read instead of the ~30s contacts↔ghl_contact join. `force`
    skips both caches and recomputes live; the recompute job uses it to
    rebuild snapshots from fresh data.

    `None` results (unknown webinar_id) are not cached — they're cheap to
    recompute and we don't want a typo to be remembered for 10 minutes.
    """
    cache_key = (source, webinar_id)
    if not force:
        cached = _stats_cache.get(cache_key)
        if cached is not None and (time.monotonic() - cached[0]) < _STATS_CACHE_TTL_SECONDS:
            return cached[1]
        # Persisted snapshot — instant, the steady-state path. Populate the
        # in-memory cache so repeat hits in this worker skip the DB round-trip.
        try:
            from services import statistics_snapshot as snap
            payload = await snap.read_snapshot_payload(source, webinar_id)
        except Exception:
            payload = None
        if payload is not None:
            _stats_cache[cache_key] = (time.monotonic(), payload)
            return payload

    use_ghl = source != "workbook"
    source_label = "ghl" if use_ghl else "workbook_mock"
    result: dict[str, Any] | None = None
    if use_ghl:
        from services.ghl_statistics_source import GoHighLevelStatisticsSource
        src = GoHighLevelStatisticsSource()
        raw = await src.get_raw_webinar(webinar_id)
        if raw:
            result = _process_raw_webinar(raw, source_label)
    else:
        # Workbook source predates variants — single row per number, so the
        # synthetic webinar id encodes only the number.
        raw_all = await _workbook_source.get_raw_webinars()
        for w in raw_all:
            if f"stat-w{w['number']}" == webinar_id:
                result = _process_raw_webinar(w, source_label)
                break

    if result is not None:
        _stats_cache[cache_key] = (time.monotonic(), result)
    return result


# ---------------------------------------------------------------------------
# By-bucket funnel aggregation (Segments tab)
# ---------------------------------------------------------------------------
# Raw per-list metric keys summed into each bucket row. Percentages are
# recomputed from these sums (never averaged) by the caller / frontend.
_FUNNEL_RAW_KEYS = ("invited", "totalRegs", "totalAttended", "total10MinPlus", "totalBookings")


def _is_passed_webinar(date_str: str | None, status: str | None) -> bool:
    """Mirror the Statistics page's "passed" filter: webinar date < today, or
    date == today AND status == 'sent'. Webinar.date serializes as YYYY-MM-DD,
    so lexicographic compare matches chronological order."""
    if not date_str:
        return False
    today = _date.today().isoformat()
    if date_str < today:
        return True
    if date_str > today:
        return False
    return (status or "").lower() == "sent"


def _segment_webinar_label(s: dict[str, Any]) -> str:
    base = f"W{s.get('number')}"
    vl = s.get("variantLabel")
    if vl:
        base += f" · {vl}"
    d = s.get("date")
    if d:
        base += f" ({d})"
    return base


async def get_statistics_segments(
    source: str = "auto",
    webinar_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate per-webinar list rows into a by-bucket funnel across the
    selected webinars (default: all passed webinars).

    Each bucket row sums the raw per-list metrics of every assignment of that
    bucket on every included webinar. Rows with no bucket (Nonjoiners / No
    List Data / Self-Reg synthetics) roll up into a single "Other (no bucket)"
    row so the totals line ties out to the real webinar totals. Only the raw
    counts are returned; the frontend recomputes the funnel percentages from
    the summed counts (never averaging per-webinar percentages).

    Reuses the cached per-webinar compute (get_statistics_webinar_one) so it
    shares the per-webinar response cache with the main Statistics page.
    """
    summaries = await get_statistics_webinar_list(source=source)
    passed = [s for s in summaries if _is_passed_webinar(s.get("date"), s.get("status"))]
    # Newest first, sibling variants grouped (matches the main page ordering).
    passed.sort(key=lambda s: (s.get("date") or "", s.get("variantLabel") or ""), reverse=True)

    webinar_options = [
        {
            "webinarId": s.get("webinarId"),
            "number": s.get("number"),
            "variantLabel": s.get("variantLabel"),
            "date": s.get("date"),
            "title": s.get("title"),
            "label": _segment_webinar_label(s),
        }
        for s in passed
        if s.get("webinarId")
    ]

    all_ids = [o["webinarId"] for o in webinar_options]
    if webinar_ids:
        wanted = set(webinar_ids)
        target_ids = [i for i in all_ids if i in wanted]
    else:
        target_ids = all_ids

    # bucket_id (None = "Other") -> running raw sums.
    agg: dict[str | None, dict[str, Any]] = {}

    def _accumulate(bucket_id: str | None, bucket_name: str | None, metrics: dict[str, Any]) -> None:
        slot = agg.get(bucket_id)
        if slot is None:
            slot = {"bucketId": bucket_id, "bucketName": bucket_name}
            slot.update({k: 0.0 for k in _FUNNEL_RAW_KEYS})
            agg[bucket_id] = slot
        elif bucket_name and not slot.get("bucketName"):
            slot["bucketName"] = bucket_name
        for k in _FUNNEL_RAW_KEYS:
            v = metrics.get(k)
            if v is not None:
                slot[k] += float(v)

    # Aggregate from the snapshot store only — one query for all payloads. We
    # deliberately do NOT live-compute missing webinars here: that would be the
    # ~30s × N single-request hang this store exists to avoid (and would trip
    # the edge timeout). Any target webinar without a snapshot yet is reported
    # as "pending" so the UI can prompt a recompute instead of silently
    # undercounting the totals.
    try:
        from services import statistics_snapshot as snap
        payloads = await snap.read_all_payloads(source)
    except Exception:
        payloads = {}

    pending_ids: list[str] = []
    for wid in target_ids:
        webinar = payloads.get(wid)
        if not webinar:
            pending_ids.append(wid)
            continue
        for row in webinar.get("rows", []):
            _accumulate(row.get("bucketId"), row.get("bucketName"), row.get("metrics") or {})

    # Manual quality labels (good/medium/bad) for the named buckets present, so
    # the Segments dashboard can display + edit them. Cheap point lookup by id;
    # the ids come from the user's own snapshots, so no extra user scoping is
    # needed. None for the "Other"/Total rows and any unmarked bucket.
    quality_by_bucket: dict[str, str | None] = {}
    bucket_ids = [k for k in agg if k is not None]
    if bucket_ids:
        from sqlalchemy import select
        from db.session import AsyncSessionLocal
        from db.models import OutreachBucket
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(OutreachBucket.id, OutreachBucket.quality).where(
                    OutreachBucket.id.in_(bucket_ids)
                )
            )
            quality_by_bucket = {row[0]: row[1] for row in res.all()}

    def _shape(slot: dict[str, Any]) -> dict[str, Any]:
        return {
            "bucketId": slot["bucketId"],
            "bucketName": slot.get("bucketName"),
            "quality": quality_by_bucket.get(slot["bucketId"]),
            "invites": int(slot["invited"] or 0),
            "regs": int(slot["totalRegs"] or 0),
            "attendees10m": int(slot["total10MinPlus"] or 0),
            "bookings": int(slot["totalBookings"] or 0),
        }

    # Named buckets first (by invites desc), then the "Other (no bucket)" row.
    named = sorted(
        (v for k, v in agg.items() if k is not None),
        key=lambda r: (r.get("invited") or 0.0),
        reverse=True,
    )
    segments = [_shape(v) for v in named]
    other = agg.get(None)
    if other is not None:
        other["bucketName"] = other.get("bucketName") or "Other (no bucket)"
        segments.append(_shape(other))

    totals = {
        "bucketId": None,
        "bucketName": "Total",
        "invites": sum(s["invites"] for s in segments),
        "regs": sum(s["regs"] for s in segments),
        "attendees10m": sum(s["attendees10m"] for s in segments),
        "bookings": sum(s["bookings"] for s in segments),
    }

    return {
        "webinars": webinar_options,
        "includedWebinarIds": target_ids,
        "pendingWebinarIds": pending_ids,
        "segments": segments,
        "totals": totals,
    }
