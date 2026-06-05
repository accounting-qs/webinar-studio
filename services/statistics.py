"""
Statistics service — loads workbook fixture, computes derived metrics, aggregates parents.

v1 uses a static JSON fixture (WorkbookMockStatisticsSource).
Later GoHighLevel integration swaps only the source behind the same interface.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
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
) -> dict[str, Any] | None:
    """Fully-processed single webinar by webinar_id, or None if missing.

    Variant-aware: each A/B variant has its own UUID, so callers can
    address them unambiguously.

    Cached: hits return immediately; misses compute and populate the cache.
    `None` results (unknown webinar_id) are not cached — they're cheap to
    recompute and we don't want a typo to be remembered for 10 minutes.
    """
    cache_key = (source, webinar_id)
    cached = _stats_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _STATS_CACHE_TTL_SECONDS:
        return cached[1]

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
