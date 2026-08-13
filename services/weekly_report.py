"""Weekly webinar report — data assembly, AI narrative, HTML render, Resend send.

Pipeline (shared by the scheduler job and POST /reports/send-test):
    get_report_settings() → build_report_data() → generate_narrative() →
    render_report_html() → resend_client.send_email()

Report scope: the latest *passed* webinar (Statistics-page definition) vs the
previous webinar number. Design mirrors the legacy "Webinar Intelligence
Report" (dark cards, letter-spaced section headers):

  Header → Anomaly Flags → Summary & Actions (AI)
  Section 1 — Campaign Performance: overview + retention funnel,
      week-over-week, A/B variants, sender performance (with vs-W-1 deltas),
      segment leaderboards (current ranked 3 ways + all-time top/bottom)
  Section 2 — Copy Performance: best/worst highlight cards + full tables
  Section 3 — Sales Call Outcomes (current webinar, 1-week lag)
  Section 4 — Previous webinar final pipeline status (2-week lag)

Each section carries a one-line AI insight; the synthetic Nonjoiners /
No List Data cohorts are shown separately from real buckets.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from sqlalchemy import select

from db.models import ConnectorCredential, OutreachBucket, ReportSettings
from db.session import AsyncSessionLocal
from integrations import resend_client
from services.statistics import (
    _SUM_KEYS,
    _is_passed_webinar,
    compute_derived_metrics,
    get_statistics_webinar_list,
    get_statistics_webinar_one,
)

logger = logging.getLogger(__name__)

RESEND_PROVIDER = "resend"
VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Guard against double-sends when the scheduler reloads near the trigger time
# (settings PATCH re-registers the cron job; APScheduler may re-fire).
DOUBLE_SEND_GUARD_MINUTES = 30

NARRATIVE_MAX_TOKENS = 1400
NARRATIVE_TIMEOUT_SECONDS = 120.0

STATISTICS_PAGE_URL = "https://competeiq-frontend.onrender.com/statistics"

# All-time segment leaderboards ignore buckets below this invite volume so a
# 3-reg / 500-invite fluke can't top the table.
ALL_TIME_MIN_INVITES = 3000


# ---------------------------------------------------------------------------
# Settings (singleton row, id=1 — seeded on first read like GHLSyncSettings)
# ---------------------------------------------------------------------------

def _settings_dict(row: ReportSettings) -> dict[str, Any]:
    return {
        "enabled": row.enabled,
        "day_of_week": row.day_of_week,
        "hour_local": row.hour_local,
        "minute_local": row.minute_local,
        "timezone": row.timezone,
        "recipients": list(row.recipients or []),
        "from_address": row.from_address,
        "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
        "last_error": row.last_error,
    }


async def get_report_settings() -> dict[str, Any]:
    """Read (and seed if missing) the report settings singleton."""
    async with AsyncSessionLocal() as db:
        row = await db.get(ReportSettings, 1)
        if row is None:
            row = ReportSettings(id=1)
            db.add(row)
            await db.commit()
            row = await db.get(ReportSettings, 1)
        return _settings_dict(row)


async def _record_send_result(*, sent_at: datetime | None, error: str | None) -> None:
    """Stamp last_sent_at / last_error on the settings row (best-effort)."""
    try:
        async with AsyncSessionLocal() as db:
            row = await db.get(ReportSettings, 1)
            if row is None:
                return
            if sent_at is not None:
                row.last_sent_at = sent_at
                row.last_error = None
            else:
                row.last_error = error
            await db.commit()
    except Exception as exc:
        logger.error("Failed to record report send result: %s", exc)


async def _resolve_resend_key() -> str | None:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(ConnectorCredential).where(
                ConnectorCredential.provider == RESEND_PROVIDER,
                ConnectorCredential.name == "default",
            )
        )).scalar_one_or_none()
        return row.api_key if row else None


# ---------------------------------------------------------------------------
# Webinar selection
# ---------------------------------------------------------------------------

def _primary_of(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline variant of an A/B group: the unlabeled one if present,
    otherwise the first by label — mirrors the Statistics-page convention."""
    unlabeled = [v for v in variants if not v.get("variantLabel")]
    if unlabeled:
        return unlabeled[0]
    return sorted(variants, key=lambda v: v.get("variantLabel") or "")[0]


async def _select_webinars(webinar_id: str | None = None) -> dict[str, Any] | None:
    """Pick the report's current webinar (+ siblings) and the previous webinar.

    Returns {"current": summary, "siblings": [...], "previous": summary|None}
    or None when no passed webinar exists. `webinar_id` pins the current
    webinar explicitly (test sends); otherwise the latest passed one wins.
    """
    summaries = await get_statistics_webinar_list("auto")
    passed = [s for s in summaries
              if s.get("webinarId") and _is_passed_webinar(s.get("date"), s.get("status"))]
    if not passed:
        return None

    if webinar_id:
        current = next((s for s in passed if s.get("webinarId") == webinar_id), None)
        if current is None:
            return None
    else:
        latest = max(passed, key=lambda s: (s.get("date") or "", s.get("number") or 0))
        group = [s for s in passed if s.get("number") == latest.get("number")]
        current = _primary_of(group)

    siblings = [s for s in passed
                if s.get("number") == current.get("number")
                and s.get("webinarId") != current.get("webinarId")]

    # Previous = greatest number strictly below the current one; variants of
    # the same number never count (mirrors api/routers/statistics.py).
    prior = [s for s in passed if (s.get("number") or 0) < (current.get("number") or 0)]
    previous = None
    if prior:
        prev_number = max(s.get("number") or 0 for s in prior)
        previous = _primary_of([s for s in prior if s.get("number") == prev_number])

    return {"current": current, "siblings": siblings, "previous": previous}


# ---------------------------------------------------------------------------
# Row folding (copy variants / senders)
# ---------------------------------------------------------------------------

def _fold_rows(rows: list[dict[str, Any]], key_fn, label_fn) -> list[dict[str, Any]]:
    """Group per-list rows, sum their raw metrics, recompute derived rates.

    Percentages are always derived from the summed counts, never averaged —
    same convention as the Segments tab.
    """
    groups: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") != "list":
            continue
        key = key_fn(row)
        if key is None:
            continue
        slot = groups.get(key)
        if slot is None:
            slot = {"label": label_fn(row), "listCount": 0, "raw": {}}
            groups[key] = slot
        slot["listCount"] += 1
        m = row.get("metrics") or {}
        for k in _SUM_KEYS:
            v = m.get(k)
            if v is not None:
                slot["raw"][k] = slot["raw"].get(k, 0.0) + float(v)

    out = []
    for slot in groups.values():
        derived, _ = compute_derived_metrics(slot["raw"])
        out.append({"label": slot["label"], "listCount": slot["listCount"], "metrics": derived})
    out.sort(key=lambda g: g["metrics"].get("invited") or 0, reverse=True)
    return out


def _truncate(text: str | None, n: int = 80) -> str:
    if not text:
        return "—"
    return text if len(text) <= n else text[: n - 1] + "…"


def _copy_key(field: str):
    def key_fn(row: dict[str, Any]):
        c = row.get(field)
        return c.get("id") if c else None
    return key_fn


def _copy_label(field: str):
    def label_fn(row: dict[str, Any]) -> str:
        c = row.get(field) or {}
        idx = c.get("variantIndex")
        prefix = f"V{idx + 1}: " if isinstance(idx, int) else ""
        return prefix + _truncate(c.get("text"))
    return label_fn


# ---------------------------------------------------------------------------
# Segment folding (from the per-list rows, so kind info survives)
# ---------------------------------------------------------------------------
# The Segments dashboard endpoint rolls every bucket-less row into one
# "Other (no bucket)" line; the report instead keeps the synthetic Nonjoiners /
# No List Data rows separate so they can be shown apart from the real buckets.

_SEGMENT_COUNT_KEYS = (
    ("invites", "invited"),
    ("regs", "totalRegs"),
    ("attendees10m", "total10MinPlus"),
    ("bookings", "totalBookings"),
)

