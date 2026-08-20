"""
Statistics router — read-only dashboard data from workbook fixture.
All routes require bearer auth.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.auth import require_auth
from services import statistics as stats_svc

router = APIRouter(dependencies=[Depends(require_auth)])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StatisticsMetrics(BaseModel):
    # Raw source fields
    gcalInvitedGhl: float | None = None
    accountsNeeded: float | None = None
    invited: float | None = None
    actuallyUsed: float | None = None
    unsubscribes: float | None = None
    lpRegs: float | None = None
    yesMarked: float | None = None
    yesAttended: float | None = None
    yes10MinPlus: float | None = None
    yes30MinPlus: float | None = None
    yesAttendBySmsClick: float | None = None
    yesBookings: float | None = None
    maybeMarked: float | None = None
    maybeAttended: float | None = None
    maybe10MinPlus: float | None = None
    maybe30MinPlus: float | None = None
    maybeAttendBySmsClick: float | None = None
    maybeBookings: float | None = None
    selfRegMarked: float | None = None
    selfRegAttended: float | None = None
    selfReg10MinPlus: float | None = None
    selfRegBookings: float | None = None
    totalRegs: float | None = None
    totalAttended: float | None = None
    attendBySmsReminder: float | None = None
    total10MinPlus: float | None = None
    total30MinPlus: float | None = None
    totalBookings: float | None = None
    uniqueBookers: float | None = None  # distinct booked contacts (displayed "Bookings")
    totalCallsDatePassed: float | None = None
    confirmed: float | None = None
    shows: float | None = None
    noShows: float | None = None
    canceled: float | None = None
    won: float | None = None
    disqualified: float | None = None
    qualified: float | None = None
    leadQualityGreat: float | None = None
    leadQualityOk: float | None = None
    leadQualityBarelyPassable: float | None = None
    leadQualityBadDq: float | None = None
    avgProjectedDealSize: float | None = None
    avgClosedDealValue: float | None = None

    # Derived fields
    unsubPercent: float | None = None
    yesPer1kInv: float | None = None
    yesPercent: float | None = None
    yesAttendPercent: float | None = None
    yesStay10MinPercent: float | None = None
    yesStay30MinPercent: float | None = None
    yesAttendBySmsClickPercent: float | None = None
    yesBookingsPer1kInv: float | None = None
    maybePer1kInv: float | None = None
    maybeAttendPercent: float | None = None
    maybeStay10MinPercent: float | None = None
    maybeStay30MinPercent: float | None = None
    maybeAttendBySmsClickPercent: float | None = None
    maybeBookingsPer1kInv: float | None = None
    selfRegPer1kInv: float | None = None
    selfRegAttendPercent: float | None = None
    selfRegStay10MinPercent: float | None = None
    selfRegBookingsPer1kInv: float | None = None
    invitedToRegPercent: float | None = None
    totalRegsPer1kInv: float | None = None
    regToAttendPercent: float | None = None
    invitedToAttendPercent: float | None = None
    totalAttendedPer1kInv: float | None = None
    attendBySmsReminderPercent: float | None = None
    total10MinPlusPer1kInv: float | None = None
    attend10MinPercent: float | None = None
    total30MinPlusPer1kInv: float | None = None
    attend30MinPercent: float | None = None
    bookingsPerAttended: float | None = None
    bookingsPerPast10Min: float | None = None
    totalBookingsPer1kInv: float | None = None
    showPercent: float | None = None
    closeRatePercent: float | None = None
    qualPercent: float | None = None


class StatisticsCopy(BaseModel):
    id: str
    text: str
    variantIndex: int


class ApiStatisticsRow(BaseModel):
    id: str
    webinarNumber: int
    workbookRow: int
    assignmentId: str | None = None
    kind: str  # "list" | "nonjoiners" | "no_list_data"
    status: str | None = None
    note: str | None = None
    listUrl: str | None = None
    description: str | None = None
    listName: str | None = None
    sendInfo: str | None = None
    senderColor: str | None = None
    bucketId: str | None = None
    bucketName: str | None = None
    descLabel: str | None = None
    titleText: str | None = None
    titleCopy: StatisticsCopy | None = None
    descCopy: StatisticsCopy | None = None
    segmentName: str | None = None
    createdDate: str | None = None
    industry: str | None = None
    employeeRange: str | None = None
    country: str | None = None
    metrics: StatisticsMetrics


class ApiStatisticsWebinar(BaseModel):
    id: str
    # Underlying Webinar.id (UUID). Required by the frontend for drill-down
    # requests so A/B variants (which share a number) resolve unambiguously.
    # Without it the model silently stripped the value and drill-downs fell
    # back to the bare number, 500-ing the contacts endpoint on variants.
    webinarId: str | None = None
    number: int
    variantLabel: str | None = None
    hasSiblingVariants: bool = False
    date: str | None = None
    title: str | None = None
    workbookRow: int
    source: str  # "workbook_mock"
    summary: StatisticsMetrics
    rows: list[ApiStatisticsRow]


class StatisticsMetaResponse(BaseModel):
    source: str  # "ghl" | "workbook"
    last_sync: dict | None = None


class StatisticsResponse(BaseModel):
    webinars: list[ApiStatisticsWebinar]
    meta: StatisticsMetaResponse


class ApiStatisticsWebinarSummary(BaseModel):
    """Lightweight webinar identity used by the progressive-load list."""
    id: str
    # Underlying Webinar UUID. The progressive-load endpoint addresses
    # rows by this value so A/B variants sharing a `number` don't collide.
    webinarId: str | None = None
    number: int
    # Free-text variant tag, e.g. "Account A". NULL for the unique row of
    # a non-variant number.
    variantLabel: str | None = None
    date: str | None = None
    title: str | None = None
    status: str | None = None
    listCount: int = 0
    broadcastId: str | None = None


class StatisticsListResponse(BaseModel):
    webinars: list[ApiStatisticsWebinarSummary]
    meta: StatisticsMetaResponse


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class ContactDrilldownItem(BaseModel):
    # Nullable: the webinar-wide opportunity drilldown LEFT-JOINs ghl_contact, so
    # an opportunity whose ghl_contact_id has no matching ghl_contact row (orphan
    # inbound/self-booked opp) emits NULL here — a required str 500'd the endpoint.
    ghl_contact_id: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    company_website: str | None = None
    assignment_id: str | None = None
    ghl_url: str
    # Booking-source UTMs (GHL contact "Book - Campaign *" fields)
    book_source: str | None = None
    book_medium: str | None = None
    book_name: str | None = None
    book_content: str | None = None
    book_term: str | None = None
    book_id: str | None = None
    # When metric unit is "opportunity"
    opportunity_id: str | None = None
    opportunity_url: str | None = None
    opportunity_stage_id: str | None = None
    opportunity_value: float | None = None
    owner: str | None = None
    call1_status: str | None = None
    call1_date: str | None = None
    call1_booking_date: str | None = None
    # Calendar the 1st call was booked on + its curated source label.
    call1_calendar_name: str | None = None
    call1_source_label: str | None = None
    webinar_source_number: int | None = None
    lead_quality: str | None = None


class ContactDrilldownResponse(BaseModel):
    metric: str
    webinar_number: int
    webinar_id: str | None = None
    assignment_id: str | None = None
    unit: str  # "contact" | "opportunity"
    total: int
    unique_total: int | None = None  # distinct booked contacts (headline "unique bookers")
    items: list[ContactDrilldownItem]
    available: bool
    reason: str | None = None


@router.get("/contacts", response_model=ContactDrilldownResponse)
async def list_contacts_for_metric(
    metric: str,
    webinar: int | None = None,
    webinar_id: str | None = None,
    assignment: str | None = None,
    limit: int = 500,
):
    """Return contacts (or opportunities) behind a specific metric on the
    Statistics dashboard. Each item has a GHL deep-link for opening the
    contact / opportunity in a new tab.

    With variants in play, prefer `webinar_id` (UUID) — it picks exactly
    one variant. The legacy `webinar` (number) param still works for
    back-compat: it resolves to the unlabeled webinar for that number, or
    fails if all rows for that number are labeled variants (in which case
    the caller must pass `webinar_id`).
    """
    from datetime import timedelta
    from sqlalchemy import select, text
    from db.models import Webinar as WebinarModel
    from db.session import AsyncSessionLocal
    from services.statistics_metric_filters import (
        spec_for_metric, build_contacts_query, build_webinar_wide_opp_query,
    )

    if webinar_id is None and webinar is None:
        raise HTTPException(400, "Either webinar (number) or webinar_id (UUID) is required")

    async with AsyncSessionLocal() as db:
        # Resolve webinar — UUID takes precedence; bare number falls back to
        # the unlabeled row.
        if webinar_id is not None:
            r = await db.execute(select(WebinarModel).where(WebinarModel.id == webinar_id))
            w = r.scalar_one_or_none()
        else:
            r = await db.execute(
                select(WebinarModel).where(
                    WebinarModel.number == webinar,
                    WebinarModel.variant_label.is_(None),
                )
            )
            w = r.scalar_one_or_none()
            if w is None:
                # All rows for this number are labeled variants; require explicit webinar_id.
                # Use .first() — scalar_one_or_none() itself raises when a number
                # has 2+ variants (the exact case we're detecting here).
                ambig = await db.execute(select(WebinarModel).where(WebinarModel.number == webinar))
                if ambig.scalars().first() is not None:
                    return {
                        "metric": metric, "webinar_number": webinar, "webinar_id": None,
                        "assignment_id": assignment, "unit": "contact", "total": 0, "items": [],
                        "available": False,
                        "reason": (
                            f"Webinar {webinar} has multiple variants — pass webinar_id to disambiguate."
                        ),
                    }
        if w is None:
            return {
                "metric": metric, "webinar_number": webinar or 0, "webinar_id": webinar_id,
                "assignment_id": assignment, "unit": "contact", "total": 0, "items": [],
                "available": False, "reason": "Webinar not found",
            }
        webinar = w.number  # for response field below

        # Compute prev_date — walk distinct numbers so sibling variants don't
        # count as "the previous webinar" for date windows.
        prev_r = await db.execute(
            select(WebinarModel)
            .where(WebinarModel.number < w.number)
            .order_by(WebinarModel.number.desc(), WebinarModel.date.desc())
            .limit(1)
        )
        prev_w = prev_r.scalar_one_or_none()
        prev_date = prev_w.date if prev_w else None
        current_date = w.date
        if prev_date is None and current_date is not None:
            prev_date = current_date - timedelta(days=30)

        spec = spec_for_metric(
            metric, w.number, broadcast_id=w.broadcast_id,
            prev_date=prev_date, current_date=current_date,
        )
        if spec is None:
            return {
                "metric": metric, "webinar_number": w.number, "webinar_id": w.id,
                "assignment_id": assignment, "unit": "contact", "total": 0, "items": [],
                "available": False, "reason": f"Metric '{metric}' not supported for drill-down",
            }
        if spec.unavailable:
            return {
                "metric": metric, "webinar_number": w.number, "webinar_id": w.id,
                "assignment_id": assignment, "unit": spec.unit, "total": 0, "items": [],
                "available": False,
                "reason": "Required data missing (broadcast not linked or no prior webinar for date window)",
            }

        # Opportunity-unit metrics drilled from the parent summary (no
        # assignment) use the webinar-wide query so the list ties out to the
        # displayed number — including inbound / self-booked opportunities that
        # were never in an outreach list. Per-list drilldowns (assignment set)
        # and contact-unit metrics keep the outreach-list-scoped query, which
        # already matches their per-list counts.
        unique_total: int | None = None
        if spec.unit == "opportunity" and assignment is None:
            list_sql, count_sql, params = build_webinar_wide_opp_query(spec, w.number, limit=limit)
            count_params = {k: v for k, v in params.items() if k != "limit"}
            crow = (await db.execute(text(count_sql).bindparams(**count_params))).mappings().first()
            total = int((crow or {}).get("total") or 0)
            unique_total = int((crow or {}).get("unique_total") or 0)
            r = await db.execute(text(list_sql).bindparams(**params))
            rows = r.mappings().all()
        else:
            sql, params = build_contacts_query(spec, w.id, assignment_id=assignment, limit=limit)
            r = await db.execute(text(sql).bindparams(**params))
            rows = r.mappings().all()
            total = len(rows)
            if spec.unit == "opportunity":
                unique_total = len({row.get("ghl_contact_id") for row in rows if row.get("ghl_contact_id")})

        from integrations.ghl_client import get_ghl_location_id
        loc = (await get_ghl_location_id()) or ""
        items: list[dict] = []
        for row in rows:
            contact_id = row.get("ghl_contact_id")
            opportunity_id = row.get("opportunity_id") if spec.unit == "opportunity" else None
            item = {
                "ghl_contact_id": contact_id,
                "email": row.get("email"),
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
                "company_website": row.get("company_website"),
                "assignment_id": str(row.get("assignment_id")) if row.get("assignment_id") else None,
                "ghl_url": f"https://app.gohighlevel.com/v2/location/{loc}/contacts/detail/{contact_id}" if contact_id else "",
                "book_source": row.get("book_source"),
                "book_medium": row.get("book_medium"),
                "book_name": row.get("book_name"),
                "book_content": row.get("book_content"),
                "book_term": row.get("book_term"),
                "book_id": row.get("book_id"),
            }
            if opportunity_id:
                item.update({
                    "opportunity_id": opportunity_id,
                    "opportunity_url": f"https://app.gohighlevel.com/v2/location/{loc}/opportunities/{opportunity_id}?tab=Opportunity+Details",
                    "opportunity_stage_id": row.get("pipeline_stage_id"),
                    "opportunity_value": float(row["monetary_value"]) if row.get("monetary_value") is not None else None,
                    "owner": row.get("owner_name"),
                    "call1_status": row.get("call1_appointment_status"),
                    "call1_date": row["call1_appointment_date"].isoformat() if row.get("call1_appointment_date") else None,
                    "call1_booking_date": row["call1_booking_date"].isoformat() if row.get("call1_booking_date") else None,
                    "call1_calendar_name": row.get("call1_calendar_name"),
                    "call1_source_label": row.get("call1_source_label"),
                    "webinar_source_number": row.get("webinar_source_number"),
                    "lead_quality": row.get("lead_quality"),
                })
            items.append(item)

        return {
            "metric": metric,
            "webinar_number": w.number,
            "webinar_id": w.id,
            "assignment_id": assignment,
            "unit": spec.unit,
            "total": total,
            "unique_total": unique_total,
            "items": items,
            "available": True,
            "reason": None,
        }


# Free / personal email providers — contacts on these domains are not tied to a
# company, so we flag them separately in the domain distribution.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "rocketmail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com",
    "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "gmx.com", "gmx.net",
    "gmx.de", "proton.me", "protonmail.com", "pm.me", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "fastmail.com", "hey.com", "tutanota.com",
    "web.de", "qq.com", "163.com", "126.com", "naver.com", "hanmail.net",
    "comcast.net", "verizon.net", "sbcglobal.net", "att.net", "btinternet.com",
})


class ListDistributionItem(BaseModel):
    list_name: str | None = None  # contacts.lead_list_name (None = no list name on the contact)
    count: int
    pct: float  # share of the scope's total contacts, 0–100


class DomainItem(BaseModel):
    domain: str | None = None  # email domain, lowercased (None = no parseable email)
    count: int
    pct: float  # share of contacts with a parseable email, 0–100
    is_free: bool = False  # True when the domain is a known free / personal provider


class DomainDistribution(BaseModel):
    total: int  # contacts with a parseable email address (the % denominator)
    unique_domains: int  # distinct domains across those contacts
    free_domain_contacts: int  # contacts whose domain is a free / personal provider
    free_domain_unique: int  # distinct free / personal domains seen
    top: list[DomainItem]  # top domains by contact count (capped)
    free: list[DomainItem]  # every free / personal domain seen, by contact count


class ListDistributionResponse(BaseModel):
    scope: str  # "assignment" | "webinar" | "bucket"
    assignment_id: str | None = None
    bucket_id: str | None = None
    webinar_id: str | None = None
    webinar_number: int | None = None
    label: str | None = None
    total: int
    items: list[ListDistributionItem]
    domains: DomainDistribution


@router.get("/list-distribution", response_model=ListDistributionResponse)
async def list_name_distribution(
    assignment: str | None = None,
    bucket: str | None = None,
    webinar_id: str | None = None,
    webinar: int | None = None,
):
    """Distribution of source list names (`contacts.lead_list_name`) and email
    domains for one of three scopes:

    - `assignment` — a single assigned list.
    - `bucket` (+ `webinar_id` / `webinar`) — every assigned list of that bucket
      on the given webinar (i.e. the bucket group shown under a webinar).
    - `webinar_id` / `webinar` — every assigned list on a webinar.

    Each contact carries the list name it originated from (set at upload time)
    and an email address. This groups the contacts in scope by list name and by
    email domain, returning counts + percentage shares so the dashboard can show
    "which lists / domains, and what % of contacts came from each", plus how many
    sit on free / personal email providers.
    """
    from sqlalchemy import select, text
    from db.models import Webinar as WebinarModel
    from db.session import AsyncSessionLocal

    if assignment is None and bucket is None and webinar_id is None and webinar is None:
        raise HTTPException(400, "Provide assignment, bucket (+ webinar), or webinar_id / webinar")

    DOMAIN_TOP_N = 10

    def _empty_domains() -> dict:
        return {
            "total": 0, "unique_domains": 0, "free_domain_contacts": 0,
            "free_domain_unique": 0, "top": [], "free": [],
        }

    async with AsyncSessionLocal() as db:
        # Resolve the webinar when given — needed for both the webinar and bucket
        # scopes. UUID takes precedence; a bare number falls back to the
        # unlabeled row, matching the /contacts endpoint.
        w = None
        if webinar_id is not None:
            w = (await db.execute(select(WebinarModel).where(WebinarModel.id == webinar_id))).scalar_one_or_none()
        elif webinar is not None:
            w = (await db.execute(
                select(WebinarModel).where(
                    WebinarModel.number == webinar,
                    WebinarModel.variant_label.is_(None),
                )
            )).scalar_one_or_none()

        if assignment is not None:
            scope = "assignment"
            from_where = (
                "FROM contacts c "
                "JOIN webinar_contact_memberships m ON m.contact_id = c.id "
                "WHERE m.assignment_id = CAST(:aid AS uuid)"
            )
            params: dict = {"aid": assignment}
            resp_webinar_id = w.id if w else None
            resp_webinar_number = w.number if w else None
        elif bucket is not None:
            scope = "bucket"
            if w is None:
                return {
                    "scope": scope, "assignment_id": None, "bucket_id": bucket,
                    "webinar_id": webinar_id, "webinar_number": webinar, "label": None,
                    "total": 0, "items": [], "domains": _empty_domains(),
                }
            resp_webinar_id = w.id
            resp_webinar_number = w.number
            from_where = (
                "FROM contacts c "
                "JOIN webinar_contact_memberships m ON m.contact_id = c.id "
                "WHERE m.webinar_id = CAST(:wid AS uuid) AND m.bucket_id = CAST(:bid AS uuid)"
            )
            params = {"wid": w.id, "bid": bucket}
        else:
            scope = "webinar"
            if w is None:
                return {
                    "scope": scope, "assignment_id": None, "bucket_id": None,
                    "webinar_id": webinar_id, "webinar_number": webinar, "label": None,
                    "total": 0, "items": [], "domains": _empty_domains(),
                }
            resp_webinar_id = w.id
            resp_webinar_number = w.number
            from_where = (
                "FROM contacts c "
                "JOIN webinar_contact_memberships m ON m.contact_id = c.id "
                "WHERE m.webinar_id = CAST(:wid AS uuid)"
            )
            params = {"wid": w.id}

        # ── List-name distribution ──────────────────────────────────────────
        list_sql = (
            f"SELECT c.lead_list_name AS list_name, COUNT(*) AS cnt {from_where} "
            "GROUP BY c.lead_list_name ORDER BY cnt DESC, list_name ASC"
        )
        rows = (await db.execute(text(list_sql).bindparams(**params))).mappings().all()
        total = sum(int(r["cnt"]) for r in rows)
        items = [
            {
                "list_name": r["list_name"],
                "count": int(r["cnt"]),
                "pct": round(100.0 * int(r["cnt"]) / total, 1) if total else 0.0,
            }
            for r in rows
        ]

        # ── Email-domain distribution ───────────────────────────────────────
        # Only contacts with a parseable address (contains '@') count toward the
        # domain breakdown; the domain is the lowercased part after the '@'.
        domain_sql = (
            "SELECT lower(split_part(c.email, '@', 2)) AS domain, COUNT(*) AS cnt "
            f"{from_where} AND c.email LIKE '%@%' "
            "GROUP BY domain ORDER BY cnt DESC, domain ASC"
        )
        drows = (await db.execute(text(domain_sql).bindparams(**params))).mappings().all()
        domain_total = sum(int(r["cnt"]) for r in drows)
        free_contacts = 0
        top: list[dict] = []
        free: list[dict] = []  # drows is sorted by count desc, so free stays sorted too
        for i, r in enumerate(drows):
            dom = r["domain"] or None
            cnt = int(r["cnt"])
            pct = round(100.0 * cnt / domain_total, 1) if domain_total else 0.0
            is_free = bool(dom and dom in FREE_EMAIL_DOMAINS)
            if is_free:
                free_contacts += cnt
                free.append({"domain": dom, "count": cnt, "pct": pct, "is_free": True})
            if i < DOMAIN_TOP_N:
                top.append({"domain": dom, "count": cnt, "pct": pct, "is_free": is_free})
        domains = {
            "total": domain_total,
            "unique_domains": len(drows),
            "free_domain_contacts": free_contacts,
            "free_domain_unique": len(free),
            "top": top,
            "free": free,
        }

        return {
            "scope": scope,
            "assignment_id": assignment,
            "bucket_id": bucket,
            "webinar_id": resp_webinar_id,
            "webinar_number": resp_webinar_number,
            "label": None,
            "total": total,
            "items": items,
            "domains": domains,
        }


async def _resolve_meta(source: str) -> dict:
    used = "workbook" if source == "workbook" else "ghl"
    last_sync = None
    if used == "ghl":
        from services.ghl_statistics_source import get_last_sync_summary
        last_sync = await get_last_sync_summary()
    return {"source": used, "last_sync": last_sync}


@router.get("/webinars", response_model=StatisticsResponse)
async def list_statistics_webinars(source: str = "auto"):
    """Return all statistics webinars with derived metrics.

    Heavy: computes metrics for every webinar. Prefer the split
    `/webinars/list` + `/webinars/{number}` flow for the dashboard.
    """
    webinars = await stats_svc.get_statistics_webinars(source=source)
    meta = await _resolve_meta(source)
    return {"webinars": webinars, "meta": meta}


@router.get("/webinars/list", response_model=StatisticsListResponse)
async def list_statistics_webinar_summaries(source: str = "auto"):
    """Lightweight identity-only list. The dashboard renders parent rows
    immediately from this and then fetches per-webinar metrics in priority
    order via `/webinars/{number}`."""
    webinars = await stats_svc.get_statistics_webinar_list(source=source)
    meta = await _resolve_meta(source)
    return {"webinars": webinars, "meta": meta}


# ---------------------------------------------------------------------------
# Overview series (Statistics Home charts)
# ---------------------------------------------------------------------------

class OverviewScopes(BaseModel):
    """The same webinar at three widening audience scopes."""
    assigned: StatisticsMetrics
    newJoiners: StatisticsMetrics
    overall: StatisticsMetrics


class OverviewWebinar(BaseModel):
    webinarId: str
    number: int | None = None
    variantLabel: str | None = None
    date: str | None = None
    title: str | None = None
    label: str | None = None
    listCount: int = 0
    scopes: OverviewScopes


class OverviewWebinarOption(BaseModel):
    """A passed webinar, offered as a filter option on the Home charts."""
    webinarId: str
    number: int | None = None
    variantLabel: str | None = None
    date: str | None = None
    title: str | None = None
    label: str | None = None


class OverviewResponse(BaseModel):
    webinars: list[OverviewWebinar]  # oldest first (chart reading order)
    allWebinars: list[OverviewWebinarOption]  # every passed webinar (filter options)
    includedWebinarIds: list[str] = []
    pendingWebinarIds: list[str] = []
    meta: StatisticsMetaResponse


@router.get("/overview", response_model=OverviewResponse)
async def get_statistics_overview(source: str = "auto", webinars: str | None = None):
    """Per-webinar metric series for the Statistics Home charts — one snapshot
    read for every passed webinar, at three audience scopes (assigned lists /
    new joiners / overall). `webinars` is a comma-separated list of Webinar
    UUIDs; omit for all passed. Reads the precomputed snapshots only, so it is
    instant once they are built (POST /statistics/recompute)."""
    ids = [x.strip() for x in webinars.split(",")] if webinars else []
    ids = [x for x in ids if x]
    data = await stats_svc.get_statistics_overview(source=source, webinar_ids=ids or None)
    meta = await _resolve_meta(source)
    return {**data, "meta": meta}


# ---------------------------------------------------------------------------
# By-bucket funnel (Segments tab)
# ---------------------------------------------------------------------------

class SegmentFunnelWebinar(BaseModel):
    """A passed webinar, offered as a filter option on the Segments tab."""
    webinarId: str
    number: int
    variantLabel: str | None = None
    date: str | None = None
    title: str | None = None
    label: str


class SegmentFunnelRow(BaseModel):
    """One bucket's rolled-up raw funnel counts across the selected webinars.
    Percentages (Reg%, Att% of inv / of reg, Book% of att, Book/1k inv) are
    derived on the frontend from these counts so they reflect summed totals,
    not averaged per-webinar rates."""
    bucketId: str | None = None  # None for the "Other (no bucket)" / Total rows
    bucketName: str | None = None
    # Manual quality label: "good" | "medium" | "bad". None on the Other/Total
    # rows and on any bucket the operator hasn't marked yet.
    quality: str | None = None
    # User-set per-segment employee-count range (literal headcount). None = undefined
    # or on the Other/Total rows. Either bound may be None for an open range.
    statEmpMin: int | None = None
    statEmpMax: int | None = None
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    # Sales + quality counts rolled up per bucket. Show% / Close% / Qual% are
    # derived on the frontend from these sums (never averaged). Default 0 so any
    # row that predates these fields still validates.
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class SegmentFunnelResponse(BaseModel):
    webinars: list[SegmentFunnelWebinar]  # all passed webinars (filter options)
    includedWebinarIds: list[str]  # the subset requested for aggregation
    # Selected webinars with no snapshot yet — excluded from the totals until a
    # recompute builds them. Surfaced so the UI can prompt "Recompute now".
    pendingWebinarIds: list[str] = []
    segments: list[SegmentFunnelRow]  # named buckets (invites desc) + Other
    totals: SegmentFunnelRow
    meta: StatisticsMetaResponse


@router.get("/segments", response_model=SegmentFunnelResponse)
async def get_statistics_segments(source: str = "auto", webinars: str | None = None):
    """By-bucket funnel overview. `webinars` is a comma-separated list of
    Webinar UUIDs to include; omit (or pass empty) to include all passed
    webinars. Heavy on a cold cache — shares the per-webinar response cache
    with the Statistics page, so it's instant once that cache is warm."""
    ids = [x.strip() for x in webinars.split(",")] if webinars else []
    ids = [x for x in ids if x]
    data = await stats_svc.get_statistics_segments(source=source, webinar_ids=ids or None)
    meta = await _resolve_meta(source)
    return {**data, "meta": meta}


