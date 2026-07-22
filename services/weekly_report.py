"""Weekly webinar report — data assembly, AI narrative, HTML render, Resend send.

Pipeline (shared by the scheduler job and POST /reports/send-test):
    get_report_settings() → build_report_data() → generate_narrative() →
    render_report_html() → resend_client.send_email()

Report scope: the latest *passed* webinar (Statistics-page definition) vs the
previous webinar number, with per-bucket segments, A/B variant comparison,
copy-variant and sender aggregations folded from the per-list stats rows.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from sqlalchemy import select

from db.models import ConnectorCredential, ReportSettings
from db.session import AsyncSessionLocal
from integrations import resend_client
from services.statistics import (
    _SUM_KEYS,
    _is_passed_webinar,
    compute_derived_metrics,
    get_statistics_segments,
    get_statistics_webinar_list,
    get_statistics_webinar_one,
)

logger = logging.getLogger(__name__)

RESEND_PROVIDER = "resend"
VALID_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Guard against double-sends when the scheduler reloads near the trigger time
# (settings PATCH re-registers the cron job; APScheduler may re-fire).
DOUBLE_SEND_GUARD_MINUTES = 30

NARRATIVE_MAX_TOKENS = 900
NARRATIVE_TIMEOUT_SECONDS = 90.0

STATISTICS_PAGE_URL = "https://competeiq-frontend.onrender.com/statistics"


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

    # Segments cover the whole webinar number (all variants) so bucket totals
    # tie out to what the Segments tab shows for this webinar.
    group_ids = [current_id] + [s["webinarId"] for s in selection["siblings"]]
    try:
        segments = await get_statistics_segments("auto", group_ids)
    except Exception as exc:
        logger.warning("Weekly report: segments unavailable: %s", exc)
        segments = None

    # Copy + sender folds across the whole variant group's list rows.
    all_rows: list[dict[str, Any]] = []
    for w in (variants or [current]):
        all_rows.extend(w.get("rows") or [])

    return {
        "current": current,
        "previous": previous,
        "variants": variants,  # empty when no A/B siblings
        "segments": segments,
        "titleCopies": _fold_rows(all_rows, _copy_key("titleCopy"), _copy_label("titleCopy")),
        "descCopies": _fold_rows(all_rows, _copy_key("descCopy"), _copy_label("descCopy")),
        "senders": _fold_rows(
            all_rows,
            lambda r: r.get("sendInfo"),
            lambda r: r.get("sendInfo") or "Unknown",
        ),
    }


# ---------------------------------------------------------------------------
# AI narrative
# ---------------------------------------------------------------------------

# Headline metrics shared by the narrative prompt and the email table.
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
    return {key: summary.get(key) for key, _, _ in _HEADLINE_METRICS}


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
        payload["segments"] = data["segments"].get("segments")
    return payload


_NARRATIVE_SYSTEM = """You are the analytics assistant for a cold-outreach webinar operation.
You get the stats JSON of the most recent webinar (plus the previous one for
comparison, per-bucket segments, A/B variants, copy variants and senders).
Metric conventions: keys ending in Percent are FRACTIONS (0.031 = 3.1%);
/1k metrics are per 1000 invited.

Write the weekly report narrative as plain text (no markdown, no HTML) in
exactly four sections, each opened by its header line (exact words, all caps):

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