_SPECIAL_KIND_LABELS = (
    ("nonjoiners", "Nonjoiners"),
    ("no_list_data", "No List Data"),
)


def _new_seg_slot(label: str) -> dict[str, Any]:
    return {"label": label, "quality": None,
            "invites": 0.0, "regs": 0.0, "attendees10m": 0.0, "bookings": 0.0}


def _acc_seg(slot: dict[str, Any], metrics: dict[str, Any]) -> None:
    for out_key, src_key in _SEGMENT_COUNT_KEYS:
        v = metrics.get(src_key)
        if v is not None:
            slot[out_key] += float(v)


async def _fold_segment_rows(all_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold stats rows into {special: [...], buckets: [...], totals: {...}}."""
    special_slots: dict[str, dict[str, Any]] = {}
    bucket_slots: dict[str, dict[str, Any]] = {}
    other: dict[str, Any] | None = None
    label_by_kind = dict(_SPECIAL_KIND_LABELS)

    for row in all_rows:
        kind = row.get("kind")
        metrics = row.get("metrics") or {}
        if kind in label_by_kind:
            slot = special_slots.setdefault(kind, _new_seg_slot(label_by_kind[kind]))
            _acc_seg(slot, metrics)
        elif kind == "list":
            bucket_id = row.get("bucketId")
            if bucket_id:
                slot = bucket_slots.get(bucket_id)
                if slot is None:
                    slot = _new_seg_slot(row.get("bucketName") or "—")
                    bucket_slots[bucket_id] = slot
                _acc_seg(slot, metrics)
            else:
                if other is None:
                    other = _new_seg_slot("Other (no bucket)")
                _acc_seg(other, metrics)

    # Manual quality labels (good/medium/bad) for the named buckets, same
    # lookup the Segments dashboard does.
    if bucket_slots:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(OutreachBucket.id, OutreachBucket.quality).where(
                    OutreachBucket.id.in_(list(bucket_slots))
                )
            )
            quality_by_id = {r[0]: r[1] for r in res.all()}
        for bucket_id, slot in bucket_slots.items():
            slot["quality"] = quality_by_id.get(bucket_id)

    buckets = list(bucket_slots.values())
    if other is not None:
        buckets.append(other)
    special = [special_slots[k] for k, _ in _SPECIAL_KIND_LABELS if k in special_slots]

    totals = _new_seg_slot("Total")
    for slot in special + buckets:
        for out_key, _ in _SEGMENT_COUNT_KEYS:
            totals[out_key] += slot[out_key]

    return {"special": special, "buckets": buckets, "totals": totals}


async def _fold_all_time_buckets() -> dict[str, Any] | None:
    """All-time per-bucket invites/regs across every passed webinar's snapshot.
    Returns {"top": [...], "bottom": [...]} ranked by reg rate (min-volume
    gated), or None if the snapshot store is empty/unavailable."""
    try:
        from services import statistics_snapshot as snap
        summaries = await get_statistics_webinar_list("auto")
        passed_ids = {
            s["webinarId"] for s in summaries
            if s.get("webinarId") and _is_passed_webinar(s.get("date"), s.get("status"))
        }
        payloads = await snap.read_all_payloads("auto")
    except Exception as exc:
        logger.warning("Weekly report: all-time segment fold unavailable: %s", exc)
        return None

    slots: dict[str, dict[str, Any]] = {}
    for wid, payload in payloads.items():
        if wid not in passed_ids:
            continue
        for row in payload.get("rows") or []:
            if row.get("kind") != "list" or not row.get("bucketId"):
                continue
            m = row.get("metrics") or {}
            slot = slots.get(row["bucketId"])
            if slot is None:
                slot = {"label": row.get("bucketName") or "—", "invites": 0.0, "regs": 0.0}
                slots[row["bucketId"]] = slot
            for key, src in (("invites", "invited"), ("regs", "totalRegs")):
                v = m.get(src)
                if v is not None:
                    slot[key] += float(v)

    ranked = [
        {**s, "rate": (s["regs"] / s["invites"]) if s["invites"] else 0.0}
        for s in slots.values()
        if s["invites"] >= ALL_TIME_MIN_INVITES
    ]
    if not ranked:
        return None
    ranked.sort(key=lambda s: s["rate"], reverse=True)
    return {"top": ranked[:5], "bottom": ranked[-5:][::-1]}


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

async def build_report_data(webinar_id: str | None = None) -> dict[str, Any] | None:
    """Assemble everything the report needs. None when no passed webinar."""
    selection = await _select_webinars(webinar_id)
    if selection is None:
        return None

    current_id = selection["current"]["webinarId"]
    current = await get_statistics_webinar_one("auto", current_id)
    if current is None:
        return None

    previous = None
    if selection["previous"]:
        previous = await get_statistics_webinar_one("auto", selection["previous"]["webinarId"])

    variants = []
    if selection["siblings"]:
        variants.append(current)
        for sib in selection["siblings"]:
            full = await get_statistics_webinar_one("auto", sib["webinarId"])
            if full:
                variants.append(full)

    # Copy + sender + segment folds across the whole variant group's list rows.
    all_rows: list[dict[str, Any]] = []
    for w in (variants or [current]):
        all_rows.extend(w.get("rows") or [])

    try:
        segments = await _fold_segment_rows(all_rows)
    except Exception as exc:
        logger.warning("Weekly report: segments unavailable: %s", exc)
        segments = None

    all_time = await _fold_all_time_buckets()

    senders = _fold_rows(
        all_rows,
        lambda r: r.get("sendInfo"),
        lambda r: r.get("sendInfo") or "Unknown",
    )
    # Previous webinar's per-sender fold — powers the "vs W-1" delta column.
    senders_prev: dict[str, dict[str, Any]] = {}
    if previous:
        for g in _fold_rows(
            previous.get("rows") or [],
            lambda r: r.get("sendInfo"),
            lambda r: r.get("sendInfo") or "Unknown",
        ):
            senders_prev[g["label"]] = g["metrics"]

    return {
        "current": current,
        "previous": previous,
        "variants": variants,  # empty when no A/B siblings
        "segments": segments,
        "allTime": all_time,
        "titleCopies": _fold_rows(all_rows, _copy_key("titleCopy"), _copy_label("titleCopy")),
        "descCopies": _fold_rows(all_rows, _copy_key("descCopy"), _copy_label("descCopy")),
        "senders": senders,
        "sendersPrev": senders_prev,
    }


# ---------------------------------------------------------------------------
# AI narrative
# ---------------------------------------------------------------------------

# Headline metrics shared by the narrative prompt and the week-over-week table.
_HEADLINE_METRICS: list[tuple[str, str, str]] = [
    # (key, label, format: "int" | "pct" | "ratio")
    ("invited", "Invited (planned)", "int"),
    ("actuallyUsed", "Actually used", "int"),
    ("totalRegs", "Registrations", "int"),
    ("invitedToRegPercent", "Registration rate (Inv>Reg)", "pct"),
    ("totalAttended", "Attendees", "int"),
    ("regToAttendPercent", "Attendance rate (Reg>Att)", "pct"),
    ("invitedToAttendPercent", "Inv>Att rate", "pct"),
    ("total10MinPlus", "Stayed 10m+", "int"),
    ("yesMarked", "Yes marked", "int"),
    ("yesPercent", "Yes % of invited", "pct"),
    ("maybeMarked", "Maybe marked", "int"),
    ("maybePer1kInv", "Maybe /1k invited", "ratio"),
    ("totalBookings", "Bookings", "int"),
    ("bookingsPerAttended", "Booking % (per attended)", "pct"),
    ("totalBookingsPer1kInv", "Bookings /1k invited", "ratio"),
    ("unsubPercent", "Unsub %", "pct"),
]


def _metric_snapshot(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    keys = [k for k, _, _ in _HEADLINE_METRICS] + [
        "total30MinPlus", "attend10MinPercent", "attend30MinPercent",
        "totalCallsDatePassed", "confirmed", "shows", "noShows", "canceled",
        "won", "disqualified", "qualified", "showPercent", "closeRatePercent",
        "qualPercent", "selfRegMarked", "selfRegAttended", "selfReg10MinPlus",
        "bookingsPerPast10Min",
    ]
    return {key: summary.get(key) for key in keys}


def _narrative_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Compact JSON handed to the model — headline + section aggregates."""
    current = data["current"]
    previous = data["previous"]
    payload: dict[str, Any] = {
        "currentWebinar": {
            "number": current.get("number"),
            "date": current.get("date"),
            "title": current.get("title"),
            "metrics": _metric_snapshot(current.get("summary")),
        },
        "previousWebinar": None,
        "segments": None,
        "allTimeSegments": data.get("allTime"),
        "variants": [
            {
                "variantLabel": v.get("variantLabel") or "(primary)",
                "metrics": _metric_snapshot(v.get("summary")),
            }
            for v in data["variants"]
        ],
        "titleCopies": [
            {"copy": g["label"], "lists": g["listCount"], "metrics": _metric_snapshot(g["metrics"])}
            for g in data["titleCopies"]
        ],
        "descCopies": [
            {"copy": g["label"], "lists": g["listCount"], "metrics": _metric_snapshot(g["metrics"])}
            for g in data["descCopies"]
        ],
        "senders": [
            {"sender": g["label"], "lists": g["listCount"], "metrics": _metric_snapshot(g["metrics"])}
            for g in data["senders"]
        ],
    }
    if previous:
        payload["previousWebinar"] = {
            "number": previous.get("number"),
            "date": previous.get("date"),
            "title": previous.get("title"),
            "metrics": _metric_snapshot(previous.get("summary")),
        }
    if data["segments"]:
        payload["segments"] = {
            "note": ("'special' rows (Nonjoiners / No List Data) are synthetic cohorts "
                     "without invite attribution — their rates are not comparable to buckets"),
            "special": data["segments"]["special"],
            "buckets": data["segments"]["buckets"],
        }
    return payload


_NARRATIVE_SYSTEM = """You are the analytics assistant for a cold-outreach webinar operation.
You get the stats JSON of the most recent webinar (plus the previous one for
comparison, per-bucket segments incl. all-time leaderboards, A/B variants,
copy variants and senders).
Metric conventions: keys ending in Percent are FRACTIONS (0.031 = 3.1%);
/1k metrics are per 1000 invited.

Write the weekly report narrative as plain text (no markdown, no HTML) in
exactly five sections, each opened by its header line (exact words, all caps):

FLAGS
0-3 bullet lines starting with "- ": anomalies that need attention — a metric
far outside its normal range, a sharp week-over-week drop, or a data artifact
(e.g. a 100% rate on 1-2 contacts, a bucket with implausible numbers). If
nothing is anomalous, output exactly "- none".

HIGHLIGHTS
3-5 bullet lines: the numbers that matter this week and how they moved vs the
previous webinar (say direction + magnitude, e.g. "+0.8pp").

MEANINGFUL CHANGES
2-4 bullet lines: differences that look real (not noise) across segments,
A/B variants, copies or senders. Name the segment/variant/copy/sender.

ACTIONS
3-5 bullet lines: concrete improvement ideas for next week — what to test or
change (copy angle, segment mix, sender allocation, invite volume), what to
scale up because it's working, what to close out or stop, and what to watch.
Tie each to a number. If sample sizes are small, say so instead of
over-claiming.

SECTION INSIGHTS
Exactly five bullet lines, one per report section, each formatted
"- <Key>: <insight>" using exactly these keys: Overview, Senders, Segments,
Copies, Sales. Each insight is one sentence (max 25 words), the single most
useful observation for that section, tied to a number.

Keep the whole thing under 420 words. Numbers with at most 1 decimal."""


async def generate_narrative(data: dict[str, Any]) -> str | None:
    """One Anthropic call → plain-text narrative. Any failure → None (the
    report still ships without it)."""
    try:
        from services.chat_agent import resolve_model_id
        from services.generation import _resolve_client

        async with AsyncSessionLocal() as db:
            client = await _resolve_client(db)
        payload = json.dumps(_narrative_payload(data), separators=(",", ":"), default=str)
        resp = await client.with_options(timeout=NARRATIVE_TIMEOUT_SECONDS).messages.create(
            model=resolve_model_id(None),
            max_tokens=NARRATIVE_MAX_TOKENS,
            system=_NARRATIVE_SYSTEM,
            messages=[{"role": "user", "content": f"Webinar stats JSON:\n{payload}"}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return text or None
    except Exception as exc:
        logger.warning("Weekly report: narrative generation failed (sending without it): %s", exc)
        return None


_NARRATIVE_HEADERS = ("FLAGS", "HIGHLIGHTS", "MEANINGFUL CHANGES", "ACTIONS", "SECTION INSIGHTS")

_INSIGHT_KEYS = ("Overview", "Senders", "Segments", "Copies", "Sales")


def _parse_narrative(narrative: str) -> dict[str, Any]:
    """Split the model's plain-text output into {section: [bullet, ...]} plus
    an "insights" dict keyed by report-section name."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in narrative.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.rstrip(":").upper()
        if upper in _NARRATIVE_HEADERS:
            current = upper
            sections[current] = []
            continue
        if current is None:
            continue
        sections[current].append(line[2:] if line.startswith("- ") else line)

    insights: dict[str, str] = {}
    for bullet in sections.get("SECTION INSIGHTS") or []:
        for key in _INSIGHT_KEYS:
            prefix = f"{key}:"
            if bullet.lower().startswith(prefix.lower()):
                insights[key] = bullet[len(prefix):].strip()
                break
    return {**sections, "insights": insights}


# ---------------------------------------------------------------------------
# HTML rendering — dark "Intelligence Report" theme, tables + inline CSS only
# ---------------------------------------------------------------------------

_BG = "#0d0d10"
_CARD = "#17171b"
_CARD_BORDER = "#2a2a31"
_ROW_BORDER = "#26262c"
_TEXT = "#e7e7ea"
_MUTED = "#9aa0a8"
_GREEN = "#34d399"
_RED = "#f87171"
_AMBER = "#fbbf24"
_PURPLE = "#a78bfa"
_BLUE = "#60a5fa"
_CYAN = "#2dd4bf"

_SPECIAL_ROW_BG = "#1d1d23"

# Metrics where a decrease is good (delta colored green when negative).
_LOWER_IS_BETTER = {"unsubPercent"}


def _esc(v: Any) -> str:
    return html_lib.escape(str(v))


def _fmt(value: float | None, fmt: str) -> str:
    if value is None:
        return "—"
    if fmt == "pct":
        return f"{value * 100:.1f}%"
    if fmt == "ratio":
        return f"{value:.1f}"
    return f"{int(round(value)):,}"


def _fmt_delta(cur: float | None, prev: float | None, fmt: str, key: str = "") -> str:
    if cur is None or prev is None:
        return f"<span style='color:{_MUTED}'>—</span>"
    diff = cur - prev
    mag = abs(diff)
    # Collapse deltas that would display as 0 (e.g. "▼0.0pp") into ±0.
    if fmt == "pct":
        rounded_zero = round(mag * 100, 1) == 0
    elif fmt == "ratio":
        rounded_zero = round(mag, 1) == 0
    else:
        rounded_zero = int(round(mag)) == 0
    if rounded_zero:
        return f"<span style='color:{_MUTED}'>±0</span>"
    good = diff < 0 if key in _LOWER_IS_BETTER else diff > 0
    color = _GREEN if good else _RED
    arrow = "&#9650;" if diff > 0 else "&#9660;"
    if fmt == "pct":
        text = f"{mag * 100:.1f}pp"
    elif fmt == "ratio":
        text = f"{mag:.1f}"
    else:
        text = f"{int(round(mag)):,}"
    return f"<span style='color:{color};font-weight:600'>{arrow}{text}</span>"


def _eyebrow(text: str, color: str, size: int = 11) -> str:
    return (
        f"<div style='font-size:{size}px;font-weight:700;letter-spacing:2px;"
        f"text-transform:uppercase;color:{color};margin:0 0 12px 0'>{text}</div>"
    )


def _card(inner: str) -> str:
    return (
        f"<div style='background:{_CARD};border:1px solid {_CARD_BORDER};"
        f"border-radius:10px;padding:16px 18px;margin:0 0 16px 0'>{inner}</div>"
    )


def _section_header(text: str, color: str) -> str:
    return (
        f"<div style='font-size:12px;font-weight:700;letter-spacing:2px;"
        f"text-transform:uppercase;color:{color};margin:26px 0 12px 2px'>{text}</div>"
    )


def _kv(label: str, value: str, note: str | None = None, value_color: str = _TEXT) -> str:
    note_html = (
        f" <span style='font-size:11px;color:{_MUTED};font-weight:600'>{note}</span>"
        if note else ""
    )
    return (
        f"<table cellpadding='0' cellspacing='0' style='width:100%;border-collapse:collapse'>"
        f"<tr><td style='padding:7px 0;font-size:13px;color:{_MUTED};"
        f"border-bottom:1px solid {_ROW_BORDER}'>{label}</td>"
        f"<td style='padding:7px 0;font-size:13px;color:{value_color};font-weight:700;"
        f"text-align:right;border-bottom:1px solid {_ROW_BORDER};white-space:nowrap'>"
        f"{value}{note_html}</td></tr></table>"
    )


def _insight_line(text: str | None) -> str:
    if not text:
        return ""
    return (
        f"<div style='margin-top:12px;padding:8px 12px;border-left:3px solid {_CYAN};"
        f"background:#1a1f1f;border-radius:0 6px 6px 0;font-size:12px;"
        f"line-height:1.5;color:{_TEXT}'>"
        f"<span style='color:{_CYAN};font-weight:700;letter-spacing:1px;font-size:10px'>"
        f"AI INSIGHT&nbsp;&nbsp;</span>{_esc(text)}</div>"
    )


_TH = (
    f"padding:6px 10px;text-align:left;font-size:10px;color:{_MUTED};font-weight:700;"
    f"letter-spacing:1px;text-transform:uppercase;border-bottom:2px solid {_CARD_BORDER};"
    "white-space:nowrap"
)
_TD = (
    f"padding:7px 10px;font-size:13px;color:{_TEXT};"
    f"border-bottom:1px solid {_ROW_BORDER};white-space:nowrap"
)


def _table(headers: list[str], rows: list[Any]) -> str:
    """rows: list of cell-lists, or (cell-list, bg-color) tuples for tinted rows."""
    head = "".join(f"<th style='{_TH}'>{h}</th>" for h in headers)
    body_parts = []
    for row in rows:
        bg = None
        if isinstance(row, tuple):
            row, bg = row
        td = _TD + (f";background:{bg}" if bg else "")
        body_parts.append("<tr>" + "".join(f"<td style='{td}'>{c}</td>" for c in row) + "</tr>")
    return (
        "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%'>"
        f"<tr>{head}</tr>{''.join(body_parts)}</table>"
    )


def _scroll(inner: str) -> str:
    return f"<div style='overflow-x:auto'>{inner}</div>"


def _webinar_label(w: dict[str, Any] | None) -> str:
    if not w:
        return "—"
    label = f"W{w.get('number')}"
    if w.get("variantLabel"):
        label += f" · {w['variantLabel']}"
    if w.get("date"):
        label += f" ({w['date']})"
    return _esc(label)


def _rate(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return a / b


# ── Header / flags / summary ───────────────────────────────────────────────

_DAY_NAMES = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
              "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}


def _brand_header(schedule: dict[str, Any] | None) -> str:
    generated = datetime.now(dt_timezone.utc).strftime("%B %d, %Y")
    sub = f"Generated {generated}"
    if schedule:
        day = _DAY_NAMES.get(schedule.get("day_of_week", ""), "")
        if day:
            sub += (f" &middot; Sent every {day} "
                    f"{schedule.get('hour_local', 0):02d}:{schedule.get('minute_local', 0):02d} "
                    f"{_esc(schedule.get('timezone', ''))}")
    return (
        f"<div style='font-size:11px;font-weight:700;letter-spacing:4px;color:{_MUTED};"
        "text-transform:uppercase;margin:0 0 6px 0'>Quantum Scaling</div>"
        f"<h1 style='font-size:22px;margin:0 0 6px;color:{_TEXT}'>Webinar Intelligence Report</h1>"
        f"<div style='font-size:12px;color:{_MUTED};margin-bottom:22px'>{sub}</div>"
    )


def _flags_card(flags: list[str]) -> str:
    real = [f for f in flags if f.strip().lower() not in ("none", "- none")]
    if not real:
        return ""
    bullets = "".join(
        f"<div style='font-size:13px;line-height:1.6;margin:0 0 8px 0;color:{_AMBER}'>"
        f"&#9888;&#65039;&nbsp;{_esc(f)}</div>"
        for f in real
    )
    return _card(_eyebrow("Anomaly Flags", _AMBER) + bullets)


def _bullet(text: str) -> str:
    return (
        f"<div style='font-size:13px;line-height:1.55;margin:0 0 7px 0;color:{_TEXT}'>"
        f"<span style='color:{_MUTED}'>&bull;&nbsp;</span>{_esc(text)}</div>"
    )


def _summary_card(sections: dict[str, Any]) -> str:
    blocks: list[str] = []
    styling = [
        ("HIGHLIGHTS", "Highlights", _GREEN),
        ("MEANINGFUL CHANGES", "Meaningful Changes", _BLUE),
        ("ACTIONS", "Actions for Next Week", _PURPLE),
    ]
    for key, title, color in styling:
        bullets = sections.get(key) or []
        if not bullets:
            continue
        margin = "0 0 8px 0" if not blocks else "16px 0 8px 0"
        blocks.append(
            f"<div style='font-size:11px;font-weight:700;letter-spacing:2px;color:{color};"
            f"text-transform:uppercase;margin:{margin}'>{title}</div>"
            + "".join(_bullet(b) for b in bullets)
        )
    if not blocks:
        return ""
    return _card("".join(blocks))


# ── Section 1 — campaign performance ───────────────────────────────────────

def _overview_card(current: dict[str, Any], insight: str | None) -> str:
    m = current.get("summary") or {}
    regs = m.get("totalRegs")
    att10 = m.get("total10MinPlus")
    att30 = m.get("total30MinPlus")
    bookings = m.get("totalBookings")
    rows = [
        _kv("Webinar Date", _esc(current.get("date") or "—")),
        _kv("Total Registrations", _fmt(regs, "int")),
        _kv("Total Attended (live)", _fmt(m.get("totalAttended"), "int")),
        _kv("10min+ Att Rate", _fmt(_rate(att10, regs), "pct"),
            note=f"{_fmt(att10, 'int')} stayed 10min+", value_color=_GREEN),
        _kv("30min+ Att Rate", _fmt(_rate(att30, regs), "pct"),
            note=f"{_fmt(att30, 'int')} stayed 30min+", value_color=_GREEN),
        _kv("30m Retention (of 10m viewers)", _fmt(_rate(att30, att10), "pct"),
            value_color=_AMBER),
        _kv("Booking Rate (of 10m+)", _fmt(_rate(bookings, att10), "pct"),
            note=f"{_fmt(bookings, 'int')} booked &divide; {_fmt(att10, 'int')} stayed 10min+",
            value_color=_GREEN),
    ]
    # Self-reg vs outreach attendance quality (10m+ of regs per cohort).
    sr_marked, sr_10m = m.get("selfRegMarked"), m.get("selfReg10MinPlus")
    footnote = ""
    if sr_marked and regs:
        sr_rate = _rate(sr_10m, sr_marked)
        out_rate = _rate((att10 or 0) - (sr_10m or 0), regs - sr_marked)
        footnote = (
            f"<div style='margin-top:10px;font-size:11px;color:{_MUTED}'>"
            f"Self-reg 10min+ rate: <span style='color:{_GREEN};font-weight:700'>{_fmt(sr_rate, 'pct')}</span>"
            f" &middot; Outreach 10min+ rate: <span style='color:{_CYAN};font-weight:700'>{_fmt(out_rate, 'pct')}</span>"
            "</div>"
        )
    return _card(
        _eyebrow("Webinar Overview + Retention Funnel", _GREEN)
        + "".join(rows) + footnote + _insight_line(insight)
    )


def _wow_card(current: dict[str, Any], previous: dict[str, Any] | None) -> str:
    cur_m = current.get("summary") or {}
    prev_m = (previous or {}).get("summary") or {}
    cur_col = f"W{current.get('number')} <span style='color:{_GREEN};font-size:9px'>(latest)</span>"
    prev_col = f"W{previous.get('number')}" if previous else "Previous"
    rows = []
    for key, label, fmt in _HEADLINE_METRICS:
        rows.append([
            _esc(label),
            f"<b>{_fmt(cur_m.get(key), fmt)}</b>",
            _fmt(prev_m.get(key), fmt) if previous else "—",
            _fmt_delta(cur_m.get(key), prev_m.get(key), fmt, key) if previous else "—",
        ])
    title = f"Week-over-week: {_webinar_label(previous)} &rarr; {_webinar_label(current)}" if previous \
        else f"Headline stats: {_webinar_label(current)}"
    return _card(_eyebrow(title, _GREEN) + _scroll(_table(["Metric", cur_col, prev_col, "Change"], rows)))


def _variants_card(variants: list[dict[str, Any]]) -> str:
    headers = ["Metric"] + [_esc(v.get("variantLabel") or "Primary") for v in variants]
    rows = []
    for key, label, fmt in _HEADLINE_METRICS:
        row = [_esc(label)]
        for v in variants:
            row.append(_fmt((v.get("summary") or {}).get(key), fmt))
        rows.append(row)
    return _card(_eyebrow("A/B Variants", _GREEN) + _scroll(_table(headers, rows)))


def _senders_card(senders: list[dict[str, Any]], senders_prev: dict[str, dict[str, Any]],
                  insight: str | None) -> str:
    rows = []
    for g in senders:
        m = g["metrics"]
        prev_m = senders_prev.get(g["label"])
        reg_rate = m.get("invitedToRegPercent")
        rows.append([
            _esc(g["label"]),
            _fmt(m.get("invited"), "int"),
            _fmt(m.get("totalRegs"), "int"),
            f"<span style='color:{_GREEN};font-weight:700'>{_fmt(reg_rate, 'pct')}</span>",
            _fmt_delta(reg_rate, prev_m.get("invitedToRegPercent") if prev_m else None, "pct"),
            _fmt(_rate(m.get("total10MinPlus"), m.get("totalRegs")), "pct"),
            _fmt(m.get("totalBookings"), "int"),
        ])
    table = _table(
        ["Sender", "Invited", "Regs", "Reg %", "vs W-1", "Att% 10m+", "Bookings"], rows
    )
    return _card(_eyebrow("Sender Performance", _GREEN) + _scroll(table) + _insight_line(insight))


def _segment_leaderboard_card(segments: dict[str, Any], all_time: dict[str, Any] | None,
                              current: dict[str, Any], insight: str | None) -> str:
    special = list(segments.get("special") or [])
    buckets = list(segments.get("buckets") or [])
    totals = segments.get("totals")

    def _name_cell(s: dict[str, Any], *, bold: bool = False, muted: bool = False) -> str:
        name = s.get("label") or "—"
        if bold:
            return f"<b>{_esc(name)}</b>"
        if muted:
            return f"<i style='color:{_MUTED}'>{_esc(name)}</i>"
        cell = _esc(name)
        quality = s.get("quality")
        if quality:
            cell += f" <span style='color:{_MUTED};font-size:11px'>({_esc(quality)})</span>"
        return cell

    def _build(headers: list[str], row_fn, sort_key) -> str:
        rows: list[Any] = [(row_fn(s, muted=True), _SPECIAL_ROW_BG) for s in special]
        rows.extend(row_fn(s) for s in sorted(buckets, key=sort_key, reverse=True))
        if totals:
            rows.append(row_fn(totals, bold=True))
        return _scroll(_table(headers, rows))

    def _reg_row(s, *, bold=False, muted=False):
        inv, regs = s.get("invites") or 0, s.get("regs") or 0
        return [_name_cell(s, bold=bold, muted=muted), f"{int(inv):,}", f"{int(regs):,}",
                _fmt(_rate(regs, inv), "pct") if not muted else "—"]

    def _att_row(s, *, bold=False, muted=False):
        regs, att10 = s.get("regs") or 0, s.get("attendees10m") or 0
        return [_name_cell(s, bold=bold, muted=muted), f"{int(regs):,}", f"{int(att10):,}",
                _fmt(_rate(att10, regs), "pct")]

    def _book_row(s, *, bold=False, muted=False):
        att10, bookings = s.get("attendees10m") or 0, s.get("bookings") or 0
        return [_name_cell(s, bold=bold, muted=muted), f"{int(att10):,}", f"{int(bookings):,}",
                _fmt(_rate(bookings, att10), "pct")]

    parts = [_eyebrow("Segment Leaderboard", _GREEN)]
    sub = f"<div style='font-size:11px;font-weight:700;letter-spacing:1px;color:{_CYAN};text-transform:uppercase;margin:%s 0 8px 0'>%s</div>"
    parts.append(sub % ("0", f"{_webinar_label(current)} — by Registrations"))
    parts.append(_build(["Segment", "Invites", "Regs", "Inv&gt;Reg %"], _reg_row,
                        lambda s: (_rate(s.get("regs") or 0, s.get("invites") or 0) or 0, s.get("regs") or 0)))
    parts.append(sub % ("16px", "By Attendance (10m+ of Regs)"))
    parts.append(_build(["Segment", "Regs", "10m+", "Att 10m+ %"], _att_row,
                        lambda s: (_rate(s.get("attendees10m") or 0, s.get("regs") or 0) or 0, s.get("attendees10m") or 0)))
    parts.append(sub % ("16px", "By Bookings"))
    parts.append(_build(["Segment", "10m+", "Bookings", "Book/10m %"], _book_row,
                        lambda s: (s.get("bookings") or 0, _rate(s.get("bookings") or 0, s.get("attendees10m") or 0) or 0)))

    if special:
        parts.append(
            f"<div style='font-size:11px;color:{_MUTED};margin-top:8px'>"
            "Nonjoiners / No List Data are synthetic cohorts without invite attribution — "
            "shown separately because their rates aren't comparable to bucket rates.</div>"
        )

    if all_time:
        def _at_rows(entries):
            return [[_esc(e["label"]), f"{int(e['invites']):,}", f"{int(e['regs']):,}",
                     _fmt(e["rate"], "pct")] for e in entries]
        parts.append(
            f"<div style='font-size:11px;font-weight:700;letter-spacing:1px;color:{_GREEN};"
            "text-transform:uppercase;margin:16px 0 8px 0'>&#9650; Top Segments (All-Time)</div>"
        )
        parts.append(_scroll(_table(["Segment", "Invited", "Regs", "Reg %"], _at_rows(all_time["top"]))))
        parts.append(
            f"<div style='font-size:11px;font-weight:700;letter-spacing:1px;color:{_RED};"
            "text-transform:uppercase;margin:16px 0 8px 0'>&#9660; Bottom Segments (All-Time)</div>"
        )
        parts.append(_scroll(_table(["Segment", "Invited", "Regs", "Reg %"], _at_rows(all_time["bottom"]))))
        parts.append(
            f"<div style='font-size:11px;color:{_MUTED};margin-top:6px'>"
            f"All-time tables include buckets with &ge;{ALL_TIME_MIN_INVITES:,} invites.</div>"
        )

    parts.append(_insight_line(insight))
    return _card("".join(parts))


# ── Section 2 — copy performance ───────────────────────────────────────────

def _copy_highlight(title: str, color: str, symbol: str, g: dict[str, Any]) -> str:
    m = g["metrics"]
    att10_rate = _rate(m.get("total10MinPlus"), m.get("totalRegs"))
    stat = (
        f"Inv: <b>{_fmt(m.get('invited'), 'int')}</b> &middot; "
        f"Regs: <b>{_fmt(m.get('totalRegs'), 'int')}</b> &middot; "
        f"Reg%: <span style='color:{color};font-weight:700'>{_fmt(m.get('invitedToRegPercent'), 'pct')}</span> &middot; "
        f"Att 10m: <b>{_fmt(att10_rate, 'pct')}</b> &middot; "
        f"Bookings: <b>{_fmt(m.get('totalBookings'), 'int')}</b>"
    )
    return (
        f"<div style='font-size:10px;font-weight:700;letter-spacing:1px;color:{color};"
        f"text-transform:uppercase;margin:14px 0 6px 0'>{symbol} {title}</div>"
        f"<div style='border:1px solid {_ROW_BORDER};border-radius:8px;padding:10px 14px'>"
        f"<div style='font-size:13px;font-weight:700;color:{_TEXT};margin-bottom:4px'>{_esc(g['label'])}</div>"
        f"<div style='font-size:11px;color:{_MUTED}'>{stat}</div></div>"
    )


def _copy_cards(title_copies: list[dict[str, Any]], insight: str | None) -> str:
    if not title_copies:
        return ""
    with_volume = [g for g in title_copies if (g["metrics"].get("invited") or 0) >= 1000]
    pool = with_volume or title_copies
    best_reg = max(pool, key=lambda g: g["metrics"].get("invitedToRegPercent") or 0)
    best_att = max(pool, key=lambda g: _rate(g["metrics"].get("total10MinPlus"),
                                             g["metrics"].get("totalRegs")) or 0)
    worst = min(pool, key=lambda g: g["metrics"].get("invitedToRegPercent") or 0)
    parts = [
        _eyebrow("Title Copy — Reg Rate + Attendance Quality", _PURPLE),
        _copy_highlight("Best Registration Rate", _GREEN, "&#9650;", best_reg).replace("margin:14px", "margin:0px", 1),
        _copy_highlight("Best Attendance Quality (10m+ rate)", _CYAN, "&#9733;", best_att),
    ]
    if worst is not best_reg:
        parts.append(_copy_highlight("Weakest Performer", _RED, "&#9660;", worst))
    parts.append(_insight_line(insight))
    return _card("".join(parts))


def _group_table_card(title: str, groups: list[dict[str, Any]], first_col: str) -> str:
    if not groups:
        return ""
    rows = []
    for g in groups:
        m = g["metrics"]
        rows.append([
            _esc(g["label"]),
            str(g["listCount"]),
            _fmt(m.get("invited"), "int"),
            _fmt(m.get("totalRegs"), "int"),
            _fmt(m.get("invitedToRegPercent"), "pct"),
            _fmt(m.get("yesMarked"), "int"),
            _fmt(m.get("maybeMarked"), "int"),
            _fmt(m.get("totalBookings"), "int"),
        ])
    table = _table(
        [first_col, "Lists", "Invited", "Regs", "Inv&gt;Reg %", "Yes", "Maybe", "Bookings"], rows
    )
    return _card(_eyebrow(title, _PURPLE) + _scroll(table))


# ── Sections 3 + 4 — sales pipeline ────────────────────────────────────────

def _sales_card(current: dict[str, Any], insight: str | None) -> str:
    m = current.get("summary") or {}
    att10 = m.get("total10MinPlus")
    bookings = m.get("totalBookings")
    rows = [
        _kv("Bookings (attributed)", _fmt(bookings, "int")),
        _kv("Calls with date passed", _fmt(m.get("totalCallsDatePassed"), "int")),
        _kv("Confirmed", _fmt(m.get("confirmed"), "int")),
        _kv("Showed", _fmt(m.get("shows"), "int"),
            note=f"{_fmt(m.get('showPercent'), 'pct')} show rate", value_color=_GREEN),
        _kv("No-Show", _fmt(m.get("noShows"), "int"), value_color=_AMBER),
        _kv("Cancelled", _fmt(m.get("canceled"), "int"), value_color=_AMBER),
        _kv("Deals Won", _fmt(m.get("won"), "int"),
            note=f"{_fmt(m.get('closeRatePercent'), 'pct')} close rate", value_color=_GREEN),
        _kv("Disqualified", _fmt(m.get("disqualified"), "int"), value_color=_RED),
        _kv("Qualified", _fmt(m.get("qualified"), "int"),
            note=f"{_fmt(m.get('qualPercent'), 'pct')} of shows"),
    ]
    retention = (
        f"<div style='margin-top:12px;font-size:12px;color:{_MUTED}'>Retention &rarr; Booking:<br>"
        f"<span style='color:{_TEXT};font-weight:700'>{_fmt(att10, 'int')} stayed 10min+ &rarr; "
        f"{_fmt(bookings, 'int')} booked = "
        f"<span style='color:{_GREEN}'>{_fmt(_rate(bookings, att10), 'pct')}</span></span></div>"
    )
    note = (
        f"<div style='font-size:11px;color:{_MUTED};font-style:italic;margin-bottom:10px'>"
        "Pipeline numbers are ~1 week behind the webinar — calls are still being scheduled and held.</div>"
    )
    return _card(_eyebrow("GHL Pipeline — 1 Week Lag", _BLUE) + note + "".join(rows)
                 + retention + _insight_line(insight))


def _prev_pipeline_card(previous: dict[str, Any]) -> str:
    m = previous.get("summary") or {}
    rows = [
        _kv("Total Bookings (attributed)", _fmt(m.get("totalBookings"), "int")),
        _kv("Showed", _fmt(m.get("shows"), "int"),
            note=f"{_fmt(m.get('showPercent'), 'pct')} show rate"),
        _kv("No-Show", _fmt(m.get("noShows"), "int"), value_color=_AMBER),
        _kv("Cancelled", _fmt(m.get("canceled"), "int"), value_color=_AMBER),
        _kv("Deals Won", _fmt(m.get("won"), "int"), value_color=_GREEN),
        _kv("Disqualified", _fmt(m.get("disqualified"), "int"), value_color=_RED),
        _kv("Qualified", _fmt(m.get("qualified"), "int")),
    ]
    return _card(_eyebrow("Close Data — 2 Week Lag", _AMBER) + "".join(rows))


# ── Assembly ───────────────────────────────────────────────────────────────

def render_report_html(data: dict[str, Any], narrative: str | None,
                       schedule: dict[str, Any] | None = None) -> str:
    current = data["current"]
    previous = data["previous"]

    parsed: dict[str, Any] = _parse_narrative(narrative) if narrative else {}
    insights: dict[str, str] = parsed.get("insights") or {}

    body: list[str] = [_brand_header(schedule)]

    flags_html = _flags_card(parsed.get("FLAGS") or [])
    if flags_html:
        body.append(flags_html)
    summary_html = _summary_card(parsed)
    if summary_html:
        body.append(summary_html)

    # Section 1 — campaign performance
    body.append(_section_header(
        f"Section 1 — {_webinar_label(current)} Campaign Performance", _GREEN))
    body.append(_overview_card(current, insights.get("Overview")))
    body.append(_wow_card(current, previous))
    if data["variants"]:
        body.append(_variants_card(data["variants"]))
    if data["senders"]:
        body.append(_senders_card(data["senders"], data.get("sendersPrev") or {},
                                  insights.get("Senders")))
    seg = data["segments"]
    if seg and (seg.get("buckets") or seg.get("special")):
        body.append(_segment_leaderboard_card(seg, data.get("allTime"), current,
                                              insights.get("Segments")))

    # Section 2 — copy performance
    if data["titleCopies"] or data["descCopies"]:
        body.append(_section_header(
            f"Section 2 — Copy Performance ({_webinar_label(current)})", _PURPLE))
        body.append(_copy_cards(data["titleCopies"], insights.get("Copies")))
        body.append(_group_table_card("Title Copy Variants", data["titleCopies"], "Title copy"))
        body.append(_group_table_card("Description Copy Variants", data["descCopies"], "Description copy"))

    # Section 3 — sales call outcomes (current webinar)
    body.append(_section_header(
        f"Section 3 — {_webinar_label(current)} Sales Call Outcomes", _BLUE))
    body.append(_sales_card(current, insights.get("Sales")))

    # Section 4 — previous webinar's final pipeline status
    if previous:
        body.append(_section_header(
            f"Section 4 — {_webinar_label(previous)} Final Pipeline Status", _AMBER))
        body.append(_prev_pipeline_card(previous))

    footer = (
        f"<div style='text-align:center;font-size:11px;color:{_MUTED};margin-top:26px;"
        f"padding-top:14px;border-top:1px solid {_CARD_BORDER}'>"
        "Quantum Scaling &middot; Webinar Studio &middot; Weekly Intelligence Report<br>"
        f"Auto-generated &middot; <a href='{STATISTICS_PAGE_URL}' "
        f"style='color:{_BLUE}'>Open the Statistics dashboard</a></div>"
    )
    body.append(footer)

    inner = (
        "<div style='max-width:700px;margin:0 auto;padding:28px 22px;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        f"color:{_TEXT}'>" + "".join(body) + "</div>"
    )
    # Dark canvas: bgcolor attribute + inline style for broad email-client support.
    return (
        f"<table width='100%' cellpadding='0' cellspacing='0' bgcolor='{_BG}' "
        f"style='background:{_BG}'><tr><td>{inner}</td></tr></table>"
    )


# ---------------------------------------------------------------------------
# Per-webinar report email (services.webinar_report artifact → email HTML)
# ---------------------------------------------------------------------------

def _pct(v: float | None) -> str:
    return _fmt(v, "pct")


def _int(v: float | None) -> str:
    return _fmt(v, "int")


def _r1(v: float | None) -> str:
    return "—" if v is None else f"{v:,.1f}"


def _report_scorecard_card(payload: dict[str, Any]) -> str:
    sc = payload.get("scorecard") or {}
    cur = sc.get("current") or {}
    ball = sc.get("baselineAll") or {}
    b4w = sc.get("baseline4w") or {}

    rows_spec = [
        ("Invited", "invited", "int"),
        ("Net-new registrations", "netNewRegs", "int"),
        ("Net-new reg rate", "regRate", "pct"),
        ("Non-joiner registrations", "nonjoinerRegs", "int"),
        ("No-list-data registrations", "noListDataRegs", "int"),
        ("Total registrations", "totalRegs", "int"),
        ("Yes marks", "yesMarked", "int"),
        ("Maybe marks", "maybeMarked", "int"),
        ("Live attendance", "totalAttended", "int"),
        ("Attendance % of regs", "attendRateOfRegs", "pct"),
        ("Attendees / 10k invited", "attendPer10kInvited", "ratio"),
        ("Booked contacts", "uniqueBookers", "int"),
    ]
    rows = []
    for label, key, fmt in rows_spec:
        c = cur.get(key)
        rows.append([
            label,
            f"<b>{_fmt(c, fmt)}</b>",
            _fmt(ball.get(key), fmt),
            _fmt_delta(c, ball.get(key), fmt, key),
            _fmt(b4w.get(key), fmt),
            _fmt_delta(c, b4w.get(key), fmt, key),
        ])
    n_all = ball.get("webinarCount") or 0
    n_4w = b4w.get("webinarCount") or 0
    return _card(
        _eyebrow("Scorecard — vs average webinar", _GREEN)
        + _scroll(_table(
            ["Metric", "This webinar", f"Last-{n_all} avg", "Δ", f"4-week avg ({n_4w})", "Δ"],
            rows,
        ))
    )


def _report_funnel_cards(payload: dict[str, Any]) -> str:
    titles = {
        "employeeSize": "Employee size",
        "industry": "Industry",
        "geography": "Geography",
        "segments": "Segments (buckets)",
    }
    out: list[str] = []
    for dim in ("segments", "industry", "geography", "employeeSize"):
        block = (payload.get("funnels") or {}).get(dim)
        if not block or not block.get("cells"):
            continue
        rows = []
        for cell in block["cells"][:8]:
            c = cell.get("current") or {}
            b = cell.get("baseline") or {}
            rows.append([
                _esc(str(cell.get("key"))[:38]),
                _int(c.get("invited")),
                f"{_pct(c.get('regRate'))} {_fmt_delta(c.get('regRate'), b.get('regRate'), 'pct')}",
                f"{_pct(c.get('attPctOfRegs'))} {_fmt_delta(c.get('attPctOfRegs'), b.get('attPctOfRegs'), 'pct')}",
                f"{_r1(c.get('attendeesPer10kInv'))} {_fmt_delta(c.get('attendeesPer10kInv'), b.get('attendeesPer10kInv'), 'ratio')}",
            ])
        out.append(_card(
            _eyebrow(titles[dim], _BLUE)
            + f"<div style='font-size:11px;color:{_MUTED};margin:-6px 0 10px 0'>Δ vs the last-{block.get('baselineWebinarCount') or 0}-webinar average</div>"
            + _scroll(_table(
                [titles[dim], "Invited", "Reg rate", "Att % of regs", "Att / 10k inv"],
                rows,
            ))
        ))
    return "".join(out)


def _report_bookings_card(payload: dict[str, Any]) -> str:
    bk = payload.get("bookings") or {}
    if not bk:
        return ""
    q = bk.get("quality") or {}
    st = bk.get("callStatus") or {}
    origin = bk.get("origin") or {}
    inner = _eyebrow("Bookings deep-dive", _PURPLE)
    inner += _kv("Unique booked contacts", f"<b>{_int(bk.get('uniqueBookedContacts'))}</b>",
                 note="rebooked contact counts once")
    inner += _kv(
        "Call status",
        f"{st.get('showed', 0)} showed &middot; {st.get('noShow', 0)} no-show &middot; "
        f"{st.get('cancelled', 0)} cancelled &middot; {st.get('confirmed', 0)} upcoming",
    )
    inner += _kv(
        "Lead quality (rated)",
        f"{q.get('great', 0)} Great &middot; {q.get('ok', 0)} Ok &middot; "
        f"{q.get('barely', 0)} Barely &middot; {q.get('bad', 0)} Bad/DQ",
        note=f"{bk.get('rated', 0)} of {bk.get('uniqueBookedContacts', 0)} rated",
    )
    inner += _kv("Implied close rate", _pct(bk.get("impliedCloseRate")),
                 note="Great 25% · Ok 13% · Barely 5%")
    inner += _kv(
        "Booking origin",
        f"{origin.get('netNew', 0)} net-new &middot; {origin.get('nonjoiner', 0)} non-joiner &middot; "
        f"{origin.get('noListData', 0)} no-list &middot; {origin.get('notRegistrant', 0)} not registered",
    )
    sources = bk.get("leadSources") or []
    if sources:
        inner += "<div style='height:10px'></div>" + _scroll(_table(
            ["Lead source", "Booked"],
            [[_esc(s["source"]), _int(s["count"])] for s in sources[:5]],
        ))
    return _card(inner)


def _report_nonjoiners_card(payload: dict[str, Any]) -> str:
    nj = payload.get("nonjoiners") or {}
    if not nj:
        return ""
    inner = _eyebrow("Non-joiner package", _CYAN)
    inner += _kv("Pool (last %d webinars)" % (nj.get("windowWebinars") or 6),
                 _int(nj.get("poolSize")))
    inner += _kv("Re-registered", f"{_int(nj.get('regs'))} ({_pct(nj.get('regRate'))} of pool)")
    inner += _kv("Attended live", f"{_int(nj.get('attended'))} ({_pct(nj.get('attendRateOfRegs'))} of NJ regs)")
    inner += _kv("Net-new attendance rate", _pct(nj.get("netNewAttendRateOfRegs")),
                 note="comparison")
    inner += _kv("Net-new reg rate of invited", _pct(nj.get("netNewRegRateOfInvited")),
                 note="comparison")
    return _card(inner)


def _report_insights_card(insights: list[dict[str, Any]] | None, ai_error: str | None) -> str:
    banner = (
        f"<div style='margin:0 0 12px 0;padding:8px 12px;border:1px solid {_AMBER};"
        f"border-radius:6px;font-size:11px;color:{_AMBER};line-height:1.5'>"
        "&#9888;&#65039; AI-generated insights (beta) — not always accurate; verify the numbers "
        "above before acting on them.</div>"
    )
    if not insights:
        note = _esc(ai_error or "Insights unavailable for this report.")
        return _card(_eyebrow("AI Insights", _AMBER) + banner
                     + f"<div style='font-size:12px;color:{_MUTED}'>{note}</div>")
    blocks: list[str] = []
    for group in insights:
        blocks.append(
            f"<div style='font-size:12px;font-weight:700;letter-spacing:1px;color:{_TEXT};"
            f"text-transform:uppercase;margin:{'0' if not blocks else '14px'} 0 6px 0'>"
            f"{_esc(group.get('title', ''))}</div>"
            + "".join(_bullet(b) for b in (group.get("bullets") or []))
        )
    return _card(_eyebrow("AI Insights", _AMBER) + banner + "".join(blocks))


def render_webinar_report_email(report: dict[str, Any],
                                schedule: dict[str, Any] | None = None) -> str:
    """Email-safe HTML for a stored per-webinar report artifact."""
    from config import settings as app_settings

    payload = report.get("payload") or {}
    webinar_id = report.get("webinarId") or payload.get("webinarId") or ""
    label = f"W{payload.get('number')}"
    if payload.get("variantLabel"):
        label += f" · {payload['variantLabel']}"
    if payload.get("date"):
        label += f" ({payload['date']})"

    report_url = f"{app_settings.APP_BASE_URL.rstrip('/')}/statistics/report/{webinar_id}"

    body: list[str] = [_brand_header(schedule)]
    body.append(
        f"<div style='font-size:14px;color:{_TEXT};margin:-10px 0 16px 0'>"
        f"Webinar report — <b>{_esc(label)}</b> &middot; "
        f"<a href='{report_url}' style='color:{_BLUE}'>Open the interactive report</a></div>"
    )

    body.append(_report_scorecard_card(payload))
    body.append(_section_header("Funnel breakdowns — vs last-10-webinar average", _BLUE))
    body.append(_report_funnel_cards(payload))
    body.append(_section_header("Sales & non-joiners", _PURPLE))
    body.append(_report_bookings_card(payload))
    body.append(_report_nonjoiners_card(payload))
    body.append(_section_header("Insights", _AMBER))
    body.append(_report_insights_card(report.get("insights"), report.get("aiError")))

    caveats = payload.get("caveats") or []
    if caveats:
        body.append(_card(
            _eyebrow("Data notes", _MUTED)
            + "".join(
                f"<div style='font-size:11px;color:{_MUTED};line-height:1.5;margin:0 0 5px 0'>"
                f"&bull; {_esc(c)}</div>" for c in caveats
            )
        ))

    footer = (
        f"<div style='text-align:center;font-size:11px;color:{_MUTED};margin-top:26px;"
        f"padding-top:14px;border-top:1px solid {_CARD_BORDER}'>"
        "Quantum Scaling &middot; Webinar Studio &middot; Weekly Report<br>"
        f"Auto-generated &middot; <a href='{report_url}' style='color:{_BLUE}'>"
        "Open the full report in Webinar Studio</a></div>"
    )
    body.append(footer)

    inner = (
        "<div style='max-width:700px;margin:0 auto;padding:28px 22px;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        f"color:{_TEXT}'>" + "".join(body) + "</div>"
    )
    return (
        f"<table width='100%' cellpadding='0' cellspacing='0' bgcolor='{_BG}' "
        f"style='background:{_BG}'><tr><td>{inner}</td></tr></table>"
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def send_weekly_report(*, test: bool = False, webinar_id: str | None = None) -> dict[str, Any]:
    """Generate + send the report. Returns {ok, message_id?, error?, ...}.

    Scheduled runs (test=False) respect `enabled` and the double-send guard;
    test sends bypass both and prefix the subject with [TEST]. Failures are
    recorded on report_settings.last_error (except test sends, which surface
    the error to the caller only).
    """
    settings = await get_report_settings()

    if not test:
        if not settings["enabled"]:
            logger.info("Weekly report: disabled — skipping")
            return {"ok": False, "error": "Reports are disabled"}
        last = settings.get("last_sent_at")
        if last:
            last_dt = datetime.fromisoformat(last)
            if datetime.now(dt_timezone.utc) - last_dt < timedelta(minutes=DOUBLE_SEND_GUARD_MINUTES):
                logger.info("Weekly report: sent %s — double-send guard, skipping", last)
                return {"ok": False, "error": "Already sent recently (double-send guard)"}

    async def _fail(msg: str) -> dict[str, Any]:
        logger.warning("Weekly report: %s", msg)
        if not test:
            await _record_send_result(sent_at=None, error=msg)
        return {"ok": False, "error": msg}

    recipients = [r for r in settings["recipients"] if r]
    if not recipients:
        return await _fail("No recipients configured")

    api_key = await _resolve_resend_key()
    if not api_key:
        return await _fail("Resend API key not configured (Connectors → Resend)")

    # The email body is the per-webinar report artifact (services.webinar_report):
    # normally pre-generated by the prep job 15 minutes earlier; regenerate
    # synchronously when missing or stale (no HTTP timeout in job context).
    from services import webinar_report

    try:
        target_id = webinar_id or await webinar_report.resolve_latest_passed_webinar_id()
        if not target_id:
            return await _fail("No passed webinar found to report on")

        report = await webinar_report.read_report(target_id)
        stale = True
        if report and report.get("generatedAt"):
            try:
                gen_dt = datetime.fromisoformat(report["generatedAt"])
                stale = datetime.now(dt_timezone.utc) - gen_dt > timedelta(hours=2)
            except Exception:
                stale = True
        if report is None or stale:
            logger.info("Weekly report: report for %s missing/stale — generating now", target_id)
            report = await webinar_report.generate_report(target_id) or report
        if report is None or not report.get("payload"):
            return await _fail("Failed to build the webinar report")
    except Exception as exc:
        logger.exception("Weekly report: failed to build report")
        return await _fail(f"Failed to build report data: {exc}")

    html = render_webinar_report_email(report, schedule=settings)

    current = report.get("payload") or {}
    # ASCII-only subject: non-ASCII chars (em dashes etc.) get RFC2047-encoded
    # in the header, which trips Gmail's "abnormal characters" spoof warning
    # on a young sending domain.
    subject = f"Webinar Report - W{current.get('number')}"
    if current.get("date"):
        subject += f" ({current['date']})"
    if test:
        subject = "[TEST] " + subject

    try:
        message_id = await resend_client.send_email(
            api_key,
            from_addr=settings["from_address"],
            to=recipients,
            subject=subject,
            html=html,
        )
    except resend_client.ResendError as exc:
        return await _fail(f"Resend send failed: {exc.message}")
    except Exception as exc:
        return await _fail(f"Resend send failed: {exc}")

    if not test:
        await _record_send_result(sent_at=datetime.now(dt_timezone.utc), error=None)
    logger.info(
        "Weekly report sent%s: W%s to %s (message %s)",
        " [test]" if test else "", current.get("number"), recipients, message_id,
    )
    return {
        "ok": True,
        "message_id": message_id,
        "webinar_number": current.get("number"),
        "recipients": recipients,
        "narrative_included": report.get("insights") is not None,
    }