class SegmentEmployeeBand(BaseModel):
    """One canonical company-size band's funnel counts for a single segment,
    summed across the selected webinars. Raw counts — the frontend derives the
    percentages and the suggested range from these sums."""
    sizeLabel: str
    minEmp: int | None = None  # literal headcount lower bound (None only for "(no size)")
    maxEmp: int | None = None  # upper bound; None = open ("10000+") or "(no size)"
    invites: int = 0
    regs: int = 0
    attendees10m: int = 0
    bookings: int = 0
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class SegmentEmployeeResponse(BaseModel):
    bucketId: str
    bucketName: str | None = None
    statEmpMin: int | None = None
    statEmpMax: int | None = None
    includedWebinarIds: list[str] = []
    # Selected webinars whose snapshot predates the segment x company-size cross
    # — excluded from the bands until a recompute builds them (mirrors /segments).
    pendingWebinarIds: list[str] = []
    bands: list[SegmentEmployeeBand]


@router.get("/segments/{bucket_id}/by-employee", response_model=SegmentEmployeeResponse)
async def get_statistics_segment_by_employee(
    bucket_id: str, source: str = "auto", webinars: str | None = None
):
    """Per-segment × company-size funnel for one bucket across the selected
    webinars. Powers the Segments-tab drill-down and the data-driven suggested
    employee range (conversion: booked calls + lead-quality tiers + won).
    `webinars` is a comma-separated list of Webinar UUIDs; omit for all passed.
    Reads the precomputed snapshots only — instant once they're built (POST
    /statistics/recompute); webinars without the cross come back as pending."""
    ids = [x.strip() for x in webinars.split(",")] if webinars else []
    ids = [x for x in ids if x]
    data = await stats_svc.get_statistics_segment_employee(
        bucket_id=bucket_id, source=source, webinar_ids=ids or None
    )
    if data is None:
        raise HTTPException(404, "Segment (bucket) not found")
    return data