Keep the whole thing under 300 words. Numbers with at most 1 decimal."""


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


# ---------------------------------------------------------------------------
# HTML rendering (email-client-safe: tables + inline CSS only)
# ---------------------------------------------------------------------------

_GREEN = "#16a34a"
_RED = "#dc2626"
_MUTED = "#6b7280"
_BORDER = "#e5e7eb"

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
        return "<span style='color:%s'>—</span>" % _MUTED
    diff = cur - prev
    mag = abs(diff)
    # Collapse deltas that would display as 0 (e.g. "−0.0pp") into ±0.
    if fmt == "pct":
        rounded_zero = round(mag * 100, 1) == 0
    elif fmt == "ratio":
        rounded_zero = round(mag, 1) == 0
    else:
        rounded_zero = int(round(mag)) == 0
    if rounded_zero:
        return "<span style='color:%s'>±0</span>" % _MUTED
    good = diff < 0 if key in _LOWER_IS_BETTER else diff > 0
    color = _GREEN if good else _RED
    sign = "+" if diff > 0 else "−"
    if fmt == "pct":
        text = f"{sign}{mag * 100:.1f}pp"
    elif fmt == "ratio":
        text = f"{sign}{mag:.1f}"
    else:
        text = f"{sign}{int(round(mag)):,}"
    return f"<span style='color:{color};font-weight:600'>{text}</span>"


_TH = (
    "padding:6px 10px;text-align:left;font-size:12px;color:%s;"
    "border-bottom:2px solid %s;white-space:nowrap" % (_MUTED, _BORDER)
)
_TD = "padding:6px 10px;font-size:13px;border-bottom:1px solid %s;white-space:nowrap" % _BORDER


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th style='{_TH}'>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td style='{_TD}'>{c}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%'>"
        f"<tr>{head}</tr>{body}</table>"
    )


def _section(title: str, inner: str) -> str:
    return (
        f"<h2 style='font-size:16px;margin:28px 0 10px'>{title}</h2>"
        f"<div style='overflow-x:auto'>{inner}</div>"
    )


def _webinar_label(w: dict[str, Any] | None) -> str:
    if not w:
        return "—"
    label = f"Webinar #{w.get('number')}"
    if w.get("variantLabel"):
        label += f" · {w['variantLabel']}"
    if w.get("date"):
        label += f" · {w['date']}"
    return _esc(label)


def _headline_table(current: dict[str, Any], previous: dict[str, Any] | None) -> str:
    cur_m = current.get("summary") or {}
    prev_m = (previous or {}).get("summary") or {}
    cur_col = f"W{current.get('number')} <span style='color:{_GREEN};font-size:10px'>(latest)</span>"
    prev_col = f"W{previous.get('number')}" if previous else "Previous"
    rows = []
    for key, label, fmt in _HEADLINE_METRICS:
        rows.append([
            _esc(label),
            f"<b>{_fmt(cur_m.get(key), fmt)}</b>",
            _fmt(prev_m.get(key), fmt) if previous else "—",
            _fmt_delta(cur_m.get(key), prev_m.get(key), fmt, key) if previous else "—",
        ])
    return _table(["Metric", cur_col, prev_col, "Δ"], rows)


_NARRATIVE_HEADERS = ("FLAGS", "HIGHLIGHTS", "MEANINGFUL CHANGES", "ACTIONS")


def _parse_narrative(narrative: str) -> dict[str, list[str]]:
    """Split the model's plain-text output into {section: [bullet, ...]}."""
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
    return sections


def _bullet(text: str, color: str = "#111827") -> str:
    return (
        f"<div style='font-size:13px;line-height:1.5;margin:0 0 6px 0;color:{color}'>"
        f"<span style='color:{_MUTED}'>&bull;&nbsp;</span>{_esc(text)}</div>"
    )


def _flags_box(flags: list[str]) -> str:
    """Amber anomaly-flags callout, shown at the top when the AI raises any."""
    real = [f for f in flags if f.strip().lower() not in ("none", "- none")]
    if not real:
        return ""
    bullets = "".join(
        f"<div style='font-size:13px;line-height:1.5;margin:0 0 6px 0;color:#92400e'>&#9888;&#65039;&nbsp;{_esc(f)}</div>"
        for f in real
    )
    return (
        "<div style='border:1px solid #f59e0b66;background:#fffbeb;border-radius:8px;"
        "padding:12px 16px;margin:0 0 18px 0'>"
        "<div style='font-size:11px;font-weight:700;letter-spacing:1px;color:#b45309;"
        "text-transform:uppercase;margin-bottom:8px'>Anomaly Flags</div>"
        f"{bullets}</div>"
    )


