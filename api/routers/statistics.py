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
    yesAttendBySmsClick: float | None = None
    yesBookings: float | None = None
    maybeMarked: float | None = None
    maybeAttended: float | None = None
    maybe10MinPlus: float | None = None
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
    yesAttendBySmsClickPercent: float | None = None
    yesBookingsPer1kInv: float | None = None
    maybePer1kInv: float | None = None
    maybeAttendPercent: float | None = None
    maybeStay10MinPercent: float | None = None
    maybeAttendBySmsClickPercent: float | None = None
    maybeBookingsPer1kInv: float | None = None
    selfRegPer1kInv: float | None = None
    selfRegAttendPercent: float | None = None
    selfRegStay10MinPercent: float | None = None
    selfRegBookingsPer1kInv: float | None = None
    invitedToRegPercent: float | None = None
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
    ghl_contact_id: str
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
    webinar_source_number: int | None = None
    lead_quality: str | None = None


class ContactDrilldownResponse(BaseModel):
    metric: str
    webinar_number: int
    webinar_id: str | None = None
    assignment_id: str | None = None
    unit: str  # "contact" | "opportunity"
    total: int
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
        if spec.unit == "opportunity" and assignment is None:
            list_sql, count_sql, params = build_webinar_wide_opp_query(spec, w.number, limit=limit)
            count_params = {k: v for k, v in params.items() if k != "limit"}
            total = int((await db.execute(text(count_sql).bindparams(**count_params))).scalar() or 0)
            r = await db.execute(text(list_sql).bindparams(**params))
            rows = r.mappings().all()
        else:
            sql, params = build_contacts_query(spec, w.id, assignment_id=assignment, limit=limit)
            r = await db.execute(text(sql).bindparams(**params))
            rows = r.mappings().all()
            total = len(rows)

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
            from_where = "FROM contacts c WHERE c.assignment_id = CAST(:aid AS uuid)"
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
                "JOIN webinar_list_assignments wla ON c.assignment_id = wla.id "
                "WHERE wla.webinar_id = CAST(:wid AS uuid) AND wla.bucket_id = CAST(:bid AS uuid)"
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
                "JOIN webinar_list_assignments wla ON c.assignment_id = wla.id "
                "WHERE wla.webinar_id = CAST(:wid AS uuid)"
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