# ---------------------------------------------------------------------------
# By-source funnel (By List Source tab)
# ---------------------------------------------------------------------------

class SourceVintageRow(BaseModel):
    """One list-vintage (e.g. "2025-11") within a data source. Raw counts —
    the frontend derives the funnel percentages from these sums."""
    vintage: str
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class SourceFunnelRow(BaseModel):
    """One data source (AmpleLeads / FindyLeads / ZoomInfo / …) with its
    per-vintage breakdown."""
    source: str
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    # Sales + quality counts (mirrors SegmentFunnelRow). Rates are derived on the
    # frontend from these sums. Default 0 so pre-feature snapshots still validate.
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0
    vintages: list[SourceVintageRow] = []


class SourceFunnelWebinarBreakdown(BaseModel):
    webinarId: str
    number: int | None = None
    variantLabel: str | None = None
    date: str | None = None
    label: str | None = None
    bySource: list[SourceFunnelRow]


class SourceFunnelTotals(BaseModel):
    source: str = "Total"
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class SourceFunnelResponse(BaseModel):
    webinars: list[SegmentFunnelWebinar]  # all passed webinars (filter options)
    includedWebinarIds: list[str]
    pendingWebinarIds: list[str] = []
    bySource: list[SourceFunnelRow]  # cross-webinar aggregate (invites desc)
    perWebinar: list[SourceFunnelWebinarBreakdown]  # newest first
    totals: SourceFunnelTotals
    meta: StatisticsMetaResponse


