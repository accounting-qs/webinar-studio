"""Per-webinar report generation — the data + AI-insights artifact behind the
Statistics page "Report" button and the weekly email.

A report is a frozen JSONB payload per webinar variant:
  - scorecard: this webinar vs the last-10-webinar average AND the last-4-week
    average (never just vs previous),
  - funnels: invited / reg rate / attendance by industry, geography, employee
    size and segment (bucket), each vs the last-10-webinar baseline,
  - bookings deep-dive: unique booked contacts (webinar_booking_attribution
    deduped by ghl_contact_id — a rebooked contact counts once), call status,
    lead-quality mix, implied close rate, booking origin cohorts, lead sources,
  - non-joiner package using the shared 6-webinar pool definition
    (services/nonjoiners.py),
  - data caveats.

Generation runs in the background (2–4 min of heavy SQL) mirroring
services.statistics_snapshot's recompute pattern: asyncio lock + process-local
status + fire-and-forget scheduling with coalescing. Triggered automatically
15 minutes before the weekly email send (services.ghl_scheduler), on first
view from the report page, or via the manual Regenerate action. The AI
insights step (Claude, REPORT_MODEL) runs after the numbers persist; its
failure never blocks the report (ai_error is stored instead).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from services.nonjoiners import NONJOINER_WINDOW, nonjoiner_pool_emails

logger = logging.getLogger(__name__)

REPORT_MODEL = "claude-opus-5"
# claude-opus-5 thinks by default and max_tokens caps thinking + answer
# together — leave generous headroom or the answer truncates to nothing.
INSIGHTS_MAX_TOKENS = 16000
INSIGHTS_EFFORT = "medium"
INSIGHTS_TIMEOUT_SECONDS = 240.0
# Funnel baselines pool the last N passed webinars before the current one.
BASELINE_WINDOW = 10
# Give up retrying a durably-requested generation after this many failures.
MAX_GENERATION_ATTEMPTS = 5
# Non-joiner pool definition (NONJOINER_WINDOW) is shared with the Statistics
# page — see services/nonjoiners.py.
# Lloyd's close rates by lead quality (implied close rate weighting).
CLOSE_RATES = {"great": 0.25, "ok": 0.13, "barely": 0.05}

_LQ_KEYS = {
    "Great": "great",
    "Ok": "ok",
    "Barely Passable": "barely",
    "Bad / DQ": "bad",
}

# ---------------------------------------------------------------------------
# Status (process-local, same single-worker assumption as statistics_snapshot)
# ---------------------------------------------------------------------------

_gen_lock = asyncio.Lock()
_bg_tasks: set[asyncio.Task] = set()
_pending: set[str] = set()

_status: dict[str, Any] = {
    "running": False,
    "webinar_id": None,
    "phase": None,          # "queries" | "ai"
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}


async def get_status(webinar_id: str) -> dict[str, Any]:
    from sqlalchemy import func as sa_func, select
    from db.models import WebinarReport, WebinarReportRequest
    from db.session import AsyncSessionLocal

    generated_at = None
    typical_ms = None
    queued = False
    queue_error = None
    queue_attempts = 0
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                select(WebinarReport.generated_at).where(WebinarReport.webinar_id == webinar_id)
            )
            row = r.scalar_one_or_none()
            generated_at = row.isoformat() if row else None
            # Typical duration across recent reports → frontend ETA.
            typical_ms = (await db.execute(
                select(sa_func.avg(WebinarReport.generation_ms))
                .where(WebinarReport.generation_ms.isnot(None))
            )).scalar_one_or_none()
            req = (await db.execute(
                select(WebinarReportRequest).where(WebinarReportRequest.webinar_id == webinar_id)
            )).scalar_one_or_none()
            if req is not None:
                queued = req.attempts < MAX_GENERATION_ATTEMPTS
                queue_error = req.last_error
                queue_attempts = req.attempts
    except Exception as exc:  # table missing before migration, etc.
        logger.warning("webinar_report.get_status: read failed: %s", exc)

    running_here = bool(_status["running"] and _status["webinar_id"] == webinar_id)
    pending_here = f"report:{webinar_id}" in _pending
    return {
        "running": running_here or pending_here,
        # Durable request marker: survives restarts; the scheduler sweep picks
        # it up, so "queued but not running" means "will start shortly".
        "queued": queued,
        "phase": _status["phase"] if running_here else None,
        "started_at": _status["started_at"] if running_here else None,
        "finished_at": _status["finished_at"] if _status["webinar_id"] == webinar_id else None,
        "last_error": (
            (_status["last_error"] if _status["webinar_id"] == webinar_id else None)
            or (queue_error if queue_attempts else None)
        ),
        "attempts": queue_attempts,
        "generated_at": generated_at,
        "typical_ms": int(typical_ms) if typical_ms else None,
    }


# ---------------------------------------------------------------------------
# Read / persist
# ---------------------------------------------------------------------------

async def read_report(webinar_id: str) -> dict[str, Any] | None:
    from sqlalchemy import select
    from db.models import WebinarReport
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(WebinarReport).where(WebinarReport.webinar_id == webinar_id)
        )).scalar_one_or_none()
    if row is None:
        return None
    return {
        "webinarId": row.webinar_id,
        "number": row.webinar_number,
        "variantLabel": row.variant_label,
        "payload": row.payload,
        "insights": row.insights,
        "insightsModel": row.insights_model,
        "aiError": row.ai_error,
        "generatedAt": row.generated_at.isoformat() if row.generated_at else None,
        "generationMs": row.generation_ms,
    }


async def _upsert_payload(payload: dict[str, Any], generation_ms: int) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from db.models import WebinarReport
    from db.session import AsyncSessionLocal

    clean = json.loads(json.dumps(payload, default=str))
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        stmt = pg_insert(WebinarReport).values(
            webinar_id=payload["webinarId"],
            webinar_number=payload.get("number"),
            variant_label=payload.get("variantLabel"),
            payload=clean,
            generated_at=now,
            generation_ms=generation_ms,
        )
        # Insights are intentionally NOT touched here: a rebuild that later
        # fails in the AI step keeps the previous insights visible.
        stmt = stmt.on_conflict_do_update(
            index_elements=["webinar_id"],
            set_={
                "webinar_number": stmt.excluded.webinar_number,
                "variant_label": stmt.excluded.variant_label,
                "payload": stmt.excluded.payload,
                "generated_at": stmt.excluded.generated_at,
                "generation_ms": stmt.excluded.generation_ms,
            },
        )
        await db.execute(stmt)
        await db.commit()


async def _update_insights(
    webinar_id: str,
    insights: list[dict[str, Any]] | None,
    ai_error: str | None,
) -> None:
    from sqlalchemy import update
    from db.models import WebinarReport
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(WebinarReport)
            .where(WebinarReport.webinar_id == webinar_id)
            .values(
                insights=insights,
                insights_model=REPORT_MODEL if insights else None,
                ai_error=ai_error,
            )
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_geo(v: str | None) -> str:
    """Collapse list-location artifacts ("'United States']") and US aliases."""
    s = re.sub(r"[\[\]']", "", (v or "").strip())
    if not s or s == "(none)":
        return "(none)"
    if s.lower() in ("united states", "usa", "us"):
        return "United States"
    return s


def _norm_industry(v: str | None) -> str:
    s = (v or "").strip().lower()
    if s in ("", "(none)", "scrape error", "site error", "null", "none", "error"):
        return "(none)"
    return s.replace(" and ", " & ")


def _safe_div(n: float, d: float) -> float | None:
    return (n / d) if d else None


def _rate(n: float, d: float, digits: int = 4) -> float | None:
    v = _safe_div(n, d)
    return round(v, digits) if v is not None else None


def _primary_of(variants: list[dict[str, Any]]) -> dict[str, Any]:
    unlabeled = [v for v in variants if not v.get("variantLabel")]
    if unlabeled:
        return unlabeled[0]
    return sorted(variants, key=lambda v: v.get("variantLabel") or "")[0]


async def resolve_latest_passed_webinar_id() -> str | None:
    """Latest passed webinar's primary-variant id (weekly prep target)."""
    from services import statistics as stats

    summaries = await stats.get_statistics_webinar_list("auto")
    passed = [
        s for s in summaries
        if s.get("webinarId") and stats._is_passed_webinar(s.get("date"), s.get("status"))
    ]
    if not passed:
        return None
    latest = max(passed, key=lambda s: (s.get("date") or "", s.get("number") or 0))
    group = [s for s in passed if s.get("number") == latest.get("number")]
    return _primary_of(group).get("webinarId")