def _summary_card(sections: dict[str, list[str]]) -> str:
    """Highlights / meaningful changes / actions as one clean card."""
    blocks: list[str] = []
    styling = [
        ("HIGHLIGHTS", "Highlights", "#4f46e5"),
        ("MEANINGFUL CHANGES", "Meaningful Changes", "#0891b2"),
        ("ACTIONS", "Actions for Next Week", "#16a34a"),
    ]
    for key, title, color in styling:
        bullets = sections.get(key) or []
        if not bullets:
            continue
        blocks.append(
            f"<div style='font-size:11px;font-weight:700;letter-spacing:1px;color:{color};"
            f"text-transform:uppercase;margin:14px 0 8px 0'>{title}</div>"
            + "".join(_bullet(b) for b in bullets)
        )
    if not blocks:
        return ""
    inner = "".join(blocks).replace("margin:14px 0 8px 0", "margin:0 0 8px 0", 1)
    return (
        "<div style='border:1px solid %s;border-left:4px solid #4f46e5;"
        "border-radius:8px;padding:14px 18px;margin:6px 0'>%s</div>"
        % (_BORDER, inner)
    )


def _segment_tables(segments: dict[str, Any]) -> list[tuple[str, str]]:
    """Three ranked views of the same bucket rows: registrations, attendance,
    bookings — each sorted best-first on its own metric."""
    seg_rows = list(segments.get("segments") or [])
    totals = segments.get("totals")

    def _name_cell(s: dict[str, Any], bold: bool = False) -> str:
        name = s.get("bucketName") or "—"
        cell = f"<b>{_esc(name)}</b>" if bold else _esc(name)
        quality = s.get("quality")
        if quality and not bold:
            cell += f" <span style='color:{_MUTED};font-size:11px'>({_esc(quality)})</span>"
        return cell

    def _rate(a: float, b: float) -> float | None:
        return a / b if b else None

    def _build(headers: list[str], row_fn, sort_key) -> str:
        ranked = sorted(seg_rows, key=sort_key, reverse=True)
        rows = [row_fn(s, False) for s in ranked]
        if totals:
            rows.append(row_fn(totals, True))
        return _table(headers, rows)

    def _reg_row(s: dict[str, Any], bold: bool) -> list[str]:
        inv, regs = s.get("invites") or 0, s.get("regs") or 0
        return [_name_cell(s, bold), f"{inv:,}", f"{regs:,}", _fmt(_rate(regs, inv), "pct")]

    def _att_row(s: dict[str, Any], bold: bool) -> list[str]:
        regs, att10 = s.get("regs") or 0, s.get("attendees10m") or 0
        return [_name_cell(s, bold), f"{regs:,}", f"{att10:,}", _fmt(_rate(att10, regs), "pct")]

    def _book_row(s: dict[str, Any], bold: bool) -> list[str]:
        att10, bookings = s.get("attendees10m") or 0, s.get("bookings") or 0
        return [_name_cell(s, bold), f"{att10:,}", f"{bookings:,}", _fmt(_rate(bookings, att10), "pct")]

    return [
        (
            "Segments — by Registrations",
            _build(
                ["Bucket", "Invites", "Regs", "Inv>Reg %"],
                _reg_row,
                lambda s: (_rate(s.get("regs") or 0, s.get("invites") or 0) or 0, s.get("regs") or 0),
            ),
        ),
        (
            "Segments — by Attendance",
            _build(
                ["Bucket", "Regs", "10m+", "Att 10m+ %"],
                _att_row,
                lambda s: (_rate(s.get("attendees10m") or 0, s.get("regs") or 0) or 0, s.get("attendees10m") or 0),
            ),
        ),
        (
            "Segments — by Bookings",
            _build(
                ["Bucket", "10m+", "Bookings", "Book/10m %"],
                _book_row,
                lambda s: (s.get("bookings") or 0, _rate(s.get("bookings") or 0, s.get("attendees10m") or 0) or 0),
            ),
        ),
    ]