@router.get("/by-list-source", response_model=SourceFunnelResponse)
async def get_statistics_by_list_source(source: str = "auto", webinars: str | None = None):
    """By-data-source funnel (AmpleLeads / FindyLeads / ZoomInfo / …) with a
    per-vintage drill-down and a per-webinar breakdown. `webinars` is a
    comma-separated list of Webinar UUIDs to include; omit (or pass empty) to
    include all passed webinars. Reads the precomputed snapshots only — instant
    once they're built (POST /statistics/recompute)."""
    ids = [x.strip() for x in webinars.split(",")] if webinars else []
    ids = [x for x in ids if x]
    data = await stats_svc.get_statistics_by_source(source=source, webinar_ids=ids or None)
    meta = await _resolve_meta(source)
    return {**data, "meta": meta}


# ---------------------------------------------------------------------------
# By-employee-size funnel (Employee count tab)
# ---------------------------------------------------------------------------

class EmployeeFunnelRow(BaseModel):
    """One company-size bucket ("1 - 10" / "11 - 50" / … / "(no size)"). Raw
    counts — the frontend derives the funnel percentages from these sums."""
    bucket: str
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class EmployeeFunnelWebinarBreakdown(BaseModel):
    webinarId: str
    number: int | None = None
    variantLabel: str | None = None
    date: str | None = None
    label: str | None = None
    byEmployee: list[EmployeeFunnelRow]