# ---------------------------------------------------------------------------
# Scorecard (statistics_snapshot-backed — no heavy SQL)
# ---------------------------------------------------------------------------

_SCORECARD_SUM_KEYS = (
    "invited", "totalRegs", "netNewRegs", "nonjoinerRegs", "noListDataRegs",
    "yesMarked", "maybeMarked", "totalAttended", "total10MinPlus",
    "uniqueBookers",
)


def _snapshot_counts(payload: dict[str, Any]) -> dict[str, float]:
    """Raw scorecard counts from one statistics snapshot payload."""
    summary = payload.get("summary") or {}
    nj = nld = 0
    nj_att = 0
    for row in payload.get("rows") or []:
        kind = row.get("kind")
        metrics = row.get("metrics") or {}
        if kind == "nonjoiners":
            nj += int(metrics.get("totalRegs") or 0)
            nj_att += int(metrics.get("totalAttended") or 0)
        elif kind == "no_list_data":
            nld += int(metrics.get("totalRegs") or 0)
    total = int(summary.get("totalRegs") or 0)
    return {
        "invited": int(summary.get("invited") or 0),
        "totalRegs": total,
        "netNewRegs": max(total - nj - nld, 0),
        "nonjoinerRegs": nj,
        "nonjoinerAttended": nj_att,
        "noListDataRegs": nld,
        "yesMarked": int(summary.get("yesMarked") or 0),
        "maybeMarked": int(summary.get("maybeMarked") or 0),
        "totalAttended": int(summary.get("totalAttended") or 0),
        "total10MinPlus": int(summary.get("total10MinPlus") or 0),
        "uniqueBookers": int(summary.get("uniqueBookers") or 0),
    }


