"""GoHighLevelStatisticsSource — compute per-webinar metrics from synced GHL tables.

Joins ghl_contact / ghl_opportunity / webinargeek_subscribers / webinar_list_assignments
against the Planning `webinars` table to produce the same
raw-metric shape as WorkbookMockStatisticsSource (then the existing
compute_derived_metrics() in services.statistics derives the ratios).

Invited numbers come from the app (Planning assignments), not GHL. Group A
(sales) comes from ghl_opportunity keyed on Webinar Source Number v2.
Yes/Maybe/Self Reg counts come from parsing GHL contact text fields.
Attendance / watch time comes from webinargeek_subscribers joined by email.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import (
    Contact, GHLContact, GHLWebinarStats, OutreachSender, Webinar,
    WebinarContactMembership, WebinarGeekSubscriber, WebinarListAssignment,
)
from db.session import AsyncSessionLocal
from services.nonjoiners import nonjoiner_pool_emails

logger = logging.getLogger(__name__)


# Known limitation — A/B same-number variants: GHL custom fields carry only the
# webinar NUMBER (webinar_source_number / booked_call_webinar_series), never the
# A/B variant. So a booking for webinar N can't be split between variant A and B
# of that number; number-keyed sales metrics count it for whichever variant(s)
# the contact belongs to. Nonjoiner/no-list specials are already pinned to the
# primary variant. Not fixable without variant data in GHL. (Reuse across
# DIFFERENT numbers is handled by webinar_booking_attribution.)

# Deal Won / Disqualified stage IDs (from reference project)
DEAL_WON_STAGE_ID = "544b178f-d1f2-4186-a8c2-00c3b0eeefe8"
DISQUALIFIED_STAGE_ID = "62448525-88ab-4e82-b414-b6880e69e2de"

# Lead quality buckets
LEAD_QUALITY_GREAT = "Great"
LEAD_QUALITY_OK = "Ok"
LEAD_QUALITY_BARELY = "Barely Passable"
LEAD_QUALITY_BAD_DQ = "Bad / DQ"

# Qualified = any lead_quality except DQ (per user: qualified = shows with a non-DQ quality)
QUALIFIED_SET = {LEAD_QUALITY_GREAT, LEAD_QUALITY_OK, LEAD_QUALITY_BARELY}

# Reusable CTE exposing the nonjoiner email set as a small joinable relation, so
# semi/anti-joins against it compile to hash joins instead of per-row array
# scans. Backed by the tmp_nj_emails session temp table filled in
# _synthetic_special_rows: since the pool became the 6-webinar window it runs
# ~9k emails rather than ~2k, and re-sending that as a text[] bind into all five
# statements below cost more than the 120s prod cap could absorb.
NJ_EMAILS_CTE = "nj_emails_cte AS (SELECT email FROM tmp_nj_emails)"


# ---------------------------------------------------------------------------
# Webinars-list TTL cache
# ---------------------------------------------------------------------------
# A single Statistics page load fans out ~50 concurrent per-webinar fetches
# (one HTTP request each — each instantiates a fresh GoHighLevelStatisticsSource,
# so instance-level caching wouldn't help). _load_webinars() is the same eager
# load every time and only needs to be fresh-ish to discover newly-created
# webinars. We cache the result process-wide for a short TTL and guard the
# refresh with an asyncio lock so the first burst on a cold cache fires one
# load, not N.

_WEBINARS_CACHE_TTL_SECONDS = 30.0
_webinars_cache: tuple[float, list[Webinar]] | None = None
_webinars_cache_lock = asyncio.Lock()


async def _get_cached_webinars() -> list[Webinar]:
    global _webinars_cache
    now = time.monotonic()
    cached = _webinars_cache
    if cached is not None and (now - cached[0]) < _WEBINARS_CACHE_TTL_SECONDS:
        return cached[1]
    async with _webinars_cache_lock:
        # Re-check after acquiring the lock: another coroutine may have just
        # populated the cache while we waited.
        cached = _webinars_cache
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < _WEBINARS_CACHE_TTL_SECONDS:
            return cached[1]
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Webinar)
                .options(
                    selectinload(Webinar.assignments)
                    .selectinload(WebinarListAssignment.bucket),
                    selectinload(Webinar.assignments)
                    .selectinload(WebinarListAssignment.sender),
                    selectinload(Webinar.assignments)
                    .selectinload(WebinarListAssignment.title_copy),
                    selectinload(Webinar.assignments)
                    .selectinload(WebinarListAssignment.desc_copy),
                )
                .order_by(Webinar.number.asc())
            )
            webinars = list(result.unique().scalars().all())
        _webinars_cache = (time.monotonic(), webinars)
        return webinars


def invalidate_webinars_cache() -> None:
    """Drop the cached webinars list. Call after any mutation that affects
    Planning rows (assignments, copy, buckets, senders) when the caller
    needs the Statistics page to see the change without waiting for the TTL.
    Currently nothing wires this up — TTL is the only refresh path."""
    global _webinars_cache
    _webinars_cache = None


# ---------------------------------------------------------------------------
# Parsing helpers for free-text contact fields
# ---------------------------------------------------------------------------

def _invite_response_regex(webinar_number: int, response: str) -> str:
    """PostgreSQL regex to find e{N}-{Yes|Maybe} token in
    calendar_invite_response_history. Uses PG's `\\y` word-boundary (Python's
    `\\b` is not supported by PG's regex engine) with case-insensitive flag.

    Example value: "e119-Yes, e113-Maybe"
    """
    return rf"\ye{webinar_number}-{response}\y"


async def _fetch_wg_broadcast_totals(db: AsyncSession, broadcast_id: str) -> dict | None:
    """Return WG broadcast cache totals (subscriptions_count, live_viewers_count,
    replay_viewers_count) for the given broadcast, or None when no row exists.
    Used to source authoritative regs/attended for the webinar parent row —
    the synced `webinargeek_subscribers` table omits no-email registrants.
    """
    from sqlalchemy import text as sa_text
    r = await db.execute(sa_text(
        """
        SELECT subscriptions_count, live_viewers_count, replay_viewers_count
        FROM webinargeek_webinars WHERE broadcast_id = :bid
        """
    ).bindparams(bid=broadcast_id))
    row = r.mappings().one_or_none()
    if row is None:
        return None
    return {
        "subscriptions_count": int(row["subscriptions_count"] or 0),
        "live_viewers_count": int(row["live_viewers_count"] or 0),
        "replay_viewers_count": int(row["replay_viewers_count"] or 0),
    }


async def _csv_mode_for_webinar(db: AsyncSession, webinar_id: str) -> bool:
    """Return True when a completed Added-to-Calendar CSV with response data
    exists for this webinar. When True, Yes/Maybe metrics source from
    webinar_calendar_invites instead of parsing GHL response history regex.

    Scoped to kind='calendar' so a Non-joiners upload (kind='nonjoiner') does
    NOT flip planned-list / No-List-Data into CSV mode."""
    from sqlalchemy import text as sa_text
    r = await db.execute(sa_text(
        """
        SELECT 1 FROM webinar_calendar_uploads
        WHERE webinar_id = CAST(:wid AS uuid)
          AND kind = 'calendar'
          AND status = 'complete'
          AND has_responses = TRUE
        LIMIT 1
        """
    ).bindparams(wid=webinar_id))
    return r.scalar() is not None


# Reusable CTE: per-webinar Non-joiners CSV responses (lowercased email + resp).
# Empty when no Non-joiners CSV was uploaded for :wid, so Yes/Maybe naturally
# fall to 0. Caller binds :wid and prefixes/joins this.
NJ_CSV_CTE = (
    "njcsv AS (SELECT LOWER(email) AS email, LOWER(calendar_invite_response) AS resp "
    "FROM webinar_nonjoiner_invites WHERE webinar_id = CAST(:wid AS uuid))"
)


def _csv_yes_maybe_ctes() -> str:
    """CTE prefix yielding csv_yes(lem) and csv_maybe(lem) — lowercased emails.
    Caller must bind :wid to the webinar id and prefix this to its SQL."""
    return """
        WITH csv_yes AS (
            SELECT LOWER(email) AS lem FROM webinar_calendar_invites
            WHERE webinar_id = CAST(:wid AS uuid)
              AND LOWER(calendar_invite_response) = 'yes'
        ),
        csv_maybe AS (
            SELECT LOWER(email) AS lem FROM webinar_calendar_invites
            WHERE webinar_id = CAST(:wid AS uuid)
              AND LOWER(calendar_invite_response) = 'maybe'
        )
    """


def _webinar_series_regex(webinar_number: int) -> str:
    """PostgreSQL regex to find e{N} token in calendar_webinar_series_history.

    Example value: "e136, e127, e121, e118, e114"
    """
    return rf"\ye{webinar_number}\y"


# ---------------------------------------------------------------------------
# Lead-source parsing (for the "By List Source" statistics tab)
# ---------------------------------------------------------------------------
# Raw metric keys summed per (source, vintage) cell. Kept identical to the
# Segments tab's _FUNNEL_RAW_KEYS so the aggregate + frontend reuse the same
# funnel-derivation logic (percentages derived from these sums, never averaged).
SOURCE_FUNNEL_RAW_KEYS = (
    "invited", "totalRegs", "totalAttended", "total10MinPlus", "totalBookings", "uniqueBookers",
    "confirmed", "shows", "noShows", "canceled", "won",
    "disqualified", "qualified",
    "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
)

# lead_list_name usually leads with the data provider, e.g.
# "Ampleleads, Apr 1 2026, Agencies, 0-10 employees, US". Match case-insensitively
# on a substring so casing/typos ("Ampleleads"/"AmpleLeads") collapse together.
_SOURCE_ALIASES: tuple[tuple[str, str], ...] = (
    ("ampleleads", "AmpleLeads"),
    ("findylead", "FindyLeads"),
    ("doctor lead", "Doctor Lead"),
    ("zoominfo", "ZoomInfo"),
    ("apollo", "Apollo"),
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)", re.I)
_YEAR_RE = re.compile(r"(20\d{2})")
_FL_PREFIX_RE = re.compile(r"^\s*fl[\s,]", re.I)


def _lead_source_from_list_name(name: str | None) -> str:
    """Map a free-text contacts.lead_list_name to a canonical data-source label.

    e.g. "Ampleleads, Apr 1 2026, Agencies, ..." -> "AmpleLeads";
         "findyLeads, Nov 24, ..." / "FL, Oct 14, ..." -> "FindyLeads".
    Unknown providers fall back to their first comma-token; date-led labels
    (no provider prefix) become "Other"; blank -> "Unlabeled".
    """
    if not name or not name.strip():
        return "Unlabeled"
    low = name.lower()
    for needle, label in _SOURCE_ALIASES:
        if needle in low:
            return label
    if _FL_PREFIX_RE.match(name):
        return "FindyLeads"
    first = name.split(",")[0].strip()
    # If the label leads with a date/number rather than a provider, treat it as
    # unlabeled rather than inventing a source from a date token.
    if first and not first[0].isdigit() and not _MONTH_RE.match(first):
        return first[:24]
    return "Other"


def _lead_vintage_from_list_name(name: str | None, webinar_date) -> str:
    """Best-effort list vintage as "YYYY-MM" parsed from lead_list_name.

    Labels are inconsistent: some carry a 4-digit year ("Apr 1 2026",
    "Sept 8 2025"), many omit it ("Nov 24", "Mar 11" — here the trailing number
    is a day, not a year). When the year is absent we infer the most-recent-past
    occurrence of that month relative to the webinar date: e.g. "Nov 24" on a
    Jun-2026 webinar -> 2025-11. Returns "(undated)" when no month is present.
    Heuristic by necessity — adjust here if the labeling convention changes.
    """
    if not name:
        return "(undated)"
    mm = _MONTH_RE.search(name)
    ym = _YEAR_RE.search(name)
    if not mm:
        return f"{ym.group(1)}-01" if ym else "(undated)"
    month = _MONTHS[mm.group(1).lower()]
    if ym:
        year = int(ym.group(1))
    else:
        wy = webinar_date.year if webinar_date else datetime.now(timezone.utc).year
        wm = webinar_date.month if webinar_date else 12
        year = wy if month <= wm else wy - 1
    return f"{year}-{month:02d}"


# ---------------------------------------------------------------------------
# Aggregation query helpers
# ---------------------------------------------------------------------------

async def _webinar_summary_from_app(
    db: AsyncSession, webinar_id: str
) -> dict[str, float | None]:
    """Fetch invited/accountsNeeded from app-side assignments, plus the live
    actuallyUsed count from webinar_contact_memberships (status='used')."""
    result = await db.execute(
        select(
            func.coalesce(func.sum(WebinarListAssignment.volume), 0).label("list_size"),
            func.coalesce(func.sum(WebinarListAssignment.accounts_used), 0).label("accts_used"),
        ).where(WebinarListAssignment.webinar_id == webinar_id)
    )
    row = result.one()
    list_size = int(row.list_size) if row.list_size is not None else 0

    # actuallyUsed: live count of memberships marked sent (status='used') for this
    # webinar. Released contacts have their membership deleted and disappear from
    # this count, so plan/actual diverge by the released amount.
    used_result = await db.execute(
        select(func.count())
        .select_from(WebinarContactMembership)
        .where(
            WebinarContactMembership.webinar_id == webinar_id,
            WebinarContactMembership.status == "used",
        )
    )
    actually_used = int(used_result.scalar() or 0)

    return {
        "accountsNeeded": int(row.accts_used) if row.accts_used is not None else 0,
        "invited": list_size,  # app-side: invited = sum of planned volumes
        "actuallyUsed": actually_used,
    }


async def _count_contact_field_match(
    db: AsyncSession, column, pattern: str
) -> int:
    """Count rows where the given TEXT column matches the regex."""
    result = await db.execute(
        select(func.count(GHLContact.ghl_contact_id)).where(column.op("~*")(pattern))
    )
    return int(result.scalar() or 0)


async def _count_attended_for_broadcast_filtered(
    db: AsyncSession,
    broadcast_id: str | None,
    invite_response_pattern: str | None,
    registration_between: tuple | None,
    min_minutes: int | None = None,
    require_sms_tag: bool = False,
) -> int:
    """Count contacts attended a webinar (via webinargeek_subscribers join on email)
    with optional filters: invite response pattern (yes/maybe), self-reg date window,
    minimum minutes_viewing, SMS click tag.
    """
    if not broadcast_id:
        return 0

    q = (
        select(func.count(func.distinct(GHLContact.ghl_contact_id)))
        .select_from(GHLContact)
        .join(
            WebinarGeekSubscriber,
            func.lower(WebinarGeekSubscriber.email) == func.lower(GHLContact.email),
        )
        .where(WebinarGeekSubscriber.broadcast_id == broadcast_id)
    )

    # Attended = watched_live=True OR minutes_viewing > 0
    attended_filter = or_(
        WebinarGeekSubscriber.watched_live.is_(True),
        WebinarGeekSubscriber.minutes_viewing > 0,
    )
    q = q.where(attended_filter)

    if min_minutes is not None:
        q = q.where(WebinarGeekSubscriber.minutes_viewing >= min_minutes)

    if invite_response_pattern:
        q = q.where(
            GHLContact.calendar_invite_response_history.op("~*")(invite_response_pattern)
        )

    if registration_between:
        start_date, end_date = registration_between
        q = q.where(
            GHLContact.webinar_registration_in_form_date > start_date,
            GHLContact.webinar_registration_in_form_date <= end_date,
        )

    if require_sms_tag:
        q = q.where(GHLContact.has_sms_click_tag.is_(True))

    result = await db.execute(q)
    return int(result.scalar() or 0)


async def _count_broadcast_attendees(
    db: AsyncSession, broadcast_id: str, min_minutes: int | None = None,
    require_sms_tag: bool = False,
) -> int:
    """Total attended count (no contact-field filter) for a broadcast."""
    if require_sms_tag:
        # Need GHL contact join for tag — use the filtered helper
        return await _count_attended_for_broadcast_filtered(
            db, broadcast_id, None, None, min_minutes, require_sms_tag=True
        )

    q = select(func.count()).where(
        WebinarGeekSubscriber.broadcast_id == broadcast_id,
        or_(
            WebinarGeekSubscriber.watched_live.is_(True),
            WebinarGeekSubscriber.minutes_viewing > 0,
        ),
    )
    if min_minutes is not None:
        q = q.where(WebinarGeekSubscriber.minutes_viewing >= min_minutes)
    result = await db.execute(q)
    return int(result.scalar() or 0)


# ---------------------------------------------------------------------------
# Main source class
# ---------------------------------------------------------------------------

def _row_kind_from_assignment(a: WebinarListAssignment) -> str:
    if a.is_nonjoiners:
        return "nonjoiners"
    if a.is_no_list_data:
        return "no_list_data"
    return "list"


def _row_for_assignment(a: WebinarListAssignment, webinar_status: str) -> dict[str, Any]:
    """Build a child row dict from a Planning WebinarListAssignment.

    Base metrics (accountsNeeded / invited) are attributed to this list. GHL /
    WebinarGeek metrics are left null on the list row since they don't
    decompose per list — they're computed per-webinar and shown in the summary.
    """
    sender_name = a.sender.name if a.sender else None
    sender_color = a.sender.color if a.sender else None
    metrics: dict[str, float | None] = {
        "accountsNeeded": a.accounts_used or 0,
        "invited": a.volume or 0,
        # actuallyUsed is filled in by _compute_per_list_metrics — count of
        # contacts marked sent for this assignment (status='used').
        "actuallyUsed": 0,
    }

    # Title + description copy variants chosen for this list
    title_copy = None
    if a.title_copy:
        title_copy = {
            "id": a.title_copy.id,
            "text": a.title_copy.text,
            "variantIndex": a.title_copy.variant_index,
        }
    desc_copy = None
    if a.desc_copy:
        desc_copy = {
            "id": a.desc_copy.id,
            "text": a.desc_copy.text,
            "variantIndex": a.desc_copy.variant_index,
        }

    return {
        "workbookRow": a.display_order or 0,
        "assignmentId": a.id,
        "kind": _row_kind_from_assignment(a),
        "status": webinar_status,
        "note": None,
        "listUrl": a.list_url,
        "description": a.description,
        "listName": a.list_name,
        "sendInfo": sender_name,
        "senderColor": sender_color,
        "bucketId": a.bucket_id,
        "bucketName": a.bucket.name if a.bucket else None,
        "descLabel": None,
        "titleText": a.title_copy.text if a.title_copy else None,
        "titleCopy": title_copy,
        "descCopy": desc_copy,
        "createdDate": a.created_at.date().isoformat() if a.created_at else None,
        "industry": a.bucket.industry if a.bucket else None,
        "employeeRange": a.emp_range_override,
        "country": a.countries_override,
        "metrics": metrics,
    }


class GoHighLevelStatisticsSource:
    """Compute per-webinar metrics from Planning assignments + WebinarGeek
    subscribers + synced GHL contacts/opportunities.

    Returns per-list child rows (one per WebinarListAssignment) with base
    metrics + a pre-computed summary dict combining aggregated list bases
    and webinar-wide GHL/WG metrics.
    """

    async def get_raw_webinars(self) -> list[dict[str, Any]]:
        webinars = await self._load_webinars()
        date_windows = self._date_windows(webinars)
        siblings_map = self._sibling_webinar_ids(webinars)
        primary_ids = self._primary_variant_ids(webinars)
        raw_webinars: list[dict[str, Any]] = []
        for w in webinars:
            prev_date, current_date = date_windows[w.id]
            raw_webinars.append(
                await self._build_raw_webinar(
                    w, prev_date, current_date,
                    sibling_webinar_ids=siblings_map.get(w.id, []),
                    is_primary=w.id in primary_ids,
                )
            )
        raw_webinars.reverse()  # descending by number for the UI
        return raw_webinars

    async def get_raw_webinar_list(self) -> list[dict[str, Any]]:
        """Lightweight list — webinar identity + list count, no metrics.

        Powers the progressive-load UI: the page renders the parent rows
        immediately, then fetches per-webinar metrics in priority order.
        Each A/B variant is its own row (its own `webinarId`); the
        frontend renders them as two separate parent rows.
        """
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Webinar).order_by(
                    Webinar.number.desc(),
                    Webinar.variant_label.asc().nullsfirst(),
                )
            )
            webinars = list(result.scalars().all())

            counts_q = await db.execute(
                select(WebinarListAssignment.webinar_id, func.count())
                .group_by(WebinarListAssignment.webinar_id)
            )
            counts = {str(wid): int(c or 0) for wid, c in counts_q.all()}

        out: list[dict[str, Any]] = []
        for w in webinars:
            # Append the variant label to the synthetic id so two siblings
            # on the same number don't collide in any client-side Map.
            id_suffix = f"-{w.variant_label}" if w.variant_label else ""
            out.append({
                "id": f"stat-w{w.number}{id_suffix}",
                "webinarId": w.id,
                "number": w.number,
                "variantLabel": w.variant_label,
                "date": w.date.isoformat() if w.date else None,
                "title": w.main_title,
                "status": w.status,
                "listCount": counts.get(str(w.id), 0),
                "broadcastId": w.broadcast_id,
            })
        return out

    async def get_raw_webinar(self, webinar_id: str) -> dict[str, Any] | None:
        """Fully-processed single webinar (summary + per-list rows + specials).

        Looks up by webinar_id so A/B variants on the same number can be
        addressed unambiguously. Callers that still have only a number
        should resolve to a webinar_id at the API layer first.
        """
        webinars = await self._load_webinars()
        date_windows = self._date_windows(webinars)
        siblings_map = self._sibling_webinar_ids(webinars)
        primary_ids = self._primary_variant_ids(webinars)
        for w in webinars:
            if w.id == webinar_id:
                prev_date, current_date = date_windows[w.id]
                return await self._build_raw_webinar(
                    w, prev_date, current_date,
                    sibling_webinar_ids=siblings_map.get(w.id, []),
                    is_primary=w.id in primary_ids,
                )
        return None

    @staticmethod
    def _sibling_webinar_ids(webinars: list[Webinar]) -> dict[str, list[str]]:
        """{webinar_id: [other webinars sharing the same (user_id, number)]}.
        Empty list when this webinar is the only one for its number."""
        by_key: dict[tuple[str, int], list[str]] = {}
        for w in webinars:
            by_key.setdefault((w.user_id, w.number), []).append(w.id)
        out: dict[str, list[str]] = {}
        for w in webinars:
            ids = by_key.get((w.user_id, w.number), [])
            out[w.id] = [wid for wid in ids if wid != w.id]
        return out

    @staticmethod
    def _primary_variant_ids(webinars: list[Webinar]) -> set[str]:
        """Id of the 'primary' webinar in each (user_id, number) group — the
        variant with the largest planned audience (sum of assignment volumes),
        ties broken by id.

        NO LIST DATA / Nonjoiners are webinar-NUMBER-level signals (booked_call,
        invite responses) that can't be tied to a specific A/B variant — those
        bookers aren't on any list (e.g. they booked with a different email than
        we invited). We attribute them to the primary variant only — the largest
        funnel, where they almost certainly came from — instead of duplicating
        them onto every variant. Non-variant webinars are always their own
        primary."""
        by_key: dict[tuple[str, int], list[Webinar]] = {}
        for w in webinars:
            by_key.setdefault((w.user_id, w.number), []).append(w)
        primary: set[str] = set()
        for ws in by_key.values():
            best = max(ws, key=lambda w: (sum((a.volume or 0) for a in w.assignments), w.id))
            primary.add(best.id)
        return primary

    async def _load_webinars(self) -> list[Webinar]:
        return await _get_cached_webinars()

    @staticmethod
    def _date_windows(webinars: list[Webinar]) -> dict[str, tuple[Any, Any]]:
        """{webinar_id: (prev_date, current_date)} — window is (prev, current],
        i.e. excludes the previous webinar's day, includes the current's. When
        no prior webinar exists, falls back to a 7-day window that includes
        the current webinar's date (prev = current - 7).

        Variant-aware: when two webinars share the same `number` (A/B test),
        neither counts as "the previous webinar" for the other. We walk
        distinct numbers, so all variants of N share the same prev_date —
        the date of the most-recent webinar with `number < N`.
        """
        from datetime import timedelta

        # Map each distinct number to its anchor date — when multiple
        # variants share a number, take the latest date among them so the
        # window for the *next* number's webinar starts after both.
        date_by_number: dict[int, Any] = {}
        for w in webinars:
            existing = date_by_number.get(w.number)
            if existing is None or (w.date is not None and existing < w.date):
                date_by_number[w.number] = w.date

        sorted_numbers = sorted(date_by_number.keys())
        prev_date_by_number: dict[int, Any] = {}
        for i, n in enumerate(sorted_numbers):
            prev_date_by_number[n] = date_by_number[sorted_numbers[i - 1]] if i > 0 else None

        out: dict[str, tuple[Any, Any]] = {}
        for w in webinars:
            prev_date = prev_date_by_number.get(w.number)
            current_date = w.date
            if prev_date is None and current_date is not None:
                prev_date = current_date - timedelta(days=7)
            out[w.id] = (prev_date, current_date)
        return out

    async def _build_raw_webinar(
        self,
        w: Webinar,
        prev_date,
        current_date,
        sibling_webinar_ids: list[str] | None = None,
        is_primary: bool = True,
    ) -> dict[str, Any]:
        """Build the full raw-stats dict for one webinar.

        `sibling_webinar_ids` is the list of OTHER webinars that share this
        webinar's `number` (i.e. A/B variants). When non-empty, we (a) sum
        per-list rows for the parent summary instead of using webinar-wide
        N counts, and (b) exclude all sibling variants' planned contacts
        from NO LIST DATA so siblings don't leak into each other's leftovers.
        """
        siblings = sibling_webinar_ids or []
        is_variant = bool(siblings)
        assignments = sorted(
            w.assignments,
            key=lambda a: (a.display_order or 0, a.created_at or 0),
        )
        rows = [_row_for_assignment(a, w.status) for a in assignments]

        async with AsyncSessionLocal() as db:
            # Large webinars (100k+ planned contacts) turn the contacts⋈ghl_contact
            # join into a nested loop of hundreds of thousands of random index
            # probes, which blows past the prod 120s statement_timeout on
            # Supabase's networked storage (W150 measured at ~183s). Networked
            # random I/O is far costlier than the default cost model assumes;
            # nudging random_page_cost up lets the planner pick a single-pass hash
            # join for those big joins (~5x faster, ~35s) while small webinars keep
            # the cheaper nested loop. Scoped to this transaction only (SET LOCAL).
            from sqlalchemy import text as sa_text
            await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
            await db.execute(sa_text("SET LOCAL work_mem = '128MB'"))

            # Per-list first so the variant summary can sum from it.
            per_list = await self._compute_per_list_metrics(
                db, w, assignments, prev_date, current_date,
            )
            # per-list restored the default random_page_cost for its chunked
            # probes — put the hash-join tuning back for the whole-webinar
            # scans below (synthetic / per-source / per-employee).
            await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
            for r, a in zip(rows, assignments):
                extra = per_list.get(a.id, {})
                r["metrics"].update(extra)

            # NO LIST DATA / Nonjoiners are webinar-NUMBER-level (shared across
            # A/B variants, not tied to one). Attribute them to the primary
            # variant only (largest funnel) so they aren't duplicated across
            # variants; non-variant webinars always show their own. The primary
            # variant folds them into its summary so its total stays consistent:
            # Total = Assigned lists + NO LIST DATA + Nonjoiners.
            show_specials = (not is_variant) or is_primary
            # Non-fatal: on very large webinars this whole-webinar scan can hit
            # the prod 120s statement cap. Losing the NLD/Nonjoiner rows for one
            # compute is better than losing the entire webinar's snapshot.
            try:
                synthetic = (
                    await self._synthetic_special_rows(
                        db, w, assignments, prev_date, current_date,
                        sibling_webinar_ids=siblings,
                    )
                    if show_specials else []
                )
            except Exception:
                logger.exception("synthetic special rows failed for webinar %s — continuing without", w.number)
                await db.rollback()
                # Reapply the txn-scoped planner tuning lost with the rollback.
                await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
                await db.execute(sa_text("SET LOCAL work_mem = '128MB'"))
                synthetic = []

            # Parent summary = Assigned lists + NO LIST DATA + Nonjoiners for
            # every metric — the sum of the partition rows beneath it — for both
            # variants AND non-variants, so the Total Webinar row always equals
            # the rows shown under it (including CSV-sourced nonjoiner Yes/Maybe,
            # which a webinar-wide GHL query can't see). invited/accountsNeeded
            # stay planned volume; totalRegs/totalAttended are overridden to the
            # WG broadcast totals below (equal to the partition sum by design).
            summary = self._summary_from_per_list(
                assignments, per_list,
                extra_metrics=[s["metrics"] for s in synthetic],
            )

            # Override registration/attendance totals with the WG broadcast
            # cache when available. Both the per-list count and the
            # webinar-wide query only see contacts we've synced (rows in
            # webinargeek_subscribers); WG's broadcast row carries the
            # authoritative `subscriptions_count` / `live_viewers_count`
            # which also covers no-email registrants who never get synced.
            if w.broadcast_id:
                wg_totals = await _fetch_wg_broadcast_totals(db, w.broadcast_id)
                if wg_totals is not None:
                    summary["totalRegs"] = wg_totals["subscriptions_count"]
                    summary["totalAttended"] = wg_totals["live_viewers_count"]

            # Override actuallyUsed with the webinar-level membership count.
            # The per-list sum drops memberships whose list was deleted
            # (assignment_id NULL) even though delete_assignment deliberately
            # keeps those contacts in the webinar's stats — the webinar-level
            # count matches list-distribution and the metric drill-downs.
            used_total = await db.execute(
                select(func.count())
                .select_from(WebinarContactMembership)
                .where(
                    WebinarContactMembership.webinar_id == w.id,
                    WebinarContactMembership.status == "used",
                )
            )
            summary["actuallyUsed"] = int(used_total.scalar() or 0)

            # Keep invited >= actuallyUsed. Orphaned 'used' memberships (their list
            # was deleted via delete_assignment, assignment_id NULL) are counted in
            # the webinar-wide actuallyUsed just set, but their planned volume is
            # gone from summary['invited'] (sum of surviving assignment volumes).
            # Add them back so a deleted list never produces an impossible >100%
            # used ratio. Non-orphan used are already within a surviving volume, so
            # this does not double-count; webinars with no orphans are unaffected.
            orphan_used = await db.execute(
                select(func.count())
                .select_from(WebinarContactMembership)
                .where(
                    WebinarContactMembership.webinar_id == w.id,
                    WebinarContactMembership.status == "used",
                    WebinarContactMembership.assignment_id.is_(None),
                )
            )
            summary["invited"] = (summary.get("invited") or 0) + int(orphan_used.scalar() or 0)

            rows.extend(synthetic)

            # Per (data-source, vintage) funnel cells — powers the By List
            # Source tab. Additive: rides the cached snapshot payload.
            # Non-fatal (see synthetic above): a 120s cancel on these extras must
            # not sink the whole webinar's snapshot.
            try:
                source_rows = await self._compute_per_source_cells(db, w)
            except Exception:
                logger.exception("per-source cells failed for webinar %s — continuing without", w.number)
                await db.rollback()
                await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
                await db.execute(sa_text("SET LOCAL work_mem = '128MB'"))
                source_rows = []

            # Per company-size funnel cells — powers the Employee count tab —
            # plus the segment × company-size cross behind the Segments-tab
            # drill-down, both out of the same scan (see split_by_bucket).
            employee_rows: list[dict[str, Any]] = []
            segment_employee_rows: dict[str, list[dict[str, Any]]] = {}
            try:
                emp_cells = await self._compute_per_employee_cells(db, w, split_by_bucket=True)
                for cell in emp_cells:
                    seg = cell.get("bucketId")
                    if seg is None:
                        employee_rows.append(cell)
                    else:
                        segment_employee_rows.setdefault(seg, []).append(
                            {"bucket": cell["bucket"], "metrics": cell["metrics"]}
                        )
            except Exception:
                logger.exception("per-employee cells failed for webinar %s — continuing without", w.number)
                await db.rollback()
                employee_rows = []
                segment_employee_rows = {}

        return {
            "number": w.number,
            "variantLabel": w.variant_label,
            "webinarId": w.id,
            "date": w.date.isoformat() if w.date else None,
            "title": w.main_title,
            "workbookRow": 0,
            "rows": rows,
            "sourceRows": source_rows,
            "employeeRows": employee_rows,
            # {bucketId: [{bucket: size label, metrics}]} — the Segments-tab
            # drill-down slice. Stripped from the bulk snapshot reads (see
            # statistics_snapshot.read_all_payloads); the drill-down selects
            # just the one segment's key out of JSONB.
            "segmentEmployeeRows": segment_employee_rows,
            "summary": summary,
            "status": w.status,
            # Operators read this on the stats page to know whether
            # rate-metric denominators are split per variant.
            "hasSiblingVariants": is_variant,
        }

    @staticmethod
    def _summary_from_per_list(
        assignments: list[WebinarListAssignment],
        per_list: dict[str, dict[str, float | None]],
        extra_metrics: list[dict[str, float | None]] | None = None,
    ) -> dict[str, float | None]:
        """Build a parent summary by summing the per-list child rows.

        Used for variants — webinar-wide N counts would conflate sibling
        variants since GHL custom fields don't carry variant info. For
        non-variant webinars the existing webinar-wide aggregate stays.

        `extra_metrics` (the NO LIST DATA / Nonjoiners rows' metric dicts) are
        summed in alongside the lists so the variant total mirrors the
        webinar-wide totals non-variant parents get. accountsNeeded / invited
        stay list-derived (planned volume) — matching the non-variant parent,
        which also excludes the nonjoiners pool from `invited`.
        """
        all_metrics = list(per_list.values()) + list(extra_metrics or [])
        keys: set[str] = set()
        for m in all_metrics:
            keys.update(m.keys())
        summary: dict[str, float | None] = {
            "accountsNeeded": sum((a.accounts_used or 0) for a in assignments),
            "invited": sum((a.volume or 0) for a in assignments),
        }
        for k in keys:
            if k in summary:
                continue
            total: float = 0.0
            any_value = False
            for m in all_metrics:
                v = m.get(k)
                if v is None:
                    continue
                total += float(v)
                any_value = True
            summary[k] = total if any_value else None
        return summary

    async def _synthetic_special_rows(
        self,
        db: AsyncSession,
        w: Webinar,
        assignments: list[WebinarListAssignment],
        prev_date,
        current_date,
        sibling_webinar_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build synthetic Nonjoiners + No List Data rows for this webinar.

        - Nonjoiners: the shared pool from services/nonjoiners.py — registrants
          of the last 6 aired webinars whose most recent registration was a
          no-show, minus permanent exits (blocklist, converted contacts) and
          this webinar's planned contacts. Always shown (value may be 0).
          Falls back to the GHL `calendar_webinar_series_non_joiners` field only
          when the window resolves to nothing. Deliberately the SAME set the
          Planning viewer and export CSV hand to the assistants, so what we
          report on is exactly who we invited.
        - No List Data: contacts with any webinar-N signal (invite_response,
          non-joiners, booked_call=N, registration_number=N, self-reg in
          window) whose email is NOT in any Planning assignment for this
          webinar. "Leftover" counts not attributable to a planned list.
          When this webinar has sibling variants on the same number, the
          `planned` CTE unions across all siblings so a sibling's planned
          contacts don't appear in this variant's leftover pool.
        """
        from sqlalchemy import text as sa_text

        # This function's scans are driven from SMALL sets (calendar invites,
        # WG registrants, nonjoiner emails) probing contacts/ghl_contact by
        # email — nested loops win. The caller's whole-webinar hash-join tuning
        # (random_page_cost=8) turns the planned-email anti-join into full
        # rescans that blow the 120s cap on 100k+-member webinars.
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))

        N = w.number
        series_nj_re = _webinar_series_regex(N)
        yes_re = _invite_response_regex(N, "Yes")
        maybe_re = _invite_response_regex(N, "Maybe")
        broadcast_id = w.broadcast_id
        wid = w.id
        sibling_ids = sibling_webinar_ids or []
        # Union of all webinars (this + siblings) whose planned contacts
        # should be excluded from "leftover" leaked-signal counts.
        planned_webinar_ids: list[str] = [wid] + list(sibling_ids)

        # Materialize the planned-email set ONCE into a session temp table via
        # chunked inserts (each well under the 120s cap) instead of rebuilding a
        # 250k-row CTE inside BOTH NLD statements below — post-backfill the
        # membership join is far too big for a single in-statement scan on
        # 100k+-member webinars. IF NOT EXISTS + TRUNCATE (never DROP) keeps the
        # table's OID stable for this pooled connection, so asyncpg's prepared
        # statements stay valid across webinars in a full recompute.
        from sqlalchemy import bindparam as sa_bindparam
        await db.execute(sa_text(
            "CREATE TEMP TABLE IF NOT EXISTS tmp_nld_planned "
            "(email text PRIMARY KEY) ON COMMIT PRESERVE ROWS"))
        await db.execute(sa_text("TRUNCATE tmp_nld_planned"))
        _fill_sql = sa_text(
            "INSERT INTO tmp_nld_planned "
            "SELECT DISTINCT LOWER(c.email) FROM contacts c "
            "JOIN webinar_contact_memberships m ON m.contact_id = c.id "
            "WHERE m.webinar_id = CAST(:pwid AS uuid) AND m.assignment_id IN :aids "
            "  AND c.email IS NOT NULL "
            "ON CONFLICT (email) DO NOTHING"
        ).bindparams(sa_bindparam("aids", expanding=True))
        for _pwid in planned_webinar_ids:
            for _aids in await self._assignment_chunks(db, _pwid):
                await db.execute(_fill_sql, {"pwid": _pwid, "aids": _aids})
            # Memberships whose list was deleted (assignment_id NULL) are still
            # planned contacts — keep them excluded from the leftover pool.
            await db.execute(sa_text(
                "INSERT INTO tmp_nld_planned "
                "SELECT DISTINCT LOWER(c.email) FROM contacts c "
                "JOIN webinar_contact_memberships m ON m.contact_id = c.id "
                "WHERE m.webinar_id = CAST(:pwid AS uuid) AND m.assignment_id IS NULL "
                "  AND c.email IS NOT NULL "
                "ON CONFLICT (email) DO NOTHING"
            ), {"pwid": _pwid})

        # ── Nonjoiners ────────────────────────────────────────────────
        # Nonjoiners = registrants of the LAST SIX webinars who never joined any
        # of them — see services/nonjoiners.py for the full rule. We materialise
        # the actual *email set* (not just a count) so we can match the cohort
        # against this webinar's broadcast + opportunities and show
        # attendance/sales for it, and carve it out of NO LIST DATA below so each
        # contact is counted once. Precedence: planned > nonjoiner, enforced by
        # passing the already-filled tmp_nld_planned as the planned exclusion
        # (it spans this webinar AND its sibling variants).
        nonjoiner_emails = await nonjoiner_pool_emails(
            db, wid,
            planned_sql="SELECT 1 FROM tmp_nld_planned t WHERE t.email = p.email",
        )
        if not nonjoiner_emails:
            # No usable window (e.g. the earliest webinar, or no broadcasts
            # linked yet) — fall back to the GHL nonjoiner custom fields so a
            # webinar with GHL-tagged nonjoiners still reports them.
            nj_emails_rows = await db.execute(sa_text("""
                SELECT DISTINCT LOWER(g.email) AS email
                FROM ghl_contact g
                LEFT JOIN tmp_nld_planned t ON t.email = LOWER(g.email)
                WHERE (g.calendar_webinar_series_non_joiners ~* :nj_src_re
                       OR g.calendar_invite_response_prefix_non_joiners ~* :nj_src_re)
                  AND g.email IS NOT NULL AND t.email IS NULL
            """).bindparams(nj_src_re=series_nj_re))
            nonjoiner_emails = [r[0] for r in nj_emails_rows.all() if r[0]]
        nj_count = len(nonjoiner_emails)

        # Publish the cohort to the session temp table backing NJ_EMAILS_CTE.
        # IF NOT EXISTS + TRUNCATE (never DROP) keeps the OID stable for this
        # pooled connection so asyncpg's prepared statements survive a full
        # multi-webinar recompute — same contract as tmp_nld_planned above.
        await db.execute(sa_text(
            "CREATE TEMP TABLE IF NOT EXISTS tmp_nj_emails "
            "(email text PRIMARY KEY) ON COMMIT PRESERVE ROWS"))
        await db.execute(sa_text("TRUNCATE tmp_nj_emails"))
        for _i in range(0, len(nonjoiner_emails), 5000):
            await db.execute(sa_text(
                "INSERT INTO tmp_nj_emails "
                "SELECT DISTINCT e FROM UNNEST(CAST(:emails AS text[])) AS e "
                "ON CONFLICT (email) DO NOTHING"
            ).bindparams(emails=nonjoiner_emails[_i:_i + 5000]))
        await db.execute(sa_text("ANALYZE tmp_nj_emails"))

        # Nonjoiners row metrics. invited = the full nonjoiner pool (rates fall
        # back to it since actuallyUsed is None); self-reg has no data for them.
        nj_metrics: dict[str, float | None] = {
            "accountsNeeded": None,
            "invited": nj_count,  # treat nonjoiners as part of the invited pool
            "actuallyUsed": None,  # not from our planning system → fallback to invited
            "yesMarked": 0,
            "maybeMarked": 0,
            "selfRegMarked": 0,
            "gcalInvitedGhl": nj_count,
        }

        # Attendance for the nonjoiner cohort, matched by email against this
        # webinar's broadcast (same ATT definition as the per-list Batch B).
        # Also breaks attendance down by uploaded Yes/Maybe response (njcsv,
        # empty when no Non-joiners CSV → 0) and by self-reg (GHL form-date in
        # window). nonjoiner_regs/attended also carve nonjoiners out of the NO
        # LIST DATA broadcast-gap below.
        nonjoiner_regs = 0
        nonjoiner_attended = 0
        has_window = bool(prev_date and current_date)
        sr_pred = (
            "g.webinar_registration_in_form_date > :sr_start "
            "AND g.webinar_registration_in_form_date <= :sr_end"
        ) if has_window else "FALSE"
        if nonjoiner_emails and broadcast_id:
            ATT = "(wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)"
            wg_att_params: dict[str, Any] = {
                "bid": broadcast_id, "wid": wid,
            }
            if has_window:
                wg_att_params["sr_start"] = prev_date
                wg_att_params["sr_end"] = current_date
            r = await db.execute(sa_text(f"""
                WITH {NJ_EMAILS_CTE}, {NJ_CSV_CTE}
                SELECT
                    COUNT(DISTINCT LOWER(wgs.email))                                                    AS total_regs,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT})                                AS total_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 10)  AS total_10m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 30)  AS total_30m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'yes')                                 AS yes_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'yes'   AND wgs.minutes_viewing >= 10) AS yes_10m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'yes'   AND wgs.minutes_viewing >= 30) AS yes_30m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'maybe')                               AS maybe_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'maybe' AND wgs.minutes_viewing >= 10) AS maybe_10m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND c.resp = 'maybe' AND wgs.minutes_viewing >= 30) AS maybe_30m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND ({sr_pred}))                                    AS self_reg_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND ({sr_pred}) AND wgs.minutes_viewing >= 10)      AS self_reg_10m
                FROM webinargeek_subscribers wgs
                JOIN nj_emails_cte njx ON njx.email = LOWER(wgs.email)
                LEFT JOIN njcsv c ON c.email = LOWER(wgs.email)
                LEFT JOIN ghl_contact g ON LOWER(g.email) = LOWER(wgs.email)
                WHERE wgs.broadcast_id = :bid
            """).bindparams(**wg_att_params))
            wrow = r.mappings().one_or_none()
            if wrow:
                nonjoiner_regs = int(wrow["total_regs"] or 0)
                nonjoiner_attended = int(wrow["total_attended"] or 0)
                nj_metrics["totalRegs"] = nonjoiner_regs
                nj_metrics["totalAttended"] = nonjoiner_attended
                nj_metrics["total10MinPlus"] = int(wrow["total_10m"] or 0)
                nj_metrics["total30MinPlus"] = int(wrow["total_30m"] or 0)
                nj_metrics["yesAttended"] = int(wrow["yes_attended"] or 0)
                nj_metrics["yes10MinPlus"] = int(wrow["yes_10m"] or 0)
                nj_metrics["yes30MinPlus"] = int(wrow["yes_30m"] or 0)
                nj_metrics["maybeAttended"] = int(wrow["maybe_attended"] or 0)
                nj_metrics["maybe10MinPlus"] = int(wrow["maybe_10m"] or 0)
                nj_metrics["maybe30MinPlus"] = int(wrow["maybe_30m"] or 0)
                if has_window:
                    nj_metrics["selfRegAttended"] = int(wrow["self_reg_attended"] or 0)
                    nj_metrics["selfReg10MinPlus"] = int(wrow["self_reg_10m"] or 0)

        # Sales + quality for the nonjoiner cohort (same join shape / filter as
        # the per-list Batch C: opp.webinar_source_number = N OR booked_call = N).
        if nonjoiner_emails:
            qual_in = "('" + "', '".join(QUALIFIED_SET) + "')"
            r = await db.execute(sa_text(f"""
                WITH {NJ_EMAILS_CTE}, {NJ_CSV_CTE}
                SELECT
                    COUNT(DISTINCT o.ghl_opportunity_id) AS total_bookings,
                    COUNT(DISTINCT g.ghl_contact_id) AS unique_bookers,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.call1_appointment_date IS NOT NULL AND o.call1_appointment_date <= :now_ts) AS calls_passed,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'confirmed') AS confirmed,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'showed') AS shows,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) IN ('noshow','no show','no-show')) AS no_shows,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'cancelled') AS canceled,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.pipeline_stage_id = :won_stage) AS won,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.pipeline_stage_id = :dq_stage) AS disqualified,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'showed' AND o.lead_quality IN {qual_in}) AS qualified,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_great) AS lq_great,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_ok) AS lq_ok,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_barely) AS lq_barely,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_dq) AS lq_dq,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE c.resp = 'yes')   AS yes_bookings,
                    COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE c.resp = 'maybe') AS maybe_bookings
                FROM nj_emails_cte njx
                JOIN ghl_contact g ON LOWER(g.email) = njx.email
                JOIN ghl_opportunity o ON o.ghl_contact_id = g.ghl_contact_id
                LEFT JOIN njcsv c ON c.email = njx.email
                WHERE (o.webinar_source_number = :N OR g.booked_call_webinar_series = :N)
            """).bindparams(
                N=N, wid=wid,
                now_ts=datetime.now(timezone.utc),
                won_stage=DEAL_WON_STAGE_ID,
                dq_stage=DISQUALIFIED_STAGE_ID,
                lq_great=LEAD_QUALITY_GREAT,
                lq_ok=LEAD_QUALITY_OK,
                lq_barely=LEAD_QUALITY_BARELY,
                lq_dq=LEAD_QUALITY_BAD_DQ,
            ))
            orow = r.mappings().one_or_none()
            if orow:
                nj_metrics["totalBookings"] = int(orow["total_bookings"] or 0)
                nj_metrics["uniqueBookers"] = int(orow["unique_bookers"] or 0)
                nj_metrics["totalCallsDatePassed"] = int(orow["calls_passed"] or 0)
                nj_metrics["confirmed"] = int(orow["confirmed"] or 0)
                nj_metrics["shows"] = int(orow["shows"] or 0)
                nj_metrics["noShows"] = int(orow["no_shows"] or 0)
                nj_metrics["canceled"] = int(orow["canceled"] or 0)
                nj_metrics["won"] = int(orow["won"] or 0)
                nj_metrics["disqualified"] = int(orow["disqualified"] or 0)
                nj_metrics["qualified"] = int(orow["qualified"] or 0)
                nj_metrics["leadQualityGreat"] = int(orow["lq_great"] or 0)
                nj_metrics["leadQualityOk"] = int(orow["lq_ok"] or 0)
                nj_metrics["leadQualityBarelyPassable"] = int(orow["lq_barely"] or 0)
                nj_metrics["leadQualityBadDq"] = int(orow["lq_dq"] or 0)
                nj_metrics["yesBookings"] = int(orow["yes_bookings"] or 0)
                nj_metrics["maybeBookings"] = int(orow["maybe_bookings"] or 0)

        # Yes/Maybe marked (from the uploaded Non-joiners CSV; 0 when none) and
        # Self-Reg marked (GHL form-date in window) for the cohort. These don't
        # need a broadcast, so they run whenever the cohort exists.
        if nonjoiner_emails:
            marked_params: dict[str, Any] = {"wid": wid}
            if has_window:
                marked_params["sr_start"] = prev_date
                marked_params["sr_end"] = current_date
            r = await db.execute(sa_text(f"""
                WITH {NJ_EMAILS_CTE}, {NJ_CSV_CTE}
                SELECT
                    COUNT(DISTINCT e.email) FILTER (WHERE c.resp = 'yes')   AS yes_marked,
                    COUNT(DISTINCT e.email) FILTER (WHERE c.resp = 'maybe') AS maybe_marked,
                    COUNT(DISTINCT e.email) FILTER (WHERE {sr_pred})        AS self_reg_marked
                FROM nj_emails_cte e
                LEFT JOIN njcsv c ON c.email = e.email
                LEFT JOIN ghl_contact g ON LOWER(g.email) = e.email
            """).bindparams(**marked_params))
            mrow = r.mappings().one_or_none()
            if mrow:
                nj_metrics["yesMarked"] = int(mrow["yes_marked"] or 0)
                nj_metrics["maybeMarked"] = int(mrow["maybe_marked"] or 0)
                if has_window:
                    sr = int(mrow["self_reg_marked"] or 0)
                    nj_metrics["selfRegMarked"] = sr
                    nj_metrics["lpRegs"] = sr

        # Default queried-but-empty cohort metrics to 0 so they render "0"
        # (we genuinely queried and found none) instead of "—". Mirrors the
        # per-list default-zero pattern; attendance only when a broadcast exists.
        nj_default_zero = [
            "totalBookings", "totalCallsDatePassed", "confirmed", "shows",
            "noShows", "canceled", "won", "disqualified", "qualified",
            "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
            "yesBookings", "maybeBookings",
        ]
        if has_window:
            nj_default_zero.extend(["selfRegMarked", "lpRegs"])
        if broadcast_id:
            nj_default_zero.extend([
                "totalRegs", "totalAttended", "total10MinPlus", "total30MinPlus",
                "yesAttended", "yes10MinPlus", "yes30MinPlus",
                "maybeAttended", "maybe10MinPlus", "maybe30MinPlus",
            ])
            if has_window:
                nj_default_zero.extend(["selfRegAttended", "selfReg10MinPlus"])
        for k in nj_default_zero:
            nj_metrics.setdefault(k, 0)

        # ── No List Data ──────────────────────────────────────────────
        # Contacts with any webinar-N signal NOT mapped to any Planning list
        # for this webinar. Use EXISTS anti-join.
        # Use LEFT JOIN anti-pattern on LOWER(email) — 156k planned emails
        # is too large for `NOT IN` without hash support; LEFT JOIN ... WHERE
        # planned.email IS NULL compiles to a hash anti-join.
        # LP Regs (= Self Reg Marked) for NO LIST DATA also counts contacts
        # with `webinar_registration_in_form_date` in this webinar's window
        # (prev, current] even when they're NOT on a planned list.
        has_window = bool(prev_date and current_date)
        # Postgres can take a UUID[] via the asyncpg driver. We pass the
        # union of (this webinar + sibling variants) so the planned CTE
        # excludes everyone covered by any plan for this number.
        nld_params: dict[str, Any] = {
            "yes_re": yes_re, "maybe_re": maybe_re,
            "nj_re": series_nj_re, "N": N,
        }
        if has_window:
            nld_params["sr_start"] = prev_date
            nld_params["sr_end"] = current_date
            relevant_window_pred = (
                "OR (g.webinar_registration_in_form_date > :sr_start "
                "AND g.webinar_registration_in_form_date <= :sr_end)"
            )
            self_reg_filter = (
                "wrd > :sr_start AND wrd <= :sr_end"
            )
        else:
            relevant_window_pred = ""
            self_reg_filter = "FALSE"

        # When an Added-to-Calendar CSV with responses exists for this
        # webinar, NLD Yes/Maybe count CSV rows with matched_assignment_id
        # IS NULL — emails that landed in the CSV but didn't match any list
        # for this specific webinar_id. The unplanned/booked/lp_regs
        # aggregates stay GHL-driven (those signals don't come from the
        # CSV).
        csv_mode_nld = await _csv_mode_for_webinar(db, wid)
        if csv_mode_nld:
            nld_yes_pred = "lem IN (SELECT lem FROM csv_yes)"
            nld_maybe_pred = "lem IN (SELECT lem FROM csv_maybe)"
            nld_csv_prefix = f"""
                csv_yes AS (
                    SELECT LOWER(email) AS lem FROM webinar_calendar_invites
                    WHERE webinar_id = CAST(:wid AS uuid)
                      AND LOWER(calendar_invite_response) = 'yes'
                ),
                csv_maybe AS (
                    SELECT LOWER(email) AS lem FROM webinar_calendar_invites
                    WHERE webinar_id = CAST(:wid AS uuid)
                      AND LOWER(calendar_invite_response) = 'maybe'
                ),
            """
            # Add CSV-only rows to `relevant` so emails that responded yes/maybe
            # in the CSV but aren't in ghl_contact still land in NLD when they
            # don't match a planned list. ghl_contact_id is NULL for those rows;
            # the aggregate uses COALESCE(ghl_contact_id, lem) so they get
            # counted by email instead of being dropped by COUNT(DISTINCT NULL).
            relevant_csv_union = """
                UNION
                SELECT NULL::text AS ghl_contact_id, lem,
                       NULL::text AS irh, NULL::int AS bcws, NULL::date AS wrd
                FROM csv_yes
                UNION
                SELECT NULL::text AS ghl_contact_id, lem,
                       NULL::text AS irh, NULL::int AS bcws, NULL::date AS wrd
                FROM csv_maybe
            """
            nld_params["wid"] = wid
        else:
            nld_yes_pred = "irh ~* :yes_re"
            nld_maybe_pred = "irh ~* :maybe_re"
            nld_csv_prefix = ""
            relevant_csv_union = ""

        # Carve nonjoiners out of the leftover pool so the signals we just
        # attributed to the Nonjoiners row aren't double-counted here (each
        # contact lands in exactly one of planned / nonjoiners / NLD).
        if nonjoiner_emails:
            nj_cte_sql = NJ_EMAILS_CTE + ","
            nj_join_sql = "LEFT JOIN nj_emails_cte njx ON njx.email = r.lem"
            nj_filter_sql = "AND njx.email IS NULL"
        else:
            nj_cte_sql = nj_join_sql = nj_filter_sql = ""

        nld_counts_sql = f"""
            WITH
            {nld_csv_prefix}
            {nj_cte_sql}
            relevant AS (
                SELECT g.ghl_contact_id, LOWER(g.email) AS lem,
                       g.calendar_invite_response_history AS irh,
                       g.booked_call_webinar_series AS bcws,
                       g.webinar_registration_in_form_date AS wrd
                FROM ghl_contact g
                WHERE g.calendar_invite_response_history ~* :yes_re
                   OR g.calendar_invite_response_history ~* :maybe_re
                   OR g.calendar_webinar_series_non_joiners ~* :nj_re
                   OR g.booked_call_webinar_series = :N
                   OR g.webinar_registration_number = :N
                   {relevant_window_pred}
                {relevant_csv_union}
            ),
            planned AS (
                -- pre-materialized by _synthetic_special_rows into an indexed
                -- session temp table (chunked fill; see above)
                SELECT email FROM tmp_nld_planned
            ),
            unplanned AS (
                SELECT r.*
                FROM relevant r
                LEFT JOIN planned p ON p.email = r.lem
                {nj_join_sql}
                WHERE p.email IS NULL
                  {nj_filter_sql}
            )
            SELECT
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem))                          AS total_unplanned,
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem)) FILTER (WHERE {nld_yes_pred})   AS yes_unplanned,
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem)) FILTER (WHERE {nld_maybe_pred}) AS maybe_unplanned,
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem)) FILTER (WHERE bcws = :N)        AS booked_unplanned,
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem)) FILTER (WHERE {self_reg_filter}) AS lp_regs_unplanned,
                COUNT(DISTINCT COALESCE(ghl_contact_id, lem)) FILTER (WHERE ({self_reg_filter}) AND bcws = :N) AS self_reg_bookings_unplanned
            FROM unplanned
        """
        r = await db.execute(sa_text(nld_counts_sql).bindparams(**nld_params))
        row = r.one_or_none()
        total_u, yes_u, maybe_u, booked_u, lp_regs_u, sr_bookings_u = (
            (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0), int(row[5] or 0))
            if row else (0, 0, 0, 0, 0, 0)
        )

        # NLD contacts are *not* part of the invite pool — we didn't send
        # them invites; they appeared via GHL signals or as unmatched CSV
        # rows. Setting `invited` to None blanks the column in the table
        # and zeroes-out the per-1k Yes/Maybe ratios that divide by it.
        nld_metrics: dict[str, float | None] = {
            "accountsNeeded": None,
            "invited": None,
            "actuallyUsed": None,
            "yesMarked": yes_u,
            "maybeMarked": maybe_u,
            "selfRegMarked": lp_regs_u,
            "lpRegs": lp_regs_u,
            "selfRegBookings": sr_bookings_u,
            "totalBookings": booked_u,
            # NLD's booking count is already distinct-contact (COUNT DISTINCT
            # COALESCE(ghl_contact_id, email)), so unique == total here.
            "uniqueBookers": booked_u,
        }

        # WG attendance for the unplanned pool: anyone who attended the
        # broadcast whose email isn't on any planned list for this webinar
        # (or its sibling variants). Per-list rows already attribute planned
        # attendees; this row catches the rest so the WG attendance total
        # ties out to the broadcast.
        if broadcast_id:
            ATT = "(wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)"
            wg_nld_params: dict[str, Any] = {
                "bid": broadcast_id,
            }
            if csv_mode_nld:
                wg_yes_pred = "LOWER(wgs.email) IN (SELECT lem FROM csv_yes)"
                wg_maybe_pred = "LOWER(wgs.email) IN (SELECT lem FROM csv_maybe)"
                wg_csv_prefix = nld_csv_prefix
                wg_nld_params["wid"] = wid
            else:
                wg_yes_pred = "g.calendar_invite_response_history ~* :yes_re"
                wg_maybe_pred = "g.calendar_invite_response_history ~* :maybe_re"
                wg_csv_prefix = ""
                wg_nld_params["yes_re"] = yes_re
                wg_nld_params["maybe_re"] = maybe_re

            if has_window:
                wg_sr_pred = (
                    "g.webinar_registration_in_form_date > :sr_start "
                    "AND g.webinar_registration_in_form_date <= :sr_end"
                )
                wg_nld_params["sr_start"] = prev_date
                wg_nld_params["sr_end"] = current_date
            else:
                wg_sr_pred = "FALSE"

            # Exclude nonjoiner attendees here too — their attendance is shown
            # on the Nonjoiners row, so it must not also count in NLD.
            if nonjoiner_emails:
                wg_nj_cte_sql = NJ_EMAILS_CTE + ","
                wg_nj_join_sql = "LEFT JOIN nj_emails_cte njx ON njx.email = LOWER(wgs.email)"
                wg_nj_filter_sql = "AND njx.email IS NULL"
            else:
                wg_nj_cte_sql = wg_nj_join_sql = wg_nj_filter_sql = ""

            wg_nld_sql = f"""
                WITH
                {wg_csv_prefix}
                {wg_nj_cte_sql}
                planned AS (
                    -- pre-materialized session temp table (see chunked fill above)
                    SELECT email FROM tmp_nld_planned
                )
                SELECT
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT})                                                  AS total_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 10)                    AS ten_min,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 30)                    AS thirty_min,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_yes_pred})                                AS yes_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_yes_pred}   AND wgs.minutes_viewing >= 10) AS yes_10m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_yes_pred}   AND wgs.minutes_viewing >= 30) AS yes_30m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_maybe_pred})                              AS maybe_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_maybe_pred} AND wgs.minutes_viewing >= 10) AS maybe_10m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND {wg_maybe_pred} AND wgs.minutes_viewing >= 30) AS maybe_30m,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND ({wg_sr_pred}))                               AS self_reg_attended,
                    COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE {ATT} AND ({wg_sr_pred}) AND wgs.minutes_viewing >= 10) AS self_reg_10m
                FROM webinargeek_subscribers wgs
                LEFT JOIN planned p ON p.email = LOWER(wgs.email)
                LEFT JOIN ghl_contact g ON LOWER(g.email) = LOWER(wgs.email)
                {wg_nj_join_sql}
                WHERE wgs.broadcast_id = :bid
                  AND p.email IS NULL
                  {wg_nj_filter_sql}
            """
            r = await db.execute(sa_text(wg_nld_sql).bindparams(**wg_nld_params))
            wrow = r.mappings().one_or_none()
            if wrow:
                # yes/maybe attendance + 10m breakdown only comes from synced
                # subscribers (we need the email match to know who responded).
                nld_metrics["yesAttended"] = int(wrow["yes_attended"] or 0)
                nld_metrics["yes10MinPlus"] = int(wrow["yes_10m"] or 0)
                nld_metrics["yes30MinPlus"] = int(wrow["yes_30m"] or 0)
                nld_metrics["maybeAttended"] = int(wrow["maybe_attended"] or 0)
                nld_metrics["maybe10MinPlus"] = int(wrow["maybe_10m"] or 0)
                nld_metrics["maybe30MinPlus"] = int(wrow["maybe_30m"] or 0)
                nld_metrics["total10MinPlus"] = int(wrow["ten_min"] or 0)
                nld_metrics["total30MinPlus"] = int(wrow["thirty_min"] or 0)
                if has_window:
                    nld_metrics["selfRegAttended"] = int(wrow["self_reg_attended"] or 0)
                    nld_metrics["selfReg10MinPlus"] = int(wrow["self_reg_10m"] or 0)

                # totalRegs / totalAttended on NLD reflect the WG-vs-planned
                # gap: anything WG reports (including no-email registrants
                # that never sync into webinargeek_subscribers) that we
                # couldn't attribute to a planned list. Falls back to the
                # synced-only unplanned count when WG cache is missing.
                wg_totals_nld = await _fetch_wg_broadcast_totals(db, broadcast_id)
                if wg_totals_nld is not None:
                    matched = await db.execute(sa_text(
                        """
                        WITH planned AS (
                            -- pre-materialized session temp table (chunked fill above)
                            SELECT email FROM tmp_nld_planned
                        )
                        SELECT
                          COUNT(DISTINCT LOWER(wgs.email)) AS planned_regs,
                          COUNT(DISTINCT LOWER(wgs.email)) FILTER (WHERE wgs.watched_live = TRUE OR wgs.minutes_viewing > 0) AS planned_attended
                        FROM webinargeek_subscribers wgs
                        JOIN planned p ON p.email = LOWER(wgs.email)
                        WHERE wgs.broadcast_id = :bid
                        """
                    ).bindparams(bid=broadcast_id))
                    mrow = matched.mappings().one()
                    planned_regs = int(mrow["planned_regs"] or 0)
                    planned_attended = int(mrow["planned_attended"] or 0)
                    # Subtract nonjoiners too — they're now their own partition,
                    # so the NLD remainder = broadcast − planned − nonjoiners.
                    nld_total_regs = max(0, wg_totals_nld["subscriptions_count"] - planned_regs - nonjoiner_regs)
                    nld_total_attended = max(0, wg_totals_nld["live_viewers_count"] - planned_attended - nonjoiner_attended)
                    nld_metrics["totalRegs"] = nld_total_regs
                    nld_metrics["totalAttended"] = nld_total_attended
                    total_u = max(total_u, nld_total_regs)
                else:
                    nld_metrics["totalAttended"] = int(wrow["total_attended"] or 0)
                    total_u = max(total_u, int(wrow["total_attended"] or 0))

        # Order rows: display_order later than any real list (negative means first; 999999 keeps them at the end)
        rows_out: list[dict[str, Any]] = []
        if nj_count > 0:
            rows_out.append({
                "workbookRow": 999998,
                "kind": "nonjoiners",
                "status": w.status,
                "note": None,
                "listUrl": None,
                "description": "Nonjoiners",
                "listName": None,
                "sendInfo": None,
                "senderColor": None,
                "bucketId": None,
                "bucketName": None,
                "descLabel": None,
                "titleText": None,
                "createdDate": None,
                "industry": None,
                "employeeRange": None,
                "country": None,
                "metrics": nj_metrics,
            })
        if total_u > 0:
            rows_out.append({
                "workbookRow": 999999,
                "kind": "no_list_data",
                "status": w.status,
                "note": None,
                "listUrl": None,
                "description": "NO LIST DATA",
                "listName": None,
                "sendInfo": None,
                "senderColor": None,
                "bucketId": None,
                "bucketName": None,
                "descLabel": None,
                "titleText": None,
                "createdDate": None,
                "industry": None,
                "employeeRange": None,
                "country": None,
                "metrics": nld_metrics,
                # When sibling variants exist for this number, the GHL
                # custom-field signals (Yes/Maybe/booked) carry only N —
                # they cannot be split between variants. The same numbers
                # therefore appear on both variants' NO LIST DATA rows. The
                # frontend reads this flag to show a "shared signals" tag.
                "sharedAcrossVariants": bool(sibling_ids),
            })
        return rows_out

    @staticmethod
    async def _assignment_chunks(
        db: AsyncSession, wid, max_members: int = 25_000,
    ) -> list[list]:
        """Greedy-pack a webinar's assignment_ids into chunks of ≤max_members
        memberships. Chunks partition the webinar's contacts (UNIQUE(webinar_id,
        contact_id)), so whole-webinar aggregates can run per chunk and be
        SUMMED per group key exactly — the pattern that keeps every statement
        under Supabase's hard 120s cap on 100k+-member webinars. Emails are
        unique within a webinar's memberships (verified on prod), so summed
        COUNT(DISTINCT email) stays exact."""
        from sqlalchemy import text as sa_text
        import uuid as _uuid

        rows = (await db.execute(sa_text(
            "SELECT assignment_id, count(*) FROM webinar_contact_memberships "
            "WHERE webinar_id = CAST(:wid AS uuid) AND assignment_id IS NOT NULL "
            "GROUP BY assignment_id ORDER BY count(*) DESC"
        ).bindparams(wid=wid))).all()
        chunks: list[list] = []
        cur: list = []
        cur_n = 0
        for aid, n in rows:
            if cur and cur_n + int(n or 0) > max_members:
                chunks.append(cur)
                cur, cur_n = [], 0
            cur.append(_uuid.UUID(str(aid)))
            cur_n += int(n or 0)
        if cur:
            chunks.append(cur)
        return chunks

    async def _compute_per_list_metrics(
        self,
        db: AsyncSession,
        w: Webinar,
        assignments: list[WebinarListAssignment],
        prev_date,
        current_date,
    ) -> dict[str, dict[str, float | None]]:
        """Return {assignment_id: partial_metrics_dict} with GHL/WG metrics
        filtered to the planning contacts of each list.

        Uses `webinar_contact_memberships.assignment_id` (per-webinar) to map
        Planning emails → list. Joins to ghl_contact via lowercase email. Only
        lists whose planned contacts actually exist in GHL will show counts > 0.

        Performance: every metric that shares a join shape is computed via
        FILTER (WHERE …) inside one grouped query — three batched queries
        replace ~20 separate scans of the planning + ghl_contact join.
        """
        from sqlalchemy import text as sa_text

        N = w.number
        broadcast_id = w.broadcast_id
        yes_re = _invite_response_regex(N, "Yes")
        maybe_re = _invite_response_regex(N, "Maybe")
        series_re = _webinar_series_regex(N)
        wid = w.id

        out: dict[str, dict[str, float | None]] = {a.id: {} for a in assignments}
        if not assignments:
            return out

        # actuallyUsed per list — live count of contacts marked sent. Drops
        # when contacts are released back to the bucket pool, while volume
        # (planned) stays the same so plan vs. actual stays comparable.
        used_q = await db.execute(
            select(WebinarContactMembership.assignment_id, func.count())
            .where(
                WebinarContactMembership.assignment_id.in_([a.id for a in assignments]),
                WebinarContactMembership.status == "used",
            )
            .group_by(WebinarContactMembership.assignment_id)
        )
        for aid, cnt in used_q.all():
            if str(aid) in out:
                out[str(aid)]["actuallyUsed"] = int(cnt or 0)
        for aid in out:
            out[aid].setdefault("actuallyUsed", 0)

        # ── Assignment chunking ────────────────────────────────────────────
        # Large webinars (150k+ members) push a single whole-webinar batch
        # query past Supabase's hard 120s statement_timeout (W152: 256k members
        # → ~150s → canceled; the cap can't be lifted through the pooler).
        # Greedy-pack assignments into chunks of ≤ ~60k members and run each
        # batch per chunk — chunks partition the webinar's contacts (UNIQUE
        # (webinar_id, contact_id)), and every batch below GROUPs BY
        # m.assignment_id, so per-chunk results are disjoint and merge exactly.
        # Small webinars pack into one chunk → same single query as before.
        # 25k leaves real headroom under live prod load — 60k chunks ran 30–90s
        # and the slowest crossed the 120s cap when the app was busy.
        _CHUNK_MEMBERS = 25_000
        mem_cnt_q = await db.execute(
            select(WebinarContactMembership.assignment_id, func.count())
            .where(
                WebinarContactMembership.webinar_id == wid,
                WebinarContactMembership.assignment_id.in_([a.id for a in assignments]),
            )
            .group_by(WebinarContactMembership.assignment_id)
        )
        _mem_counts = {str(aid): int(cnt or 0) for aid, cnt in mem_cnt_q.all()}
        import uuid as _uuid
        aid_chunks: list[list] = []
        _cur: list = []
        _cur_n = 0
        # Biggest lists first so a single huge list gets its own chunk.
        for a in sorted(assignments, key=lambda a: -_mem_counts.get(str(a.id), 0)):
            n = _mem_counts.get(str(a.id), 0)
            if _cur and _cur_n + n > _CHUNK_MEMBERS:
                aid_chunks.append(_cur)
                _cur, _cur_n = [], 0
            _cur.append(_uuid.UUID(str(a.id)))
            _cur_n += n
        if _cur:
            aid_chunks.append(_cur)

        from sqlalchemy import bindparam as sa_bindparam
        _AIDS_PRED = "AND m.assignment_id IN :aids"

        # Chunked batches probe ≤60k rows — the default cost model's nested
        # loop wins there; the caller's SET LOCAL random_page_cost=8 (tuned for
        # the whole-webinar scans) would force hash joins that rescan
        # ghl_contact per chunk. Restore the default for this txn; the caller
        # re-issues its tuning after per-list where needed.
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))

        has_window = bool(prev_date and current_date)

        # When an Added-to-Calendar CSV with responses exists for this
        # webinar, source Yes/Maybe from webinar_calendar_invites
        # (calendar_invite_response = 'Yes'/'Maybe') instead of regex-parsing
        # ghl_contact.calendar_invite_response_history. CSV is the primary
        # source; the regex path remains as fallback when no CSV exists.
        csv_mode = await _csv_mode_for_webinar(db, wid)
        if csv_mode:
            yes_pred = "LOWER(c.email) IN (SELECT lem FROM csv_yes)"
            maybe_pred = "LOWER(c.email) IN (SELECT lem FROM csv_maybe)"
            csv_cte_prefix = _csv_yes_maybe_ctes()
        else:
            yes_pred = "g.calendar_invite_response_history ~* :yes_re"
            maybe_pred = "g.calendar_invite_response_history ~* :maybe_re"
            csv_cte_prefix = ""

        # ── Batch A: ghl_contact-only counts (one scan of the planned join) ─
        # Includes yes/maybe marked, gcal invited, yes/maybe bookings,
        # self-reg, self-reg bookings, unsubscribes.
        # In CSV mode the SQL doesn't reference :yes_re/:maybe_re — and
        # SQLAlchemy's text() rejects bind params that aren't in the SQL —
        # so they're only added when the regex fallback is active.
        ghl_params: dict[str, Any] = {
            "wid": wid,
            "series_re": series_re,
            "N": N,
        }
        if not csv_mode:
            ghl_params["yes_re"] = yes_re
            ghl_params["maybe_re"] = maybe_re
        window_filter_marker = "FALSE"
        if has_window:
            ghl_params["sr_start"] = prev_date
            ghl_params["sr_end"] = current_date
            window_filter_marker = "g.webinar_registration_in_form_date > :sr_start AND g.webinar_registration_in_form_date <= :sr_end"
            unsub_filter = "g.cold_calendar_unsubscribe_date > :sr_start AND g.cold_calendar_unsubscribe_date <= :sr_end"
        else:
            unsub_filter = "FALSE"
        # LEFT JOIN ghl_contact so CSV-yes/maybe contacts who haven't been
        # synced into ghl_contact still count toward yes_marked/maybe_marked.
        # The GHL-rooted FILTER predicates below (gcal/bookings/self_reg/
        # unsubscribes) naturally evaluate to false when g is NULL, so those
        # metrics still correctly attribute only to contacts present in GHL.
        ghl_sql = f"""
            {csv_cte_prefix}
            SELECT
                m.assignment_id,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {yes_pred}) AS yes_marked,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {maybe_pred}) AS maybe_marked,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE g.calendar_webinar_series_history ~* :series_re) AS gcal_invited_ghl,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {yes_pred} AND g.booked_call_webinar_series = :N) AS yes_bookings,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {maybe_pred} AND g.booked_call_webinar_series = :N) AS maybe_bookings,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {window_filter_marker}) AS self_reg_marked,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE ({window_filter_marker}) AND g.booked_call_webinar_series = :N) AS self_reg_bookings,
                COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {unsub_filter}) AS unsubscribes
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN ghl_contact g ON LOWER(g.email) = LOWER(c.email)
            WHERE m.webinar_id = CAST(:wid AS uuid)
              {_AIDS_PRED}
            GROUP BY m.assignment_id
        """
        ghl_rows = []
        for _aids in aid_chunks:
            r = await db.execute(
                sa_text(ghl_sql).bindparams(sa_bindparam("aids", expanding=True), **ghl_params),
                {"aids": _aids},
            )
            ghl_rows.extend(r.mappings().all())
        for row in ghl_rows:
            aid = str(row["assignment_id"]) if row["assignment_id"] is not None else None
            if aid is None or aid not in out:
                continue
            m = out[aid]
            m["yesMarked"] = int(row["yes_marked"] or 0)
            m["maybeMarked"] = int(row["maybe_marked"] or 0)
            m["gcalInvitedGhl"] = int(row["gcal_invited_ghl"] or 0)
            m["yesBookings"] = int(row["yes_bookings"] or 0)
            m["maybeBookings"] = int(row["maybe_bookings"] or 0)
            if has_window:
                self_reg = int(row["self_reg_marked"] or 0)
                m["selfRegMarked"] = self_reg
                m["lpRegs"] = self_reg
                m["selfRegBookings"] = int(row["self_reg_bookings"] or 0)
                m["unsubscribes"] = int(row["unsubscribes"] or 0)

        # ── Batch B: WG attendance (one scan of planned + WG join) ───────
        if broadcast_id:
            ATT = "(wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)"
            wg_window_filter = window_filter_marker if has_window else "FALSE"
            wg_params: dict[str, Any] = {
                "wid": wid,
                "bid": broadcast_id,
            }
            if not csv_mode:
                wg_params["yes_re"] = yes_re
                wg_params["maybe_re"] = maybe_re
            if has_window:
                wg_params["sr_start"] = prev_date
                wg_params["sr_end"] = current_date

            wg_sql = f"""
                {csv_cte_prefix}
                SELECT
                    m.assignment_id,
                    COUNT(DISTINCT LOWER(c.email)) AS total_regs,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT}) AS total_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 10) AS total_10m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 30) AS total_30m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND g.has_sms_click_tag = TRUE) AS sms_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {yes_pred}) AS yes_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {yes_pred} AND wgs.minutes_viewing >= 10) AS yes_10m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {yes_pred} AND wgs.minutes_viewing >= 30) AS yes_30m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {yes_pred} AND g.has_sms_click_tag = TRUE) AS yes_sms,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {maybe_pred}) AS maybe_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {maybe_pred} AND wgs.minutes_viewing >= 10) AS maybe_10m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {maybe_pred} AND wgs.minutes_viewing >= 30) AS maybe_30m,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND {maybe_pred} AND g.has_sms_click_tag = TRUE) AS maybe_sms,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND ({wg_window_filter})) AS self_reg_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE {ATT} AND ({wg_window_filter}) AND wgs.minutes_viewing >= 10) AS self_reg_10m
                FROM webinargeek_subscribers wgs
                JOIN contacts c ON LOWER(c.email) = LOWER(wgs.email)
                JOIN webinar_contact_memberships m
                  ON m.contact_id = c.id AND m.webinar_id = CAST(:wid AS uuid)
                LEFT JOIN ghl_contact g ON LOWER(g.email) = LOWER(c.email)
                WHERE wgs.broadcast_id = :bid
                GROUP BY m.assignment_id
            """
            # Driven from the broadcast's registrants (~few thousand) probing
            # contacts via ix_contacts_lower_email + memberships via
            # uq_wcm_webinar_contact — no chunking needed; the contacts-driven
            # form flaked past the 120s cap on 100k+-member webinars.
            r = await db.execute(sa_text(wg_sql).bindparams(**wg_params))
            wg_rows = r.mappings().all()
            for row in wg_rows:
                aid = str(row["assignment_id"]) if row["assignment_id"] is not None else None
                if aid is None or aid not in out:
                    continue
                m = out[aid]
                m["totalRegs"] = int(row["total_regs"] or 0)
                m["totalAttended"] = int(row["total_attended"] or 0)
                m["total10MinPlus"] = int(row["total_10m"] or 0)
                m["total30MinPlus"] = int(row["total_30m"] or 0)
                m["attendBySmsReminder"] = int(row["sms_attended"] or 0)
                m["yesAttended"] = int(row["yes_attended"] or 0)
                m["yes10MinPlus"] = int(row["yes_10m"] or 0)
                m["yes30MinPlus"] = int(row["yes_30m"] or 0)
                m["yesAttendBySmsClick"] = int(row["yes_sms"] or 0)
                m["maybeAttended"] = int(row["maybe_attended"] or 0)
                m["maybe10MinPlus"] = int(row["maybe_10m"] or 0)
                m["maybe30MinPlus"] = int(row["maybe_30m"] or 0)
                m["maybeAttendBySmsClick"] = int(row["maybe_sms"] or 0)
                if has_window:
                    m["selfRegAttended"] = int(row["self_reg_attended"] or 0)
                    m["selfReg10MinPlus"] = int(row["self_reg_10m"] or 0)

        # ── Batch C: opportunity counts (one scan of planned + opp join) ─
        # Union of (opp.webinar_source_number = N) or (contact.booked_call = N)
        # is enforced in WHERE; per-bucket counts use FILTER.
        qual_in = "('" + "', '".join(QUALIFIED_SET) + "')"
        # Inverted join: drive from the handful of opportunities matching this
        # webinar's series (UNION splits the cross-table OR so each branch scans
        # its own small side) and probe contacts via ix_contacts_lower_email +
        # memberships via uq_wcm_webinar_contact. The old contacts-side join
        # scanned the whole membership set and timed out on 100k+-member
        # webinars regardless of chunking (the planner drove from the big side).
        opp_sql = f"""
            WITH ser AS (
                SELECT o.ghl_opportunity_id, o.call1_appointment_date,
                       o.call1_appointment_status, o.pipeline_stage_id,
                       o.lead_quality, g.ghl_contact_id, LOWER(g.email) AS lem
                FROM ghl_opportunity o
                JOIN ghl_contact g ON g.ghl_contact_id = o.ghl_contact_id
                WHERE o.webinar_source_number = :N
                UNION
                SELECT o.ghl_opportunity_id, o.call1_appointment_date,
                       o.call1_appointment_status, o.pipeline_stage_id,
                       o.lead_quality, g.ghl_contact_id, LOWER(g.email) AS lem
                FROM ghl_contact g
                JOIN ghl_opportunity o ON o.ghl_contact_id = g.ghl_contact_id
                WHERE g.booked_call_webinar_series = :N
            )
            SELECT
                m.assignment_id,
                COUNT(DISTINCT s.ghl_opportunity_id) AS total_bookings,
                COUNT(DISTINCT s.ghl_contact_id) AS unique_bookers,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.call1_appointment_date IS NOT NULL AND s.call1_appointment_date <= :now_ts) AS calls_passed,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(s.call1_appointment_status, '')) = 'confirmed') AS confirmed,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(s.call1_appointment_status, '')) = 'showed') AS shows,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(s.call1_appointment_status, '')) IN ('noshow','no show','no-show')) AS no_shows,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(s.call1_appointment_status, '')) = 'cancelled') AS canceled,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.pipeline_stage_id = :won_stage) AS won,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.pipeline_stage_id = :dq_stage) AS disqualified,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(s.call1_appointment_status, '')) = 'showed' AND s.lead_quality IN {qual_in}) AS qualified,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.lead_quality = :lq_great) AS lq_great,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.lead_quality = :lq_ok) AS lq_ok,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.lead_quality = :lq_barely) AS lq_barely,
                COUNT(DISTINCT s.ghl_opportunity_id) FILTER (WHERE s.lead_quality = :lq_dq) AS lq_dq
            FROM ser s
            JOIN contacts c ON LOWER(c.email) = s.lem
            JOIN webinar_contact_memberships m
              ON m.contact_id = c.id AND m.webinar_id = CAST(:wid AS uuid)
            GROUP BY m.assignment_id
        """
        r = await db.execute(sa_text(opp_sql).bindparams(
            wid=wid, N=N,
            now_ts=datetime.now(timezone.utc),
            won_stage=DEAL_WON_STAGE_ID,
            dq_stage=DISQUALIFIED_STAGE_ID,
            lq_great=LEAD_QUALITY_GREAT,
            lq_ok=LEAD_QUALITY_OK,
            lq_barely=LEAD_QUALITY_BARELY,
            lq_dq=LEAD_QUALITY_BAD_DQ,
        ))
        opp_rows = r.mappings().all()
        for row in opp_rows:
            aid = str(row["assignment_id"]) if row["assignment_id"] is not None else None
            if aid is None or aid not in out:
                continue
            m = out[aid]
            m["totalBookings"] = int(row["total_bookings"] or 0)
            m["uniqueBookers"] = int(row["unique_bookers"] or 0)
            m["totalCallsDatePassed"] = int(row["calls_passed"] or 0)
            m["confirmed"] = int(row["confirmed"] or 0)
            m["shows"] = int(row["shows"] or 0)
            m["noShows"] = int(row["no_shows"] or 0)
            m["canceled"] = int(row["canceled"] or 0)
            m["won"] = int(row["won"] or 0)
            m["disqualified"] = int(row["disqualified"] or 0)
            m["qualified"] = int(row["qualified"] or 0)
            m["leadQualityGreat"] = int(row["lq_great"] or 0)
            m["leadQualityOk"] = int(row["lq_ok"] or 0)
            m["leadQualityBarelyPassable"] = int(row["lq_barely"] or 0)
            m["leadQualityBadDq"] = int(row["lq_dq"] or 0)

        # Default any keys we queried to 0 for lists that had no hits — so the
        # UI shows "0" instead of "—" (we genuinely queried and found none).
        default_zero = [
            "yesMarked", "maybeMarked", "gcalInvitedGhl",
            "yesBookings", "maybeBookings",
            "totalBookings", "uniqueBookers", "totalCallsDatePassed", "confirmed", "shows", "noShows",
            "canceled", "won", "disqualified", "qualified",
            "leadQualityGreat", "leadQualityOk", "leadQualityBarelyPassable", "leadQualityBadDq",
        ]
        if has_window:
            default_zero.extend(["selfRegMarked", "selfRegBookings", "lpRegs", "unsubscribes"])
        if broadcast_id:
            default_zero.extend([
                "totalRegs", "totalAttended", "total10MinPlus", "total30MinPlus",
                "attendBySmsReminder",
                "yesAttended", "yes10MinPlus", "yes30MinPlus", "yesAttendBySmsClick",
                "maybeAttended", "maybe10MinPlus", "maybe30MinPlus", "maybeAttendBySmsClick",
            ])
            if has_window:
                default_zero.extend(["selfRegAttended", "selfReg10MinPlus"])
        for aid, m in out.items():
            for k in default_zero:
                m.setdefault(k, 0)

        return out


    async def _compute_per_source_cells(
        self, db: AsyncSession, w: Webinar,
    ) -> list[dict[str, Any]]:
        """Per (data-source, vintage) funnel cells for a webinar's cold lists —
        the data behind the "By List Source" tab.

        Mirrors _compute_per_list_metrics' definitions of registered
        (webinargeek_subscribers presence), attended (watched_live OR
        minutes_viewing>0 / >=10) and booked (opp.webinar_source_number OR
        ghl_contact.booked_call_webinar_series = N), but groups by
        contacts.lead_list_name instead of assignment_id, then rolls the raw
        list names up to (source, vintage) via the parser helpers. Nonjoiners /
        no-list-data assignments are excluded — this tab is about cold sourcing.
        invited = distinct cold contacts of that list (actual leads mailed, not
        the planned volume the per-list rows use).
        """
        from sqlalchemy import text as sa_text

        wid = w.id
        N = w.number
        bid = w.broadcast_id
        # wla.id IS NOT NULL keeps the LEFT-JOINed membership rows of DELETED
        # lists out of the cold funnels (legacy INNER-JOIN semantics) — without
        # it, a deleted NONJOINER list's contacts would leak in as "cold".
        cold = (
            "wla.id IS NOT NULL "
            "AND COALESCE(wla.is_nonjoiners, false) = false "
            "AND COALESCE(wla.is_no_list_data, false) = false"
        )
        lln_expr = "COALESCE(NULLIF(c.lead_list_name, ''), '(no label)')"

        # Chunked probes + small-side joins below both want nested loops — the
        # caller's whole-webinar hash-join tuning (random_page_cost=8) would
        # rescan contacts per chunk instead.
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))

        # {lead_list_name: {raw metric: count}} — merged across the batches.
        raw: dict[str, dict[str, int]] = {}

        # invited: distinct cold contacts per list name. This scans every
        # membership of the webinar, so run it per assignment-chunk (exact under
        # summation — chunks partition contacts, emails unique per webinar) to
        # stay under the 120s statement cap on 100k+-member webinars.
        from sqlalchemy import bindparam as sa_bindparam
        inv_sql = f"""
            SELECT {lln_expr} AS lln, COUNT(DISTINCT LOWER(c.email)) AS invited
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
            WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
              AND m.assignment_id IN :aids
            GROUP BY 1
        """
        for _aids in await self._assignment_chunks(db, wid):
            r = await db.execute(
                sa_text(inv_sql).bindparams(sa_bindparam("aids", expanding=True), wid=wid),
                {"aids": _aids},
            )
            for row in r.mappings().all():
                slot = raw.setdefault(row["lln"], {})
                slot["invited"] = slot.get("invited", 0) + int(row["invited"] or 0)

        # regs / attended / 10m: WebinarGeek subscriber join (needs a broadcast).
        if bid:
            wg_sql = f"""
                SELECT {lln_expr} AS lln,
                    COUNT(DISTINCT LOWER(c.email)) AS total_regs,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (
                        WHERE wgs.watched_live = TRUE OR wgs.minutes_viewing > 0
                    ) AS total_attended,
                    COUNT(DISTINCT LOWER(c.email)) FILTER (
                        WHERE (wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)
                        AND wgs.minutes_viewing >= 10
                    ) AS total_10m
                FROM contacts c
                JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
                JOIN webinargeek_subscribers wgs
                    ON LOWER(wgs.email) = LOWER(c.email) AND wgs.broadcast_id = :bid
                WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
                GROUP BY 1
            """
            r = await db.execute(sa_text(wg_sql).bindparams(wid=wid, bid=bid))
            for row in r.mappings().all():
                slot = raw.setdefault(row["lln"], {})
                slot["totalRegs"] = int(row["total_regs"] or 0)
                slot["totalAttended"] = int(row["total_attended"] or 0)
                slot["total10MinPlus"] = int(row["total_10m"] or 0)

        # booked + sales/quality: opportunity join, matched to this webinar
        # series number. Mirrors the FILTER predicates in _compute_per_list_metrics
        # (Batch C) so the By List Source tab shows the same Sales/Quality columns
        # as the Segments tab.
        qual_in = "('" + "', '".join(QUALIFIED_SET) + "')"
        bk_sql = f"""
            SELECT {lln_expr} AS lln,
                COUNT(DISTINCT o.ghl_opportunity_id) AS total_bookings,
                COUNT(DISTINCT g.ghl_contact_id) AS unique_bookers,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'confirmed') AS confirmed,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'showed') AS shows,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) IN ('noshow','no show','no-show')) AS no_shows,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'cancelled') AS canceled,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.pipeline_stage_id = :won_stage) AS won,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.pipeline_stage_id = :dq_stage) AS disqualified,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE LOWER(COALESCE(o.call1_appointment_status, '')) = 'showed' AND o.lead_quality IN {qual_in}) AS qualified,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_great) AS lq_great,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_ok) AS lq_ok,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_barely) AS lq_barely,
                COUNT(DISTINCT o.ghl_opportunity_id) FILTER (WHERE o.lead_quality = :lq_dq) AS lq_dq
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
            JOIN ghl_contact g ON LOWER(g.email) = LOWER(c.email)
            JOIN ghl_opportunity o ON o.ghl_contact_id = g.ghl_contact_id
            WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
              AND (o.webinar_source_number = :N OR g.booked_call_webinar_series = :N)
            GROUP BY 1
        """
        r = await db.execute(sa_text(bk_sql).bindparams(
            wid=wid, N=N,
            won_stage=DEAL_WON_STAGE_ID, dq_stage=DISQUALIFIED_STAGE_ID,
            lq_great=LEAD_QUALITY_GREAT, lq_ok=LEAD_QUALITY_OK,
            lq_barely=LEAD_QUALITY_BARELY, lq_dq=LEAD_QUALITY_BAD_DQ,
        ))
        for row in r.mappings().all():
            slot = raw.setdefault(row["lln"], {})
            slot["totalBookings"] = int(row["total_bookings"] or 0)
            slot["uniqueBookers"] = int(row["unique_bookers"] or 0)
            slot["confirmed"] = int(row["confirmed"] or 0)
            slot["shows"] = int(row["shows"] or 0)
            slot["noShows"] = int(row["no_shows"] or 0)
            slot["canceled"] = int(row["canceled"] or 0)
            slot["won"] = int(row["won"] or 0)
            slot["disqualified"] = int(row["disqualified"] or 0)
            slot["qualified"] = int(row["qualified"] or 0)
            slot["leadQualityGreat"] = int(row["lq_great"] or 0)
            slot["leadQualityOk"] = int(row["lq_ok"] or 0)
            slot["leadQualityBarelyPassable"] = int(row["lq_barely"] or 0)
            slot["leadQualityBadDq"] = int(row["lq_dq"] or 0)

        # Roll raw list names up to (source, vintage) cells.
        cells: dict[tuple[str, str], dict[str, Any]] = {}
        for lln, metrics in raw.items():
            name = None if lln == "(no label)" else lln
            source = _lead_source_from_list_name(name)
            vintage = _lead_vintage_from_list_name(name, w.date)
            key = (source, vintage)
            cell = cells.get(key)
            if cell is None:
                cell = {
                    "source": source,
                    "vintage": vintage,
                    "metrics": {k: 0 for k in SOURCE_FUNNEL_RAW_KEYS},
                }
                cells[key] = cell
            cm = cell["metrics"]
            for k in SOURCE_FUNNEL_RAW_KEYS:
                v = metrics.get(k)
                if v is not None:
                    cm[k] += int(v)
        return list(cells.values())


    async def _compute_per_employee_cells(
        self, db: AsyncSession, w: Webinar, split_by_bucket: bool = False,
    ) -> list[dict[str, Any]]:
        """Per company-size funnel cells for a webinar's cold lists — the data
        behind the "Employee count" tab.

        A trimmed clone of _compute_per_source_cells: same three funnel queries
        (invited / WebinarGeek regs+attended / opportunity bookings+quality),
        same cold filter and metric set, but groups by the canonical company-size
        bucket ("0 - 2", "3 - 5", … "10000+"; see bucket_expr below) instead of
        lead_list_name. The bucket is derived from the numeric employee_count when
        present, else a stored canonical employee_range label; everything else
        (foreign ranges, stray dates, no size) lands in "(no size)".

        With `split_by_bucket`, each query also emits a segment × company-size
        cross via GROUPING SETS — one extra aggregation pass over the SAME scan,
        so the webinar-wide rows and the per-segment rows cost barely more than
        the webinar-wide rows alone. Those cells carry a "bucketId" and feed the
        Segments-tab drill-down + suggested range straight out of the snapshot
        (computing them live took ~50s per webinar per segment). Cells without a
        "bucketId" are the webinar-wide rollup (the Employee count tab).
        """
        from sqlalchemy import text as sa_text

        wid = w.id
        N = w.number
        bid = w.broadcast_id
        # wla.id IS NOT NULL keeps the LEFT-JOINed membership rows of DELETED
        # lists out of the cold funnels (legacy INNER-JOIN semantics) — without
        # it, a deleted NONJOINER list's contacts would leak in as "cold".
        cold = (
            "wla.id IS NOT NULL "
            "AND COALESCE(wla.is_nonjoiners, false) = false "
            "AND COALESCE(wla.is_no_list_data, false) = false"
        )
        # Canonical company-size buckets — mirror _EMPLOYEE_BUCKETS in
        # api/routers/outreach/uploads.py (inclusive upper bound, ascending).
        # The tab may only ever show these labels plus "(no size)": prefer the
        # numeric headcount when present, else accept a stored canonical label,
        # else collapse anything foreign (Apollo-style ranges like "5 - 50",
        # stray export dates, legacy labels) into "(no size)".
        _emp_buckets = (
            (2, "0 - 2"), (5, "3 - 5"), (10, "6 - 10"), (20, "11 - 20"),
            (50, "21 - 50"), (100, "51 - 100"), (200, "101 - 200"),
            (500, "201 - 500"), (1000, "501 - 1000"), (2000, "1001 - 2000"),
            (5000, "2001 - 5000"), (10000, "5001 - 10000"),
        )
        _emp_overflow = "10000+"
        _emp_labels = [lbl for _, lbl in _emp_buckets] + [_emp_overflow]
        _count_case = "\n              ".join(
            f"WHEN c.employee_count <= {ceil} THEN '{lbl}'"
            for ceil, lbl in _emp_buckets
        )
        _canonical_in = ", ".join(f"'{lbl}'" for lbl in _emp_labels)
        bucket_expr = f"""CASE
            WHEN c.employee_count IS NOT NULL THEN
              CASE
              {_count_case}
              ELSE '{_emp_overflow}'
              END
            WHEN c.employee_range IN ({_canonical_in}) THEN c.employee_range
            ELSE '(no size)'
          END"""

        # Aggregation shape. Without the split it's the plain per-size rollup;
        # with it, GROUPING SETS adds the segment × size cross in the same scan.
        # GROUPING(seg) separates the rollup row from a genuine NULL bucket_id
        # (an assignment with no segment attached).
        seg_select = "t.seg, GROUPING(t.seg) AS is_rollup, "
        group_by = (
            "GROUP BY GROUPING SETS ((t.bucket), (t.bucket, t.seg))"
            if split_by_bucket else "GROUP BY t.bucket"
        )
        if not split_by_bucket:
            seg_select = "NULL::text AS seg, 1 AS is_rollup, "

        # Chunked probes + small-side joins want nested loops (see
        # _compute_per_source_cells).
        await db.execute(sa_text("SET LOCAL random_page_cost = 4"))

        # {(bucketId|None, size bucket): {raw metric: count}} — merged across the
        # batches. bucketId None = the webinar-wide rollup row.
        raw: dict[tuple[str | None, str], dict[str, int]] = {}

        def _slot(row) -> dict[str, int]:
            seg = None if int(row["is_rollup"] or 0) == 1 else (
                str(row["seg"]) if row["seg"] is not None else "__none__"
            )
            return raw.setdefault((seg, row["bucket"]), {})

        # invited: distinct cold contacts per size bucket — chunked per
        # assignment like per-source (exact under summation).
        from sqlalchemy import bindparam as sa_bindparam
        inv_sql = f"""
            SELECT t.bucket, {seg_select}COUNT(DISTINCT t.email) AS invited
            FROM (
                SELECT {bucket_expr} AS bucket,
                    CAST(wla.bucket_id AS text) AS seg,
                    LOWER(c.email) AS email
                FROM contacts c
                JOIN webinar_contact_memberships m ON m.contact_id = c.id
                LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
                WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
                  AND m.assignment_id IN :aids
            ) t
            {group_by}
        """
        for _aids in await self._assignment_chunks(db, wid):
            r = await db.execute(
                sa_text(inv_sql).bindparams(sa_bindparam("aids", expanding=True), wid=wid),
                {"aids": _aids},
            )
            for row in r.mappings().all():
                slot = _slot(row)
                slot["invited"] = slot.get("invited", 0) + int(row["invited"] or 0)

        # regs / attended / 10m: WebinarGeek subscriber join (needs a broadcast).
        if bid:
            wg_sql = f"""
                SELECT t.bucket, {seg_select}
                    COUNT(DISTINCT t.email) AS total_regs,
                    COUNT(DISTINCT t.email) FILTER (WHERE t.attended) AS total_attended,
                    COUNT(DISTINCT t.email) FILTER (
                        WHERE t.attended AND t.minutes_viewing >= 10
                    ) AS total_10m
                FROM (
                    SELECT {bucket_expr} AS bucket,
                        CAST(wla.bucket_id AS text) AS seg,
                        LOWER(c.email) AS email,
                        (wgs.watched_live = TRUE OR wgs.minutes_viewing > 0) AS attended,
                        COALESCE(wgs.minutes_viewing, 0) AS minutes_viewing
                    FROM contacts c
                    JOIN webinar_contact_memberships m ON m.contact_id = c.id
                    LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
                    JOIN webinargeek_subscribers wgs
                        ON LOWER(wgs.email) = LOWER(c.email) AND wgs.broadcast_id = :bid
                    WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
                ) t
                {group_by}
            """
            r = await db.execute(sa_text(wg_sql).bindparams(wid=wid, bid=bid))
            for row in r.mappings().all():
                slot = _slot(row)
                slot["totalRegs"] = int(row["total_regs"] or 0)
                slot["totalAttended"] = int(row["total_attended"] or 0)
                slot["total10MinPlus"] = int(row["total_10m"] or 0)

        # booked + sales/quality: opportunity join, matched to this webinar
        # series number (mirrors _compute_per_source_cells' Batch C predicates).
        qual_in = "('" + "', '".join(QUALIFIED_SET) + "')"
        bk_sql = f"""
            SELECT t.bucket, {seg_select}
                COUNT(DISTINCT t.opp_id) AS total_bookings,
                COUNT(DISTINCT t.ghl_contact_id) AS unique_bookers,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.appt_status = 'confirmed') AS confirmed,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.appt_status = 'showed') AS shows,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.appt_status IN ('noshow','no show','no-show')) AS no_shows,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.appt_status = 'cancelled') AS canceled,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.stage_id = :won_stage) AS won,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.stage_id = :dq_stage) AS disqualified,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.appt_status = 'showed' AND t.lead_quality IN {qual_in}) AS qualified,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.lead_quality = :lq_great) AS lq_great,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.lead_quality = :lq_ok) AS lq_ok,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.lead_quality = :lq_barely) AS lq_barely,
                COUNT(DISTINCT t.opp_id) FILTER (WHERE t.lead_quality = :lq_dq) AS lq_dq
            FROM (
                SELECT {bucket_expr} AS bucket,
                    CAST(wla.bucket_id AS text) AS seg,
                    o.ghl_opportunity_id AS opp_id,
                    g.ghl_contact_id AS ghl_contact_id,
                    LOWER(COALESCE(o.call1_appointment_status, '')) AS appt_status,
                    o.pipeline_stage_id AS stage_id,
                    o.lead_quality AS lead_quality
                FROM contacts c
                JOIN webinar_contact_memberships m ON m.contact_id = c.id
                LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
                JOIN ghl_contact g ON LOWER(g.email) = LOWER(c.email)
                JOIN ghl_opportunity o ON o.ghl_contact_id = g.ghl_contact_id
                WHERE m.webinar_id = CAST(:wid AS uuid) AND {cold}
                  AND (o.webinar_source_number = :N OR g.booked_call_webinar_series = :N)
            ) t
            {group_by}
        """
        r = await db.execute(sa_text(bk_sql).bindparams(
            wid=wid, N=N,
            won_stage=DEAL_WON_STAGE_ID, dq_stage=DISQUALIFIED_STAGE_ID,
            lq_great=LEAD_QUALITY_GREAT, lq_ok=LEAD_QUALITY_OK,
            lq_barely=LEAD_QUALITY_BARELY, lq_dq=LEAD_QUALITY_BAD_DQ,
        ))
        for row in r.mappings().all():
            slot = _slot(row)
            slot["totalBookings"] = int(row["total_bookings"] or 0)
            slot["uniqueBookers"] = int(row["unique_bookers"] or 0)
            slot["confirmed"] = int(row["confirmed"] or 0)
            slot["shows"] = int(row["shows"] or 0)
            slot["noShows"] = int(row["no_shows"] or 0)
            slot["canceled"] = int(row["canceled"] or 0)
            slot["won"] = int(row["won"] or 0)
            slot["disqualified"] = int(row["disqualified"] or 0)
            slot["qualified"] = int(row["qualified"] or 0)
            slot["leadQualityGreat"] = int(row["lq_great"] or 0)
            slot["leadQualityOk"] = int(row["lq_ok"] or 0)
            slot["leadQualityBarelyPassable"] = int(row["lq_barely"] or 0)
            slot["leadQualityBadDq"] = int(row["lq_dq"] or 0)

        # One cell per size bucket (webinar-wide), plus one per segment × size
        # when split_by_bucket. Cells for memberships whose list carries no
        # segment are dropped from the split ("__none__") — the drill-down is
        # per named segment.
        cells: list[dict[str, Any]] = []
        for (seg, bucket), metrics in raw.items():
            if seg == "__none__":
                continue
            cell: dict[str, Any] = {
                "bucket": bucket,
                "metrics": {k: int(metrics.get(k) or 0) for k in SOURCE_FUNNEL_RAW_KEYS},
            }
            if seg is not None:
                cell["bucketId"] = seg
            cells.append(cell)
        return cells


    async def _compute_webinar_summary(
        self,
        db: AsyncSession,
        w: Webinar,
        assignments: list[WebinarListAssignment],
        prev_date,
        current_date,
    ) -> dict[str, float | None]:
        """DEPRECATED / UNUSED — not called from any live path (the active per-webinar
        summary is `_summary_from_per_list`). Kept only for reference. Do NOT revive
        without fixing attribution: `_compute_webinar_metrics` below keys sales purely
        on the webinar NUMBER with no membership join, so it conflates same-number A/B
        variants and predates the per-webinar `webinar_booking_attribution` model.

        Aggregated base metrics (sum over lists) + webinar-wide GHL/WG metrics."""
        summary: dict[str, float | None] = {
            # Base — aggregate from lists
            "accountsNeeded": sum((a.accounts_used or 0) for a in assignments),
            "invited": sum((a.volume or 0) for a in assignments),
        }

        # Fold in webinar-wide GHL/WG metrics (overwrites any base keys they share — none)
        webinar_wide = await self._compute_webinar_metrics(db, w, prev_date, current_date)
        for k, v in webinar_wide.items():
            if k not in summary:  # keep our summed base values
                summary[k] = v
        return summary

    async def _compute_webinar_metrics(
        self,
        db: AsyncSession,
        w: Webinar,
        prev_date,
        current_date,
    ) -> dict[str, float | None]:
        """Webinar-wide GHL/WG metrics. Same FILTER batching pattern as
        _compute_per_list_metrics — groups everything that shares a join shape
        into a single grouped query.
        """
        from sqlalchemy import text as sa_text

        N = w.number
        broadcast_id = w.broadcast_id
        metrics: dict[str, float | None] = {}

        # --- Base (from app) ---
        base = await _webinar_summary_from_app(db, w.id)
        metrics.update(base)

        # --- gcalInvitedGhl: read from ghl_webinar_stats cache (populated during sync) ---
        r = await db.execute(
            select(GHLWebinarStats.gcal_invited_count)
            .where(GHLWebinarStats.webinar_number == N)
        )
        metrics["gcalInvitedGhl"] = r.scalar()

        yes_re = _invite_response_regex(N, "Yes")
        maybe_re = _invite_response_regex(N, "Maybe")
        has_window = bool(prev_date and current_date)

        # CSV-source Yes/Maybe takes precedence over GHL regex when a
        # complete Added-to-Calendar upload with responses exists for this
        # webinar (see _csv_mode_for_webinar). At the aggregate path the
        # email reference is g.email (no contacts join here).
        csv_mode = await _csv_mode_for_webinar(db, w.id)
        if csv_mode:
            yes_pred = "LOWER(g.email) IN (SELECT lem FROM csv_yes)"
            maybe_pred = "LOWER(g.email) IN (SELECT lem FROM csv_maybe)"
            csv_cte_prefix = _csv_yes_maybe_ctes()
        else:
            yes_pred = "g.calendar_invite_response_history ~* :yes_re"
            maybe_pred = "g.calendar_invite_response_history ~* :maybe_re"
            csv_cte_prefix = ""

        # ── Batch A: ghl_contact-only counts (one scan) ──────────────────
        # See per-list note: only bind :yes_re/:maybe_re in fallback mode.
        ghl_params: dict[str, Any] = {"N": N}
        if csv_mode:
            ghl_params["wid"] = w.id
        else:
            ghl_params["yes_re"] = yes_re
            ghl_params["maybe_re"] = maybe_re
        if has_window:
            ghl_params["sr_start"] = prev_date
            ghl_params["sr_end"] = current_date
            window_filter = "g.webinar_registration_in_form_date > :sr_start AND g.webinar_registration_in_form_date <= :sr_end"
            unsub_filter = "g.cold_calendar_unsubscribe_date > :sr_start AND g.cold_calendar_unsubscribe_date <= :sr_end"
        else:
            window_filter = "FALSE"
            unsub_filter = "FALSE"
        # In CSV mode the source of truth for yes/maybe is the CSV itself,
        # not ghl_contact. Use scalar subqueries against csv_yes/csv_maybe so
        # CSV responders who haven't been synced to GHL still count toward
        # the webinar-level Marked totals. Bookings/self-reg/unsubs still
        # need GHL data (they're stored on ghl_contact), so they keep
        # scanning ghl_contact.
        if csv_mode:
            ym_sql = "(SELECT COUNT(*) FROM csv_yes)"
            mm_sql = "(SELECT COUNT(*) FROM csv_maybe)"
        else:
            ym_sql = f"COUNT(g.ghl_contact_id) FILTER (WHERE {yes_pred})"
            mm_sql = f"COUNT(g.ghl_contact_id) FILTER (WHERE {maybe_pred})"
        ghl_sql = f"""
            {csv_cte_prefix}
            SELECT
                {ym_sql} AS yes_marked,
                {mm_sql} AS maybe_marked,
                COUNT(g.ghl_contact_id) FILTER (WHERE {yes_pred} AND g.booked_call_webinar_series = :N) AS yes_bookings,
                COUNT(g.ghl_contact_id) FILTER (WHERE {maybe_pred} AND g.booked_call_webinar_series = :N) AS maybe_bookings,
                COUNT(g.ghl_contact_id) FILTER (WHERE {window_filter}) AS self_reg_marked,
                COUNT(g.ghl_contact_id) FILTER (WHERE ({window_filter}) AND g.booked_call_webinar_series = :N) AS self_reg_bookings,
                COUNT(g.ghl_contact_id) FILTER (WHERE {unsub_filter}) AS unsubscribes
            FROM ghl_contact g
        """
        r = await db.execute(sa_text(ghl_sql).bindparams(**ghl_params))
        row = r.mappings().one()
        metrics["yesMarked"] = int(row["yes_marked"] or 0)
        metrics["maybeMarked"] = int(row["maybe_marked"] or 0)
        metrics["yesBookings"] = int(row["yes_bookings"] or 0)
        metrics["maybeBookings"] = int(row["maybe_bookings"] or 0)
        if has_window:
            self_reg = int(row["self_reg_marked"] or 0)
            metrics["selfRegMarked"] = self_reg
            metrics["lpRegs"] = self_reg
            metrics["selfRegBookings"] = int(row["self_reg_bookings"] or 0)
            metrics["unsubscribes"] = int(row["unsubscribes"] or 0)
        else:
            for k in ("selfRegMarked", "selfRegBookings", "unsubscribes"):
                metrics[k] = None

        # ── Batch B: WG attendance (one scan) ────────────────────────────
        if broadcast_id:
            ATT = "(wgs.watched_live = TRUE OR wgs.minutes_viewing > 0)"
            wg_window_filter = window_filter if has_window else "FALSE"
            wg_params: dict[str, Any] = {"bid": broadcast_id}
            if csv_mode:
                wg_params["wid"] = w.id
            else:
                wg_params["yes_re"] = yes_re
                wg_params["maybe_re"] = maybe_re
            if has_window:
                wg_params["sr_start"] = prev_date
                wg_params["sr_end"] = current_date

            # In CSV mode, predicates resolve via the csv_yes/csv_maybe CTEs;
            # rows are matched on LOWER(wgs.email) since not every WG
            # attendee has a ghl_contact row (LEFT JOIN below).
            if csv_mode:
                yes_pred_wg = "LOWER(wgs.email) IN (SELECT lem FROM csv_yes)"
                maybe_pred_wg = "LOWER(wgs.email) IN (SELECT lem FROM csv_maybe)"
            else:
                yes_pred_wg = "g.calendar_invite_response_history ~* :yes_re"
                maybe_pred_wg = "g.calendar_invite_response_history ~* :maybe_re"

            wg_sql = f"""
                {csv_cte_prefix}
                SELECT
                    COUNT(*) AS total_regs,
                    COUNT(*) FILTER (WHERE {ATT}) AS total_attended,
                    COUNT(*) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 10) AS total_10m,
                    COUNT(*) FILTER (WHERE {ATT} AND wgs.minutes_viewing >= 30) AS total_30m,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND g.has_sms_click_tag = TRUE) AS sms_attended,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {yes_pred_wg}) AS yes_attended,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {yes_pred_wg} AND wgs.minutes_viewing >= 10) AS yes_10m,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {yes_pred_wg} AND wgs.minutes_viewing >= 30) AS yes_30m,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {yes_pred_wg} AND g.has_sms_click_tag = TRUE) AS yes_sms,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {maybe_pred_wg}) AS maybe_attended,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {maybe_pred_wg} AND wgs.minutes_viewing >= 10) AS maybe_10m,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {maybe_pred_wg} AND wgs.minutes_viewing >= 30) AS maybe_30m,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND {maybe_pred_wg} AND g.has_sms_click_tag = TRUE) AS maybe_sms,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND ({wg_window_filter})) AS self_reg_attended,
                    COUNT(DISTINCT g.ghl_contact_id) FILTER (WHERE {ATT} AND ({wg_window_filter}) AND wgs.minutes_viewing >= 10) AS self_reg_10m
                FROM webinargeek_subscribers wgs
                LEFT JOIN ghl_contact g ON LOWER(g.email) = LOWER(wgs.email)
                WHERE wgs.broadcast_id = :bid
            """
            r = await db.execute(sa_text(wg_sql).bindparams(**wg_params))
            row = r.mappings().one()
            metrics["totalRegs"] = int(row["total_regs"] or 0)
            metrics["totalAttended"] = int(row["total_attended"] or 0)
            metrics["total10MinPlus"] = int(row["total_10m"] or 0)
            metrics["total30MinPlus"] = int(row["total_30m"] or 0)
            metrics["attendBySmsReminder"] = int(row["sms_attended"] or 0)
            metrics["yesAttended"] = int(row["yes_attended"] or 0)
            metrics["yes10MinPlus"] = int(row["yes_10m"] or 0)
            metrics["yes30MinPlus"] = int(row["yes_30m"] or 0)
            metrics["yesAttendBySmsClick"] = int(row["yes_sms"] or 0)
            metrics["maybeAttended"] = int(row["maybe_attended"] or 0)
            metrics["maybe10MinPlus"] = int(row["maybe_10m"] or 0)
            metrics["maybe30MinPlus"] = int(row["maybe_30m"] or 0)
            metrics["maybeAttendBySmsClick"] = int(row["maybe_sms"] or 0)
            if has_window:
                metrics["selfRegAttended"] = int(row["self_reg_attended"] or 0)
                metrics["selfReg10MinPlus"] = int(row["self_reg_10m"] or 0)
            else:
                metrics["selfRegAttended"] = None
                metrics["selfReg10MinPlus"] = None
        else:
            for k in (
                "totalRegs", "totalAttended", "total10MinPlus", "total30MinPlus", "attendBySmsReminder",
                "yesAttended", "yes10MinPlus", "yes30MinPlus", "yesAttendBySmsClick",
                "maybeAttended", "maybe10MinPlus", "maybe30MinPlus", "maybeAttendBySmsClick",
                "selfRegAttended", "selfReg10MinPlus",
            ):
                metrics[k] = None

        # ── Sales: load the opp set once, compute everything in Python ───
        # The union of (opp.webinar_source_number = N) and
        # (contact.booked_call_webinar_series = N) is small (handful → low
        # hundreds), so a single SELECT then bucketing in Python is faster
        # and simpler than 14 FILTER aggregates against the join.
        r = await db.execute(sa_text("""
            SELECT DISTINCT ON (o.ghl_opportunity_id)
                   o.ghl_opportunity_id,
                   o.call1_appointment_date,
                   o.call1_appointment_status,
                   o.pipeline_stage_id,
                   o.lead_quality,
                   o.projected_deal_size_value,
                   o.monetary_value
            FROM ghl_opportunity o
            LEFT JOIN ghl_contact g ON g.ghl_contact_id = o.ghl_contact_id
            WHERE o.webinar_source_number = :N
               OR g.booked_call_webinar_series = :N
        """).bindparams(N=N))
        opps = r.mappings().all()
        n_opps = len(opps)

        def cnt(pred) -> int:
            return sum(1 for o in opps if pred(o))

        now_utc = datetime.now(timezone.utc)
        metrics["totalBookings"] = n_opps
        metrics["totalCallsDatePassed"] = cnt(
            lambda o: o["call1_appointment_date"] is not None and o["call1_appointment_date"] <= now_utc
        )
        metrics["confirmed"] = cnt(lambda o: (o["call1_appointment_status"] or "").lower() == "confirmed")
        metrics["shows"] = cnt(lambda o: (o["call1_appointment_status"] or "").lower() == "showed")
        metrics["noShows"] = cnt(lambda o: (o["call1_appointment_status"] or "").lower() in ("noshow", "no show", "no-show"))
        metrics["canceled"] = cnt(lambda o: (o["call1_appointment_status"] or "").lower() == "cancelled")
        metrics["won"] = cnt(lambda o: o["pipeline_stage_id"] == DEAL_WON_STAGE_ID)
        metrics["disqualified"] = cnt(lambda o: o["pipeline_stage_id"] == DISQUALIFIED_STAGE_ID)

        metrics["leadQualityGreat"] = cnt(lambda o: o["lead_quality"] == LEAD_QUALITY_GREAT)
        metrics["leadQualityOk"] = cnt(lambda o: o["lead_quality"] == LEAD_QUALITY_OK)
        metrics["leadQualityBarelyPassable"] = cnt(lambda o: o["lead_quality"] == LEAD_QUALITY_BARELY)
        metrics["leadQualityBadDq"] = cnt(lambda o: o["lead_quality"] == LEAD_QUALITY_BAD_DQ)

        metrics["qualified"] = cnt(
            lambda o: (o["call1_appointment_status"] or "").lower() == "showed"
            and o["lead_quality"] in QUALIFIED_SET
        )

        proj_vals = [o["projected_deal_size_value"] for o in opps if o["projected_deal_size_value"]]
        metrics["avgProjectedDealSize"] = (sum(proj_vals) / len(proj_vals)) if proj_vals else None

        won_vals = [
            float(o["monetary_value"]) for o in opps
            if o["pipeline_stage_id"] == DEAL_WON_STAGE_ID and o["monetary_value"] is not None
        ]
        metrics["avgClosedDealValue"] = sum(won_vals) if won_vals else None

        metrics.setdefault("lpRegs", None)

        return metrics


async def get_last_sync_summary() -> dict | None:
    """Return latest completed GHL sync metadata for UI badge on Statistics page."""
    from db.models import GHLSyncRun
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(GHLSyncRun)
            .where(GHLSyncRun.status.in_(["completed", "running"]))
            .order_by(GHLSyncRun.started_at.desc())
            .limit(1)
        )
        run = r.scalar_one_or_none()
        if run is None:
            return None
        return {
            "run_id": run.id,
            "sync_type": run.sync_type,
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "contacts_synced": run.contacts_synced,
            "opportunities_synced": run.opportunities_synced,
        }