class EmployeeFunnelTotals(BaseModel):
    bucket: str = "Total"
    invites: int
    regs: int
    attendees10m: int
    bookings: int
    confirmed: int = 0
    shows: int = 0
    noShows: int = 0
    canceled: int = 0
    won: int = 0
    disqualified: int = 0
    qualified: int = 0
    leadQualityGreat: int = 0
    leadQualityOk: int = 0
    leadQualityBarelyPassable: int = 0
    leadQualityBadDq: int = 0


class EmployeeFunnelResponse(BaseModel):
    webinars: list[SegmentFunnelWebinar]  # all passed webinars (filter options)
    includedWebinarIds: list[str]
    pendingWebinarIds: list[str] = []
    byEmployee: list[EmployeeFunnelRow]  # cross-webinar aggregate (invites desc)
    perWebinar: list[EmployeeFunnelWebinarBreakdown]  # newest first
    totals: EmployeeFunnelTotals
    meta: StatisticsMetaResponse


@router.get("/by-employee-count", response_model=EmployeeFunnelResponse)
async def get_statistics_by_employee_count(source: str = "auto", webinars: str | None = None):
    """By-company-size funnel (grouped by contacts.employee_range buckets) with a
    per-webinar breakdown. `webinars` is a comma-separated list of Webinar UUIDs
    to include; omit (or pass empty) to include all passed webinars. Reads the
    precomputed snapshots only — instant once they're built
    (POST /statistics/recompute)."""
    ids = [x.strip() for x in webinars.split(",")] if webinars else []
    ids = [x for x in ids if x]
    data = await stats_svc.get_statistics_by_employee(source=source, webinar_ids=ids or None)
    meta = await _resolve_meta(source)
    return {**data, "meta": meta}