def _fold_baseline(counts_list: list[dict[str, float]]) -> dict[str, Any] | None:
    """Average scorecard over N webinars: counts averaged, rates from sums."""
    n = len(counts_list)
    if not n:
        return None
    sums = {k: sum(c.get(k, 0) for c in counts_list) for k in _SCORECARD_SUM_KEYS}
    out: dict[str, Any] = {k: round(v / n, 1) for k, v in sums.items()}
    out["webinarCount"] = n
    out["regRate"] = _rate(sums["netNewRegs"], sums["invited"])
    out["attendRateOfRegs"] = _rate(sums["totalAttended"], sums["totalRegs"])
    out["attendPer10kInvited"] = (
        round(sums["totalAttended"] / sums["invited"] * 10000, 1) if sums["invited"] else None
    )
    out["bookingsPerAttended"] = _rate(sums["uniqueBookers"], sums["totalAttended"])
    return out


# ---------------------------------------------------------------------------
# Funnel dimensions (heavy SQL, per-scope transactions)
# ---------------------------------------------------------------------------

_EMP_BUCKETS = (
    (2, "0 - 2"), (5, "3 - 5"), (10, "6 - 10"), (20, "11 - 20"),
    (50, "21 - 50"), (100, "51 - 100"), (200, "101 - 200"),
    (500, "201 - 500"), (1000, "501 - 1000"), (2000, "1001 - 2000"),
    (5000, "2001 - 5000"), (10000, "5001 - 10000"),
)
_EMP_OVERFLOW = "10000+"


def _dim_exprs() -> dict[str, str]:
    count_case = "\n              ".join(
        f"WHEN c.employee_count <= {ceil} THEN '{lbl}'" for ceil, lbl in _EMP_BUCKETS
    )
    canonical_in = ", ".join(
        f"'{lbl}'" for lbl in [l for _, l in _EMP_BUCKETS] + [_EMP_OVERFLOW]
    )
    emp = f"""CASE
            WHEN c.employee_count IS NOT NULL THEN
              CASE
              {count_case}
              ELSE '{_EMP_OVERFLOW}'
              END
            WHEN c.employee_range IN ({canonical_in}) THEN c.employee_range
            ELSE '(no size)'
          END"""
    industry = "COALESCE(NULLIF(LOWER(c.industry), ''), '(none)')"
    geo = "COALESCE(NULLIF(c.country, ''), NULLIF(c.list_location, ''), '(none)')"
    segment = "COALESCE(wla.bucket_id::text, '(none)')"
    return {"employeeSize": emp, "industry": industry, "geography": geo, "segments": segment}


_COLD = (
    "wla.id IS NOT NULL "
    "AND COALESCE(wla.is_nonjoiners, false) = false "
    "AND COALESCE(wla.is_no_list_data, false) = false"
)

_DIM_ORDER = ("employeeSize", "industry", "geography", "segments")


async def _funnel_scope(wids: list[str]) -> dict[str, dict[str, dict[str, int]]]:
    """One scope (current webinar OR pooled baseline): invited + WG funnel per
    dimension value, all four dimensions in one GROUPING SETS pass per stage.
    Returns {dimension: {label: {invited, regs, attended, att10}}}."""
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    exprs = _dim_exprs()
    dim_cols = ", ".join(f"{exprs[d]} AS d{i}" for i, d in enumerate(_DIM_ORDER))
    sets = ", ".join(f"({i + 1})" for i in range(len(_DIM_ORDER)))

    out: dict[str, dict[str, dict[str, int]]] = {d: {} for d in _DIM_ORDER}

    def _absorb(rows, keymap: dict[str, str]) -> None:
        for row in rows:
            m = row._mapping
            for i, d in enumerate(_DIM_ORDER):
                label = m[f"d{i}"]
                if label is None:
                    continue
                slot = out[d].setdefault(str(label), {})
                for col, key in keymap.items():
                    slot[key] = slot.get(key, 0) + int(m[col] or 0)
                break

    # Stage 1: invited (the heavy membership scan).
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
        await db.execute(sa_text("SET LOCAL work_mem = '256MB'"))
        r = await db.execute(sa_text(f"""
            SELECT {dim_cols}, COUNT(DISTINCT LOWER(c.email)) AS invited
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
            WHERE m.webinar_id = ANY(CAST(:wids AS uuid[])) AND {_COLD}
            GROUP BY GROUPING SETS ({sets})
        """).bindparams(wids=wids))
        _absorb(r.all(), {"invited": "invited"})

    # Stage 2: WebinarGeek regs / attendance (small inner join — fast).
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))
        await db.execute(sa_text("SET LOCAL work_mem = '128MB'"))
        r = await db.execute(sa_text(f"""
            SELECT {dim_cols},
                COUNT(DISTINCT LOWER(c.email)) AS regs,
                COUNT(DISTINCT LOWER(c.email)) FILTER (
                    WHERE wgs.watched_live = TRUE OR wgs.minutes_viewing > 0
                ) AS attended,
                COUNT(DISTINCT LOWER(c.email)) FILTER (
                    WHERE (wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)
                    AND wgs.minutes_viewing >= 10
                ) AS att10
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
            JOIN webinars w2 ON w2.id = m.webinar_id
            JOIN webinargeek_subscribers wgs
                ON LOWER(wgs.email) = LOWER(c.email)
               AND wgs.broadcast_id = w2.broadcast_id
            WHERE m.webinar_id = ANY(CAST(:wids AS uuid[])) AND {_COLD}
            GROUP BY GROUPING SETS ({sets})
        """).bindparams(wids=wids))
        _absorb(r.all(), {"regs": "regs", "attended": "attended", "att10": "att10"})

    return out