def _group_table(groups: list[dict[str, Any]], first_col: str) -> str:
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
    return _table(
        [first_col, "Lists", "Invited", "Regs", "Inv>Reg %", "Yes", "Maybe", "Bookings"],
        rows,
    )


def _variants_table(variants: list[dict[str, Any]]) -> str:
    headers = ["Metric"] + [
        _esc(v.get("variantLabel") or "Primary") for v in variants
    ]
    rows = []
    for key, label, fmt in _HEADLINE_METRICS:
        row = [_esc(label)]
        for v in variants:
            row.append(_fmt((v.get("summary") or {}).get(key), fmt))
        rows.append(row)
    return _table(headers, rows)


def render_report_html(data: dict[str, Any], narrative: str | None) -> str:
    current = data["current"]
    previous = data["previous"]

    sections: list[str] = []

    header = (
        f"<h1 style='font-size:20px;margin:0 0 4px'>Weekly Webinar Report — {_webinar_label(current)}</h1>"
        f"<div style='font-size:13px;color:{_MUTED};margin-bottom:18px'>"
        + (f"Compared to {_webinar_label(previous)}" if previous else "No previous webinar to compare against")
        + "</div>"
    )
    sections.append(header)

    if narrative:
        parsed = _parse_narrative(narrative)
        flags_html = _flags_box(parsed.get("FLAGS") or [])
        if flags_html:
            sections.append(flags_html)
        summary_html = _summary_card(parsed)
        if summary_html:
            sections.append(_section("Summary &amp; Actions", summary_html))

    sections.append(_section("Headline Stats", _headline_table(current, previous)))

    if data["segments"] and (data["segments"].get("segments") or []):
        for title, table_html in _segment_tables(data["segments"]):
            sections.append(_section(title, table_html))
        pending = data["segments"].get("pendingWebinarIds") or []
        if pending:
            sections.append(
                f"<div style='font-size:11px;color:{_MUTED};margin-top:4px'>"
                f"Note: {len(pending)} webinar snapshot(s) pending — totals may undercount.</div>"
            )

    if data["variants"]:
        sections.append(_section("A/B Variants", _variants_table(data["variants"])))

    if data["titleCopies"]:
        sections.append(_section("Title Copy Variants", _group_table(data["titleCopies"], "Title copy")))
    if data["descCopies"]:
        sections.append(_section("Description Copy Variants", _group_table(data["descCopies"], "Description copy")))
    if data["senders"]:
        sections.append(_section("Sender Performance", _group_table(data["senders"], "Sender")))

    generated = datetime.now(dt_timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    footer = (
        f"<div style='font-size:11px;color:{_MUTED};margin-top:28px;border-top:1px solid {_BORDER};"
        f"padding-top:10px'>Generated {generated} · "
        f"<a href='{STATISTICS_PAGE_URL}' style='color:#4f46e5'>Open the Statistics dashboard</a></div>"
    )
    sections.append(footer)

    return (
        "<div style='max-width:680px;margin:0 auto;padding:24px;"
        "font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "color:#111827'>" + "".join(sections) + "</div>"
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

    try:
        data = await build_report_data(webinar_id)
    except Exception as exc:
        logger.exception("Weekly report: failed to build report data")
        return await _fail(f"Failed to build report data: {exc}")
    if data is None:
        return await _fail("No passed webinar found to report on")

    narrative = await generate_narrative(data)
    html = render_report_html(data, narrative)

    current = data["current"]
    # ASCII-only subject: non-ASCII chars (em dashes etc.) get RFC2047-encoded
    # in the header, which trips Gmail's "abnormal characters" spoof warning
    # on a young sending domain.
    subject = f"Weekly Webinar Report - W{current.get('number')}"
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
        "narrative_included": narrative is not None,
    }