# ---------------------------------------------------------------------------
# Precomputed snapshot store — manual recompute trigger + status
# ---------------------------------------------------------------------------

class RecomputeStatusResponse(BaseModel):
    running: bool
    scope: str | None = None  # "all" | "partial"
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    done: int = 0
    errors: int = 0
    last_error: str | None = None
    last_computed_at: str | None = None  # MAX(computed_at) across snapshots
    snapshot_count: int = 0


@router.post("/recompute", response_model=RecomputeStatusResponse)
async def trigger_recompute(source: str = "auto"):
    """Force a full background rebuild of every webinar's statistics snapshot.
    Returns immediately with current status; poll /recompute/status to watch
    progress. Coalesced — a second call while one is running is a no-op."""
    from services import statistics_snapshot as snap
    snap.schedule_recompute(source=source)
    return await snap.get_status(source)


@router.get("/recompute/status", response_model=RecomputeStatusResponse)
async def recompute_status(source: str = "auto"):
    """Current recompute progress + when the snapshots were last rebuilt."""
    from services import statistics_snapshot as snap
    return await snap.get_status(source)


# ---------------------------------------------------------------------------
# Per-webinar report (frozen artifact + AI insights) — see services.webinar_report
# ---------------------------------------------------------------------------

class WebinarReportStatusResponse(BaseModel):
    running: bool
    queued: bool = False                # durable request marker (sweep retries)
    phase: str | None = None            # "queries" | "ai"
    started_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None
    attempts: int = 0
    generated_at: str | None = None     # when a stored report exists
    typical_ms: int | None = None       # avg generation time → frontend ETA