async def _bucket_names(bucket_ids: list[str]) -> dict[str, str]:
    if not bucket_ids:
        return {}
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            sa_text("SELECT id::text, name FROM outreach_buckets WHERE id = ANY(CAST(:ids AS uuid[]))")
            .bindparams(ids=bucket_ids)
        )).all()
    return {rid: name for rid, name in rows}


def _normalize_dim_labels(dim: str, cells: dict[str, dict[str, int]],
                          names: dict[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Merge label variants (geo mojibake, industry casing, bucket-id→name)."""
    out: dict[str, dict[str, int]] = {}
    for label, metrics in cells.items():
        if dim == "geography":
            key = _norm_geo(label)
        elif dim == "industry":
            key = _norm_industry(label)
        elif dim == "segments":
            key = (names or {}).get(label, label if label == "(none)" else "(unknown bucket)")
        else:
            key = label
        slot = out.setdefault(key, {})
        for k, v in metrics.items():
            slot[k] = slot.get(k, 0) + v
    return out


def _funnel_cells(
    current: dict[str, dict[str, int]],
    baseline: dict[str, dict[str, int]],
    baseline_n: int,
    top: int = 15,
) -> list[dict[str, Any]]:
    """Merge current + baseline per label into report cells."""
    def shape(m: dict[str, int], divisor: int = 1) -> dict[str, Any]:
        inv = m.get("invited", 0)
        regs = m.get("regs", 0)
        att = m.get("attended", 0)
        return {
            "invited": round(inv / divisor, 1) if divisor > 1 else inv,
            "regs": round(regs / divisor, 1) if divisor > 1 else regs,
            "regRate": _rate(regs, inv),
            "attPctOfRegs": _rate(att, regs),
            "attendeesPer10kInv": round(att / inv * 10000, 1) if inv else None,
        }

    labels = sorted(
        set(current) | set(baseline),
        key=lambda l: -(current.get(l, {}).get("invited", 0)),
    )
    cells = []
    for label in labels[:top]:
        cells.append({
            "key": label,
            "current": shape(current.get(label, {})),
            "baseline": shape(baseline.get(label, {}), divisor=max(baseline_n, 1)),
        })
    return cells


# ---------------------------------------------------------------------------
# Registrant classification + non-joiner pool + bookings (cheap SQL)
# ---------------------------------------------------------------------------

async def _fetch_regs(webinar_id: str, broadcast_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))
        rows = (await db.execute(sa_text("""
            WITH regs AS (
                SELECT LOWER(email) AS email,
                       BOOL_OR(watched_live = TRUE OR minutes_viewing > 0) AS att,
                       BOOL_OR(minutes_viewing >= 10) AS att10
                FROM webinargeek_subscribers
                WHERE broadcast_id = :bid AND email IS NOT NULL
                GROUP BY 1
            )
            SELECT r.email, r.att, r.att10,
                   EXISTS (
                       SELECT 1 FROM contacts c
                       JOIN webinar_contact_memberships m ON m.contact_id = c.id
                       WHERE m.webinar_id = CAST(:wid AS uuid)
                         AND LOWER(c.email) = r.email
                   ) AS planned
            FROM regs r
        """).bindparams(bid=broadcast_id, wid=webinar_id))).all()
    return [
        {"email": e, "att": bool(a), "att10": bool(a10), "planned": bool(p)}
        for e, a, a10, p in rows
    ]


async def _fetch_nonjoiner_pool(webinar_id: str) -> set[str]:
    """This webinar's non-joiner pool — see services/nonjoiners.py.

    Shares its definition with the Statistics page, so the report's non-joiner
    split and the Nonjoiners row now agree by construction.
    """
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))
        return set(await nonjoiner_pool_emails(db, webinar_id))


async def _fetch_bookers(webinar_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        rows = (await db.execute(sa_text("""
            SELECT b.ghl_contact_id,
                   COALESCE(LOWER(c.email), LOWER(g.email)) AS email,
                   b.lead_quality,
                   LOWER(COALESCE(b.call_status, '')) AS call_status,
                   c.lead_list_name,
                   (b.contact_id IS NOT NULL AND EXISTS (
                       SELECT 1 FROM webinar_contact_memberships m
                       WHERE m.webinar_id = CAST(:wid AS uuid)
                         AND m.contact_id = b.contact_id
                   )) AS planned
            FROM webinar_booking_attribution b
            LEFT JOIN contacts c ON c.id = b.contact_id
            LEFT JOIN ghl_contact g ON g.ghl_contact_id = b.ghl_contact_id
            WHERE b.webinar_id = CAST(:wid AS uuid)
        """).bindparams(wid=webinar_id))).mappings().all()

    # Dedupe by contact: a rebooked contact counts once, prefer the rated row.
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        cid = r["ghl_contact_id"]
        cur = best.get(cid)
        if cur is None or (not cur.get("lead_quality") and r["lead_quality"]):
            best[cid] = dict(r)
    return list(best.values())


def _source_bucket(list_name: str | None) -> str:
    s = (list_name or "").strip()
    if not s:
        return "(no source)"
    # Collapse numbered multi-part exports ("...-part-3", "Pt 2") to one source.
    s = re.sub(r"[-\s]*(part|pt)[-\s]*\d+$", "", s, flags=re.IGNORECASE).strip(" -,")
    return s[:60] or "(no source)"


# ---------------------------------------------------------------------------
# Payload build
# ---------------------------------------------------------------------------

async def build_report_payload(webinar_id: str) -> dict[str, Any]:
    from services import statistics as stats
    from services.statistics_snapshot import read_all_payloads, read_snapshot_payload

    caveats: list[str] = []
    t0 = time.monotonic()

    # -- Webinar meta + passed set --------------------------------------
    summaries = await stats.get_statistics_webinar_list("auto")
    by_id = {s.get("webinarId"): s for s in summaries if s.get("webinarId")}
    me = by_id.get(webinar_id)
    if me is None:
        raise ValueError(f"webinar {webinar_id} not found")
    number = int(me.get("number") or 0)
    my_date = me.get("date") or ""

    passed = [
        s for s in summaries
        if s.get("webinarId") and stats._is_passed_webinar(s.get("date"), s.get("status"))
    ]

    # Baseline webinars: primary variant per number, strictly before this one.
    prior_by_number: dict[int, list[dict[str, Any]]] = {}
    for s in passed:
        n = s.get("number") or 0
        if n and n != number and ((s.get("date") or "") < my_date or n < number):
            prior_by_number.setdefault(n, []).append(s)
    prior_primary = sorted(
        (_primary_of(v) for v in prior_by_number.values()),
        key=lambda s: (s.get("date") or "", s.get("number") or 0),
        reverse=True,
    )

    # -- Scorecard baselines from statistics snapshots -------------------
    # Long baseline = the last BASELINE_WINDOW webinars (not all-time): the
    # operation changes quickly, so a rolling window tracks "how we run now".
    snap_all = await read_all_payloads("ghl")
    counts_all: list[dict[str, float]] = []
    counts_4w: list[dict[str, float]] = []
    cutoff_4w = None
    try:
        cutoff_4w = (datetime.fromisoformat(my_date) - timedelta(days=28)).date().isoformat()
    except Exception:
        pass
    for s in prior_primary[:BASELINE_WINDOW]:
        p = snap_all.get(s["webinarId"])
        if not p:
            continue
        c = _snapshot_counts(p)
        counts_all.append(c)
        if cutoff_4w and (s.get("date") or "") >= cutoff_4w:
            counts_4w.append(c)
    baseline_all = _fold_baseline(counts_all)
    baseline_4w = _fold_baseline(counts_4w)
    if not counts_all:
        caveats.append("No prior statistics snapshots found — baseline columns are empty.")

    # -- Current webinar core numbers ------------------------------------
    my_snap = snap_all.get(webinar_id) or await read_snapshot_payload("ghl", webinar_id)
    if my_snap is None:
        my_snap = await stats.get_statistics_webinar_one(source="auto", webinar_id=webinar_id)
    if my_snap is None:
        raise ValueError(f"no statistics available for webinar {webinar_id}")
    my_counts = _snapshot_counts(my_snap)
    summary = my_snap.get("summary") or {}

    broadcast_id = None
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        broadcast_id = (await db.execute(
            sa_text("SELECT broadcast_id FROM webinars WHERE id = CAST(:wid AS uuid)")
            .bindparams(wid=webinar_id)
        )).scalar_one_or_none()

    # -- Registrant classification (6-webinar NJ pool) -------------------
    regs: list[dict[str, Any]] = []
    pool: set[str] = set()
    if broadcast_id:
        try:
            regs = await _fetch_regs(webinar_id, broadcast_id)
        except Exception as exc:
            caveats.append(f"Registrant classification unavailable: {str(exc)[:120]}")
            logger.warning("webinar_report: regs fetch failed: %s", exc)
        try:
            pool = await _fetch_nonjoiner_pool(webinar_id)
        except Exception as exc:
            caveats.append(f"Non-joiner pool unavailable: {str(exc)[:120]}")
            logger.warning("webinar_report: NJ pool failed: %s", exc)
    else:
        caveats.append("No WebinarGeek broadcast linked — registration/attendance cohorts unavailable.")

    def _cohort(r: dict[str, Any]) -> str:
        if r["planned"]:
            return "netNew"
        if r["email"] in pool:
            return "nonjoiner"
        return "noListData"

    cohort_counts: dict[str, dict[str, int]] = {
        k: {"regs": 0, "attended": 0, "att10": 0} for k in ("netNew", "nonjoiner", "noListData")
    }
    for r in regs:
        c = cohort_counts[_cohort(r)]
        c["regs"] += 1
        if r["att"]:
            c["attended"] += 1
        if r["att10"]:
            c["att10"] += 1

    total_regs = len(regs) or my_counts["totalRegs"]
    total_att = sum(c["attended"] for c in cohort_counts.values()) or my_counts["totalAttended"]
    invited = my_counts["invited"]

    current_scorecard: dict[str, Any] = {
        "invited": invited,
        "actuallyUsed": summary.get("actuallyUsed"),
        "totalRegs": total_regs,
        "netNewRegs": cohort_counts["netNew"]["regs"] or my_counts["netNewRegs"],
        "nonjoinerRegs": cohort_counts["nonjoiner"]["regs"] or my_counts["nonjoinerRegs"],
        "noListDataRegs": cohort_counts["noListData"]["regs"] or my_counts["noListDataRegs"],
        "regRate": _rate(cohort_counts["netNew"]["regs"] or my_counts["netNewRegs"], invited),
        "yesMarked": my_counts["yesMarked"],
        "maybeMarked": my_counts["maybeMarked"],
        "totalAttended": total_att,
        "total10MinPlus": sum(c["att10"] for c in cohort_counts.values()) or my_counts["total10MinPlus"],
        "attendRateOfRegs": _rate(total_att, total_regs),
        "netNewAttendRate": _rate(cohort_counts["netNew"]["attended"], cohort_counts["netNew"]["regs"]),
        "attendPer10kInvited": round(total_att / invited * 10000, 1) if invited else None,
        "uniqueBookers": my_counts["uniqueBookers"],
    }

    # -- Funnels (heavy) --------------------------------------------------
    funnels: dict[str, Any] = {}
    baseline_wids = [s["webinarId"] for s in prior_primary[:BASELINE_WINDOW]]
    try:
        cur_scope = await _funnel_scope([webinar_id])
    except Exception as exc:
        cur_scope = None
        caveats.append(f"Per-dimension funnels (current) failed: {str(exc)[:120]}")
        logger.warning("webinar_report: current funnel scope failed: %s", exc)
    base_scope = None
    if baseline_wids:
        try:
            base_scope = await _funnel_scope(baseline_wids)
        except Exception as exc:
            caveats.append(f"Per-dimension baselines failed: {str(exc)[:120]}")
            logger.warning("webinar_report: baseline funnel scope failed: %s", exc)
    else:
        caveats.append("No prior webinars — funnel baselines empty.")

    if cur_scope is not None:
        seg_ids = [
            l for l in set(cur_scope["segments"]) | set((base_scope or {}).get("segments", {}))
            if l != "(none)"
        ]
        names = await _bucket_names(seg_ids)
        for dim in _DIM_ORDER:
            cur_cells = _normalize_dim_labels(dim, cur_scope.get(dim, {}), names)
            base_cells = _normalize_dim_labels(dim, (base_scope or {}).get(dim, {}), names)
            funnels[dim] = {
                "cells": _funnel_cells(cur_cells, base_cells, len(baseline_wids)),
                "baselineWebinarCount": len(baseline_wids) if base_scope else 0,
            }

    # -- Bookings deep-dive ----------------------------------------------
    bookings: dict[str, Any] = {}
    try:
        bookers = await _fetch_bookers(webinar_id)
        reg_emails = {r["email"] for r in regs}
        quality = {"great": 0, "ok": 0, "barely": 0, "bad": 0, "unrated": 0}
        call_status: dict[str, int] = {}
        origin = {"netNew": 0, "nonjoiner": 0, "noListData": 0, "notRegistrant": 0}
        sources: dict[str, int] = {}
        for b in bookers:
            lq = _LQ_KEYS.get(b.get("lead_quality") or "")
            quality[lq or "unrated"] += 1
            st = b.get("call_status") or "unknown"
            if st in ("noshow", "no show", "no-show"):
                st = "noShow"
            call_status[st] = call_status.get(st, 0) + 1
            email = b.get("email")
            if b.get("planned") and email in reg_emails:
                origin["netNew"] += 1
            elif email in pool and email in reg_emails:
                origin["nonjoiner"] += 1
            elif email in reg_emails:
                origin["noListData"] += 1
            else:
                origin["notRegistrant"] += 1
            src = _source_bucket(b.get("lead_list_name"))
            sources[src] = sources.get(src, 0) + 1
        rated = quality["great"] + quality["ok"] + quality["barely"] + quality["bad"]
        implied = None
        if rated:
            implied = round(
                (quality["great"] * CLOSE_RATES["great"]
                 + quality["ok"] * CLOSE_RATES["ok"]
                 + quality["barely"] * CLOSE_RATES["barely"]) / rated,
                4,
            )
        bookings = {
            "uniqueBookedContacts": len(bookers),
            "callStatus": call_status,
            "quality": quality,
            "rated": rated,
            "impliedCloseRate": implied,
            "origin": origin,
            "leadSources": sorted(
                ({"source": k, "count": v} for k, v in sources.items()),
                key=lambda x: -x["count"],
            )[:8],
        }
        if rated < 10:
            caveats.append(f"Only {rated} of {len(bookers)} booked calls are quality-rated — quality mix is directional.")
    except Exception as exc:
        caveats.append(f"Bookings deep-dive unavailable: {str(exc)[:120]}")
        logger.warning("webinar_report: bookings failed: %s", exc)

    # -- Non-joiner package ----------------------------------------------
    nj = cohort_counts["nonjoiner"]
    nonjoiners = {
        "poolSize": len(pool),
        "windowWebinars": NONJOINER_WINDOW,
        "regs": nj["regs"],
        "regRate": _rate(nj["regs"], len(pool)),
        "attended": nj["attended"],
        "att10": nj["att10"],
        "attendRateOfRegs": _rate(nj["attended"], nj["regs"]),
        "netNewAttendRateOfRegs": _rate(
            cohort_counts["netNew"]["attended"], cohort_counts["netNew"]["regs"]
        ),
        "netNewRegRateOfInvited": _rate(cohort_counts["netNew"]["regs"], invited),
    }

    # -- Standing caveats --------------------------------------------------
    caveats.extend([
        "Bookings = unique booked contacts from the booking-attribution layer (a rebooked contact counts once); the stats page's Sales columns use GHL opportunity counting and can differ.",
        f"Non-joiners = registrants of the last {NONJOINER_WINDOW} aired webinars whose most recent registration in that window was a no-show (live, replay or any viewing minutes count as joining). Re-registering restarts the {NONJOINER_WINDOW}-invite budget; blocklisted, unsubscribed, already-planned and converted contacts (booked / won / disqualified) are removed permanently.",
        "Registration/attendance counted as distinct emails per broadcast; WebinarGeek parent totals can differ by ~1–3%.",
        "Scorecard baseline cohort splits (net-new vs non-joiner) come from statistics snapshots; snapshots taken before the 6-webinar non-joiner definition landed still carry the old previous-webinar-only split until recomputed.",
    ])
    computed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "webinarId": webinar_id,
        "number": number,
        "variantLabel": me.get("variantLabel"),
        "date": my_date,
        "title": me.get("title") or my_snap.get("title") or "",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "computeMs": computed_ms,
        "scorecard": {
            "current": current_scorecard,
            "baselineAll": baseline_all,
            "baseline4w": baseline_4w,
        },
        "funnels": funnels,
        "bookings": bookings,
        "nonjoiners": nonjoiners,
        "caveats": caveats,
    }


# ---------------------------------------------------------------------------
# AI insights (Claude — REPORT_MODEL)
# ---------------------------------------------------------------------------

_INSIGHTS_SYSTEM = """You are the in-house data analyst for a B2B webinar funnel team.
You are given one webinar's report data as JSON: a scorecard (current vs the
all-webinar average and the last-4-week average), per-dimension funnels
(industry / geography / employee size / segments; each cell has current vs a
last-10-webinar baseline), a bookings deep-dive, and a non-joiner package.

Write the insights section of the report. Rules:
- Return ONLY a JSON array, no prose, no markdown fences:
  [{"title": "...", "bullets": ["...", "..."]}, ...]
- 4 to 7 insight groups, 1–3 bullets each. Plain factual language.
- Every bullet must cite at least one concrete number from the data.
- Compare against the averages, not just directionally: say by how much.
- Distinguish mix shifts from rate shifts when the funnels support it.
- Flag small samples explicitly (e.g. "only N rated calls") instead of
  drawing confident conclusions from them.
- Registration volume is not pipeline: relate attendance to bookings and
  quality where the data allows.
- If a section's data is missing or a caveat undermines a comparison, say so
  briefly rather than guessing.
- No recommendations to "investigate further" without saying what number
  triggered it."""


def _compact_for_ai(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the payload for the model: top funnel cells only, rounded rates."""
    slim = json.loads(json.dumps(payload))  # deep copy, JSON-native
    for dim, block in (slim.get("funnels") or {}).items():
        cells = block.get("cells") or []
        block["cells"] = cells[:10]
    return slim


def _parse_insights(text: str) -> list[dict[str, Any]]:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in response")
    data = json.loads(s[start:end + 1])
    if not isinstance(data, list) or not data:
        raise ValueError("insights is not a non-empty list")
    out = []
    for item in data:
        title = str(item.get("title") or "").strip()
        bullets = [str(b).strip() for b in (item.get("bullets") or []) if str(b).strip()]
        if title and bullets:
            out.append({"title": title, "bullets": bullets})
    if not out:
        raise ValueError("no valid insight groups")
    return out


async def generate_insights(payload: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """AI insights for a report payload. Never raises: returns (insights, error)."""
    from db.session import AsyncSessionLocal
    from services.generation import _log_claude_cost, _resolve_client

    try:
        async with AsyncSessionLocal() as db:
            client = await _resolve_client(db)
    except Exception as exc:
        return None, f"no Anthropic client: {str(exc)[:200]}"

    user_msg = json.dumps(_compact_for_ai(payload), separators=(",", ":"), sort_keys=True)
    last_err: str | None = None
    for attempt in range(2):
        try:
            resp = await client.with_options(timeout=INSIGHTS_TIMEOUT_SECONDS).messages.create(
                model=REPORT_MODEL,
                max_tokens=INSIGHTS_MAX_TOKENS,
                system=_INSIGHTS_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                extra_body={"output_config": {"effort": INSIGHTS_EFFORT}},
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                # Awaited (not fire-and-forget) so the cost row always lands,
                # including in short-lived job contexts. Never raises.
                await _log_claude_cost(
                    REPORT_MODEL,
                    int(getattr(usage, "input_tokens", 0) or 0),
                    int(getattr(usage, "output_tokens", 0) or 0),
                    session_label="webinar_report",
                )
            text = "".join(
                getattr(block, "text", "") for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            return _parse_insights(text), None
        except Exception as exc:
            last_err = str(exc)[:300]
            logger.warning("webinar_report: insights attempt %d failed: %s", attempt + 1, exc)
    return None, last_err


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _upsert_request(webinar_id: str) -> None:
    """Durable 'please generate' marker — survives deploys/restarts so the
    scheduler sweep can retry a generation the process death swallowed."""
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            INSERT INTO webinar_report_request (webinar_id)
            VALUES (:wid)
            ON CONFLICT (webinar_id) DO UPDATE SET requested_at = now()
        """).bindparams(wid=webinar_id))
        await db.commit()


async def _resolve_request(webinar_id: str, error: str | None) -> None:
    """Delete the marker on success; count the failure otherwise."""
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            if error is None:
                await db.execute(sa_text(
                    "DELETE FROM webinar_report_request WHERE webinar_id = :wid"
                ).bindparams(wid=webinar_id))
            else:
                await db.execute(sa_text("""
                    UPDATE webinar_report_request
                    SET attempts = attempts + 1, last_error = :err
                    WHERE webinar_id = :wid
                """).bindparams(wid=webinar_id, err=error[:300]))
            await db.commit()
    except Exception as exc:
        logger.warning("webinar_report: request bookkeeping failed: %s", exc)


async def generate_report(webinar_id: str) -> dict[str, Any] | None:
    """Build the numbers, persist, then attach AI insights. Serialized."""
    async with _gen_lock:
        _status.update({
            "running": True,
            "webinar_id": webinar_id,
            "phase": "queries",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "last_error": None,
        })
        t0 = time.monotonic()
        try:
            payload = await build_report_payload(webinar_id)
            await _upsert_payload(payload, int((time.monotonic() - t0) * 1000))

            _status["phase"] = "ai"
            insights, ai_error = await generate_insights(payload)
            await _update_insights(webinar_id, insights, ai_error)
            await _resolve_request(webinar_id, None)
        except Exception as exc:
            _status["last_error"] = str(exc)[:300]
            logger.exception("webinar_report: generation failed for %s", webinar_id)
            await _resolve_request(webinar_id, str(exc))
            return None
        finally:
            _status.update({
                "running": False,
                "phase": None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            })
    return await read_report(webinar_id)


async def request_generate(webinar_id: str) -> None:
    """Durably request generation (marker row) and start it in-process. If
    this process dies mid-run, the scheduler sweep retries from the marker."""
    try:
        await _upsert_request(webinar_id)
    except Exception as exc:  # table missing before migration — still generate
        logger.warning("webinar_report: could not persist request: %s", exc)
    schedule_generate(webinar_id)


async def run_pending_requests() -> int:
    """Scheduler sweep: generate any durably-requested report that isn't
    already running here. Returns how many generations were run."""
    from sqlalchemy import select
    from db.models import WebinarReportRequest
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(WebinarReportRequest.webinar_id)
            .where(WebinarReportRequest.attempts < MAX_GENERATION_ATTEMPTS)
            .order_by(WebinarReportRequest.requested_at)
        )).scalars().all()

    ran = 0
    for wid in rows:
        if _status["running"] and _status["webinar_id"] == wid:
            continue
        if f"report:{wid}" in _pending:
            continue
        logger.info("webinar_report sweep: generating pending report for %s", wid)
        await generate_report(wid)
        ran += 1
    return ran


def schedule_generate(webinar_id: str) -> None:
    """Fire-and-forget report generation, coalescing duplicates."""
    token = f"report:{webinar_id}"
    if token in _pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("webinar_report.schedule_generate: no running loop; skipping %s", webinar_id)
        return

    _pending.add(token)

    async def _runner():
        try:
            await generate_report(webinar_id)
        except Exception as exc:
            logger.warning("webinar_report task %s failed: %s", token, exc)
        finally:
            _pending.discard(token)

    task = loop.create_task(_runner())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