class WebinarReportResponse(BaseModel):
    webinarId: str
    number: int | None = None
    variantLabel: str | None = None
    payload: dict | None = None
    insights: list | None = None
    insightsModel: str | None = None
    aiError: str | None = None
    generatedAt: str | None = None
    generationMs: int | None = None
    status: WebinarReportStatusResponse


@router.get("/report/{webinar_id}", response_model=WebinarReportResponse)
async def get_webinar_report(webinar_id: str, generate_if_missing: bool = True):
    """Stored report for a webinar. When none exists and generate_if_missing,
    generation is scheduled in the background (2–4 min); poll
    /report/{id}/status and re-fetch when it finishes."""
    from services import webinar_report as rpt

    report = await rpt.read_report(webinar_id)
    if report is None and generate_if_missing:
        await rpt.request_generate(webinar_id)
    status = await rpt.get_status(webinar_id)
    if report is None:
        return WebinarReportResponse(webinarId=webinar_id, status=status)
    return WebinarReportResponse(**report, status=status)


@router.post("/report/{webinar_id}/generate", response_model=WebinarReportStatusResponse)
async def trigger_webinar_report(webinar_id: str):
    """Manual (re)generation. Durably queued (survives restarts) + started
    immediately. Returns at once; poll /report/{id}/status."""
    from services import webinar_report as rpt
    await rpt.request_generate(webinar_id)
    return await rpt.get_status(webinar_id)


@router.get("/report/{webinar_id}/status", response_model=WebinarReportStatusResponse)
async def webinar_report_status(webinar_id: str):
    from services import webinar_report as rpt
    return await rpt.get_status(webinar_id)


@router.get("/webinars/{webinar_id}", response_model=ApiStatisticsWebinar)
async def get_statistics_webinar(webinar_id: str, source: str = "auto"):
    """Fully-processed single webinar by webinar_id (the row's UUID, or
    the synthetic `stat-wNNN` id from the workbook source).

    Path used to take a number; switched to webinar_id so A/B variants
    sharing a number can be addressed separately.
    """
    webinar = await stats_svc.get_statistics_webinar_one(source=source, webinar_id=webinar_id)
    if webinar is None:
        raise HTTPException(status_code=404, detail=f"Webinar {webinar_id} not found")
    return webinar


# ---------------------------------------------------------------------------
# Booking calendars — curated calendar → source mapping
# ---------------------------------------------------------------------------

class BookingCalendarItem(BaseModel):
    calendar_id: str
    name: str | None = None
    calendar_class: str | None = None  # first | followup | exclude
    funnel_tag: str | None = None      # webinar | outreach | referral
    source_label: str | None = None    # curated (editable)
    booking_count: int = 0             # distinct opps whose 1st call is on this calendar


class BookingCalendarsResponse(BaseModel):
    calendars: list[BookingCalendarItem]


class BookingCalendarUpdate(BaseModel):
    source_label: str | None = None
    calendar_class: str | None = None  # first | followup | exclude


class SourceRenameRequest(BaseModel):
    from_label: str
    to_label: str


class SourceRenameResponse(BaseModel):
    updated: int


@router.get("/booking-calendars", response_model=BookingCalendarsResponse)
async def list_booking_calendars():
    """Every calendar we book appointments on, with its auto-classification,
    curated source label, and how many 1st calls it sourced. Powers the
    Calendars tab where the source mapping is curated."""
    from sqlalchemy import text
    from db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT c.calendar_id, c.name, c.calendar_class, c.funnel_tag, c.source_label,
                   COUNT(o.ghl_opportunity_id) AS booking_count
            FROM ghl_calendar c
            LEFT JOIN ghl_opportunity o ON o.call1_calendar_id = c.calendar_id
            GROUP BY c.calendar_id, c.name, c.calendar_class, c.funnel_tag, c.source_label
            ORDER BY booking_count DESC, c.name
        """))).mappings().all()
    return {"calendars": [dict(r) for r in rows]}


@router.patch("/booking-calendars/{calendar_id}", response_model=BookingCalendarItem)
async def update_booking_calendar(calendar_id: str, payload: BookingCalendarUpdate):
    """Update a calendar's curated source label and/or its class override.

    A calendar_class edit marks the row manual (so syncs stop re-classifying
    it), retags the calendar's stored appointments, and re-derives call1/call2
    for every affected opportunity — first-call attribution follows the new
    class. Invalidates the stats cache either way."""
    from sqlalchemy import text
    from db.session import AsyncSessionLocal
    from services.ghl_sync import apply_manual_calendar_class

    fields = payload.model_fields_set
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    new_class: str | None = None
    if "calendar_class" in fields:
        new_class = (payload.calendar_class or "").strip().lower()
        if new_class not in ("first", "followup", "exclude"):
            raise HTTPException(
                status_code=400,
                detail="calendar_class must be one of: first, followup, exclude",
            )

    sets, params = [], {"cid": calendar_id}
    if "source_label" in fields:
        sets.append("source_label = :label")
        params["label"] = (payload.source_label or "").strip() or None
    if new_class is not None:
        sets.append("calendar_class = :cls")
        sets.append("class_is_manual = TRUE")
        params["cls"] = new_class

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text(f"UPDATE ghl_calendar SET {', '.join(sets)} WHERE calendar_id = :cid"),
            params,
        )
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Calendar {calendar_id} not found")
        await db.commit()

    if new_class is not None:
        await apply_manual_calendar_class(calendar_id, new_class)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("""
            SELECT c.calendar_id, c.name, c.calendar_class, c.funnel_tag, c.source_label,
                   COUNT(o.ghl_opportunity_id) AS booking_count
            FROM ghl_calendar c
            LEFT JOIN ghl_opportunity o ON o.call1_calendar_id = c.calendar_id
            WHERE c.calendar_id = :cid
            GROUP BY c.calendar_id, c.name, c.calendar_class, c.funnel_tag, c.source_label
        """), {"cid": calendar_id})).mappings().one()

    stats_svc.invalidate_stats_cache()
    return dict(row)


@router.post("/booking-calendars/rename-source", response_model=SourceRenameResponse)
async def rename_booking_source(payload: SourceRenameRequest):
    """Rename a source label across every calendar that carries it, so the
    Bookings drill-down regroups under the new name in one step."""
    from sqlalchemy import text
    from db.session import AsyncSessionLocal

    from_label = payload.from_label.strip()
    to_label = payload.to_label.strip()
    if not from_label or not to_label:
        raise HTTPException(status_code=400, detail="Both labels are required")
    if from_label == to_label:
        return {"updated": 0}

    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text("UPDATE ghl_calendar SET source_label = :to WHERE source_label = :frm"),
            {"to": to_label, "frm": from_label},
        )
        await db.commit()

    stats_svc.invalidate_stats_cache()
    return {"updated": res.rowcount}
