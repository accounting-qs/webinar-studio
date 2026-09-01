"""Outreach sub-router: Custom Fields + the Contacts directory (search/detail)."""
import uuid as uuid_mod

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text as sa_text, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from api.schemas import CustomFieldCreate
from db.models import (
    BlocklistEntry,
    BucketCopy,
    CalendarAccountSender,
    Contact,
    ContactCustomField,
    ContactReleaseLog,
    GHLContact,
    OutreachBucket,
    OutreachSender,
    UploadHistory,
    Webinar,
    WebinarBookingAttribution,
    WebinarCalendarInvite,
    WebinarContactMembership,
    WebinarGeekSubscriber,
    WebinarListAssignment,
    WebinarNonjoinerInvite,
)
from db.session import get_db

router = APIRouter()

# The search expression must match ix_contacts_search_trgm structurally, or the
# planner will not use it and every search becomes a full heap scan of contacts
# (5.6M rows, 6.9 GB). Migration 076 widens the index to include `title`; until
# it has been built the API keeps using the narrower migration-075 expression,
# so a half-deployed environment degrades to "title isn't searchable yet"
# instead of "every search scans the table". _search_fields() resolves which
# one is live, once, from the catalog.
SEARCH_FIELDS_075 = (
    "email", "first_name", "last_name", "company_website", "bucket_name", "lead_list_name",
)
SEARCH_FIELDS_076 = SEARCH_FIELDS_075 + ("title",)

_search_fields_cache: tuple[str, ...] | None = None


def _search_expr(fields: tuple[str, ...], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return " || ' ' || ".join(f"coalesce({prefix}{f}, '')" for f in fields)


async def _search_fields(db: AsyncSession) -> tuple[str, ...]:
    """Which identity fields the live trigram index actually covers."""
    global _search_fields_cache
    if _search_fields_cache is None:
        indexdef = (await db.execute(sa_text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'contacts' AND indexname = 'ix_contacts_search_trgm'"
        ))).scalar()
        _search_fields_cache = (
            SEARCH_FIELDS_076
            if indexdef and "coalesce(title" in indexdef.lower()
            else SEARCH_FIELDS_075
        )
    return _search_fields_cache

# Stop collecting matches past this many rows: bounds the sort, the count and —
# critically — how far a broad term's bitmap/seq scan runs. filtered_total ==
# the cap means "more than cap-1 matches", surfaced as total_kind='capped'.
SEARCH_MATCH_CAP = 10_001

MAX_PAGE_SIZE = 200

# Every driver other than `search`, `blocklist` and `browse` leads with a key
# from another table and then has to visit `contacts` once per row — a random
# fetch into a 6.9 GB heap, ~0.5-0.8 ms each. When a refinement can REJECT rows,
# the number of fetches is (rows shown / selectivity), which for a rare value is
# the whole cohort. So those scans take a fixed budget of driver rows per
# request instead, and report `scan_capped` when they hit it. Without
# refinements every scanned row is kept, so no budget is needed.
SCAN_BUDGET = 3_000

# Free-text response strings as they arrive from the "Added to Calendar" CSVs.
RESPONSE_VALUES = {"yes", "maybe", "no", "awaiting", "deleted", "spam"}

# Engagement drivers, all backed by a small table (WebinarGeek subscribers ~29k
# distinct emails, booking attribution ~2k contacts), so each can lead a query.
ENGAGEMENT_VALUES = {"registered", "attended", "live", "replay", "booked", "won"}

STATUS_VALUES = {"available", "assigned", "used"}

# Columns every listing mode returns, in one place so the CTEs stay in sync.
LIST_COLUMNS = (
    "id, email, first_name, last_name, title, seniority, company_website, "
    "industry, bucket_name, lead_list_name, country, list_location, "
    "employee_range, employee_count, outreach_status, is_blocklisted, "
    "times_invited, last_invited_at, created_at"
)
LIST_COLUMNS_PREFIXED = ", ".join(f"c.{col.strip()}" for col in LIST_COLUMNS.split(","))


def _iso(v):
    return v.isoformat() if v is not None else None


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _valid_uuid(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        uuid_mod.UUID(value)
    except ValueError:
        raise HTTPException(422, f"Invalid {label}")
    return value


def _response_variants(response: str) -> list[str]:
    """The CSVs write responses in Title case ("Yes"). Compare against the raw
    column rather than lower(...) so ix_wci_webinar_response stays usable."""
    return sorted({response, response.title(), response.upper()})


class _Refinements:
    """Contact-column predicates + membership/invite EXISTS probes that narrow
    whatever driver leads the query. Never a driver themselves: none of them is
    selective enough on its own to keep a 5.6M-row scan bounded."""

    def __init__(self, *, alias: str, params: dict, search_fields: tuple[str, ...]):
        self.alias = alias
        self.params = params
        self.search_fields = search_fields
        self.clauses: list[str] = []

    def _col(self, name: str) -> str:
        return f"{self.alias}.{name}" if self.alias else name

    def equals(self, column: str, value, key: str) -> None:
        if value is None or value == "":
            return
        self.clauses.append(f"{self._col(column)} = :{key}")
        self.params[key] = value

    def ilike(self, column: str, value: str | None, key: str) -> None:
        if not value:
            return
        self.clauses.append(f"{self._col(column)} ILIKE :{key}")
        self.params[key] = _like_pattern(value.strip())

    def boolean(self, column: str, value: bool | None, key: str) -> None:
        if value is None:
            return
        self.clauses.append(f"{self._col(column)} = :{key}")
        self.params[key] = value

    def search_terms(self, terms: list[str]) -> None:
        expr = _search_expr(self.search_fields, self.alias)
        for i, term in enumerate(terms):
            self.clauses.append(f"({expr}) ILIKE :term_{i}")
            self.params[f"term_{i}"] = _like_pattern(term)

    def in_campaign(self, webinar_id: str | None, response: str | None) -> None:
        """Contact is on a list for this webinar.

        Membership only: uq_wcm_webinar_contact answers it index-only in ~0.2 ms.
        OR-ing in the calendar invite (to also catch contacts RELEASED after the
        invite) costs ~1.5 ms per candidate, because ix_wci_matched_contact_id
        is keyed on contact_id alone and every miss has to fetch the heap tuple
        to read webinar_id — and a miss is exactly the common case here. Released
        contacts are still reachable through the Response filter, which leads
        with the invite table instead."""
        if not webinar_id:
            return
        self.params["ref_webinar_id"] = webinar_id
        cid = self._col("id")
        if response:
            self.params["ref_response"] = _response_variants(response)
            self.clauses.append(
                "EXISTS (SELECT 1 FROM webinar_calendar_invites i "
                f"WHERE i.matched_contact_id = {cid} AND i.webinar_id = :ref_webinar_id "
                "AND i.calendar_invite_response = ANY(CAST(:ref_response AS text[])))"
            )
            return
        self.clauses.append(
            "EXISTS (SELECT 1 FROM webinar_contact_memberships m "
            f"WHERE m.contact_id = {cid} AND m.webinar_id = :ref_webinar_id)"
        )

    def engaged(self, engagement: str | None) -> None:
        """Attendance / booking as a refinement rather than a driver. Both
        probes are per-contact index hits on small tables (ix_wba_app_contact,
        ix_wg_subs_email), so they are safe on top of any driver."""
        if not engagement:
            return
        cid = self._col("id")
        email = self._col("email")
        if engagement in ("booked", "won"):
            won = " AND b.won" if engagement == "won" else ""
            self.clauses.append(
                "EXISTS (SELECT 1 FROM webinar_booking_attribution b "
                f"WHERE b.contact_id = {cid}{won})"
            )
            return
        watched = {
            "attended": " AND (s.watched_live OR s.watched_replay)",
            "live": " AND s.watched_live",
            "replay": " AND s.watched_replay",
            "registered": "",
        }[engagement]
        # Compare against the raw column (both casings) so ix_wg_subs_email is
        # usable; lower(s.email) would force a scan of every subscriber row.
        self.clauses.append(
            "EXISTS (SELECT 1 FROM webinargeek_subscribers s "
            f"WHERE (s.email = {email} OR s.email = lower({email})){watched})"
        )

    def responded(self, response: str | None, webinar_id: str | None) -> None:
        """Response filter with no campaign chosen: any webinar counts."""
        if not response or webinar_id:
            return
        self.params["ref_response"] = _response_variants(response)
        self.clauses.append(
            "EXISTS (SELECT 1 FROM webinar_calendar_invites i "
            f"WHERE i.matched_contact_id = {self._col('id')} "
            "AND i.calendar_invite_response = ANY(CAST(:ref_response AS text[])))"
        )

    def sql(self) -> str:
        return " AND ".join(self.clauses) if self.clauses else ""


def _build_refinements(
    *,
    alias: str,
    params: dict,
    terms: list[str],
    search_fields: tuple[str, ...],
    status: str | None,
    blocklisted: bool | None,
    bucket_id: str | None,
    country: str | None,
    industry: str | None,
    seniority: str | None,
    employee_range: str | None,
    campaign_id: str | None,
    response: str | None,
    engagement: str | None = None,
) -> str:
    ref = _Refinements(alias=alias, params=params, search_fields=search_fields)
    ref.search_terms(terms)
    ref.equals("outreach_status", status, "ref_status")
    ref.boolean("is_blocklisted", blocklisted, "ref_blocklisted")
    ref.equals("bucket_id", bucket_id, "ref_bucket_id")
    ref.ilike("country", country, "ref_country")
    ref.ilike("industry", industry, "ref_industry")
    ref.ilike("seniority", seniority, "ref_seniority")
    ref.ilike("employee_range", employee_range, "ref_employee_range")
    ref.in_campaign(campaign_id, response)
    ref.responded(response, campaign_id)
    ref.engaged(engagement)
    return ref.sql()


@router.get("/contacts")
async def list_contacts(
    search: str = Query("", max_length=200),
    webinar_id: str | None = Query(None, description="Campaign (webinar) the contact took part in"),
    status: str | None = Query(None, description="available | assigned | used"),
    response: str | None = Query(None, description="yes | maybe | no | awaiting | deleted | spam"),
    engagement: str | None = Query(None, description="registered | attended | live | replay | booked | won"),
    blocklisted: bool | None = Query(None),
    bucket_id: str | None = Query(None),
    country: str | None = Query(None, max_length=100),
    industry: str | None = Query(None, max_length=100),
    seniority: str | None = Query(None, max_length=100),
    employee_range: str | None = Query(None, max_length=50),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(None, max_length=500),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Contacts directory: browse everything, substring-search across the
    identity fields, and/or narrow by campaign, status, response, engagement
    and firmographics.

    Everything here is shaped by the table's scale (5.6M contacts, 6.9 GB heap):
    a single unindexed predicate turns a page view into a multi-second
    sequential scan that evicts the working set from shared_buffers. So every
    query is led by exactly one DRIVER — a predicate an index can satisfy — and
    all other filters are refinements applied on top of it:

      search      trgm GIN on the identity expression, bounded by SEARCH_MATCH_CAP
      engagement  WebinarGeek subscribers / booking attribution (small tables)
      response    ix_wci_webinar_response for the chosen webinar
      campaign    keyset walk of uq_wcm_webinar_contact for the chosen webinar
      bucket      ix_contacts_bucket_filters
      blocklisted the partial is_blocklisted indexes (~12k rows)
      browse      keyset on uq_contacts_user_email, nothing filtered

    Refinement-only requests are rejected (422) rather than silently served as a
    table scan; the UI asks for a search term or a campaign instead.

    Pagination follows the driver: keyset (`cursor`) for browse and campaign,
    offset over a bounded match set everywhere else. `mode` in the response
    tells the client which one it got.
    """
    terms = [t for t in search.strip().split() if t][:5]
    if terms and not any(len(t) >= 3 for t in terms):
        # A pattern under 3 chars yields no trigram, degrading the GIN lookup to
        # a full-index recheck (i.e. a table scan). Require one indexable term;
        # shorter extra terms only filter the bounded candidates.
        raise HTTPException(422, "Search needs at least one term of 3+ characters")

    webinar_id = _valid_uuid(webinar_id, "webinar_id")
    bucket_id = _valid_uuid(bucket_id, "bucket_id")

    if status and status not in STATUS_VALUES:
        raise HTTPException(422, f"status must be one of {sorted(STATUS_VALUES)}")
    if response:
        response = response.strip().lower()
        if response not in RESPONSE_VALUES:
            raise HTTPException(422, f"response must be one of {sorted(RESPONSE_VALUES)}")
    if engagement:
        engagement = engagement.strip().lower()
        if engagement not in ENGAGEMENT_VALUES:
            raise HTTPException(422, f"engagement must be one of {sorted(ENGAGEMENT_VALUES)}")

    has_refinement = any([
        status, blocklisted is not None, country, industry, seniority, employee_range,
    ])
    has_driver = bool(terms or engagement or webinar_id or bucket_id or response) or blocklisted is True

    if not has_driver and has_refinement:
        raise HTTPException(
            422,
            "These filters need something to narrow first — add a search term, "
            "a campaign, or a bucket.",
        )
    if response and not webinar_id and not (terms or engagement or bucket_id):
        # A bare response filter has no index across all webinars.
        raise HTTPException(
            422, "Pick a campaign (or add a search term) to filter by invite response."
        )

    fields = await _search_fields(db)

    if terms:
        return await _list_by_search(
            db, terms=terms, search_fields=fields, status=status, response=response, engagement=engagement,
            blocklisted=blocklisted, bucket_id=bucket_id, campaign_id=webinar_id,
            country=country, industry=industry, seniority=seniority,
            employee_range=employee_range, limit=limit, offset=offset,
        )
    if engagement:
        return await _list_by_engagement(
            db, engagement=engagement, search_fields=fields, status=status, response=response,
            blocklisted=blocklisted, bucket_id=bucket_id, campaign_id=webinar_id,
            country=country, industry=industry, seniority=seniority,
            employee_range=employee_range, limit=limit, offset=offset,
        )
    if webinar_id and response:
        return await _list_by_response(
            db, webinar_id=webinar_id, response=response, search_fields=fields, status=status,
            blocklisted=blocklisted, bucket_id=bucket_id, country=country,
            industry=industry, seniority=seniority, employee_range=employee_range,
            limit=limit, offset=offset,
        )
    if webinar_id:
        return await _list_by_campaign(
            db, webinar_id=webinar_id, search_fields=fields, status=status, blocklisted=blocklisted,
            bucket_id=bucket_id, country=country, industry=industry,
            seniority=seniority, employee_range=employee_range,
            limit=limit, cursor=cursor,
        )
    if bucket_id:
        return await _list_by_bucket(
            db, bucket_id=bucket_id, search_fields=fields, status=status,
            blocklisted=blocklisted, response=response, country=country,
            industry=industry, seniority=seniority, employee_range=employee_range,
            limit=limit, offset=offset,
        )
    if blocklisted is True:
        return await _list_by_blocklist(
            db, search_fields=fields, status=status, response=response,
            country=country, industry=industry, seniority=seniority,
            employee_range=employee_range, limit=limit, offset=offset,
        )
    return await _list_browse(db, limit=limit, cursor=cursor)


# ── Listing modes ────────────────────────────────────────────────────────────

async def _list_browse(db: AsyncSession, *, limit: int, cursor: str | None) -> dict:
    """Unfiltered: keyset on (user_id, email) — cheap at any depth."""
    q = select(*_summary_columns()).where(Contact.user_id == LLOYD_USER_ID)
    if cursor:
        q = q.where(Contact.email > cursor)
    q = q.order_by(Contact.email).limit(limit + 1)
    rows = (await db.execute(q)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].email if has_more and rows else None

    # Planner estimate instead of count(*): an exact count is a multi-second
    # index scan over millions of entries, per page view. reltuples is kept
    # fresh by the aggressive autovacuum tuning of migration 074.
    est = (await db.execute(
        sa_text("SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE oid = 'contacts'::regclass")
    )).scalar()

    return {
        "mode": "browse",
        "contacts": [_summary_dict(r) for r in rows],
        "total": int(est or 0),
        "total_kind": "estimated",
        "next_cursor": next_cursor,
        "scan_capped": False,
    }


async def _scanned_page(
    db: AsyncSession, *, mode: str, scan_sql: str, refine_sql: str,
    params: dict, limit: int, offset: int,
    count_sql: str | None = None, scan_cap: int | None = None,
) -> dict:
    """Offset page over a bounded scan. Every mode except `browse` and
    `campaign` funnels through here.

    The cost that has to be bounded is always the same one: reading candidate
    rows out of the 6.9 GB contacts heap, ~0.5-0.8 ms apiece. A LIMIT on the
    OUTPUT does not bound that — a refinement that rejects 98% of rows makes the
    scan 50x longer than the page it produces — so the limit goes on the INPUT:

      scan_cap set (search, blocklist)
        the driver's own index does the selecting, so read up to scan_cap
        candidates and refine them. `total` is exact below the cap.
      no refinements
        every scanned row is shown, so scan exactly one page of them and take
        the cohort size from `count_sql`, which never touches contacts.
      refinements (bucket, response, engagement)
        read SCAN_BUDGET candidates and filter those. The page is honest but
        partial: `scan_capped` says the cohort was not read to the end.

    MATERIALIZED on both CTEs is load-bearing: without it the planner inlines
    the scan into an ordered index walk over (user_id, email), which under LIMIT
    turns a selective query into an unbounded random-order heap crawl. The scan
    also carries the display columns so ordering never re-visits the heap.
    """
    refined = bool(refine_sql)
    if scan_cap is not None:
        scan_limit = scan_cap
    elif refined:
        scan_limit = SCAN_BUDGET
    else:
        scan_limit = offset + limit + 1

    params = {**params, "scan_limit": scan_limit, "limit": limit, "offset": offset}
    where = f"WHERE {refine_sql}" if refined else ""
    sql = sa_text(f"""
        WITH scan AS MATERIALIZED (
            {scan_sql}
            LIMIT :scan_limit
        ),
        matches AS MATERIALIZED (
            SELECT s.* FROM scan s
            {where}
        )
        SELECT (SELECT count(*) FROM scan) AS scanned,
               (SELECT count(*) FROM matches) AS filtered_total,
               m.*
        FROM matches m
        ORDER BY m.email
        LIMIT :limit OFFSET :offset
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    if rows:
        scanned, filtered_total = rows[0]["scanned"], rows[0]["filtered_total"]
    else:
        # Empty page (no matches, or paged past the end): the counts still have
        # to come back, so re-run just the aggregates.
        meta = (await db.execute(sa_text(f"""
            WITH scan AS MATERIALIZED ({scan_sql} LIMIT :scan_limit)
            SELECT count(*) AS scanned,
                   count(*) FILTER (WHERE {refine_sql or 'true'}) AS filtered_total
            FROM scan s
        """), params)).mappings().first()
        scanned, filtered_total = (meta["scanned"], meta["filtered_total"]) if meta else (0, 0)

    scan_capped = scanned >= scan_limit and (refined or scan_cap is not None)
    if count_sql and not refined and scan_cap is None:
        total = (await db.execute(sa_text(count_sql), params)).scalar() or 0
        total_kind = "exact"
    else:
        total = filtered_total
        total_kind = "capped" if scan_capped else "exact"

    return {
        "mode": mode,
        "contacts": [_summary_dict(r) for r in rows],
        "total": int(total),
        "total_kind": total_kind,
        "next_cursor": None,
        "scan_capped": scan_capped,
    }


async def _list_by_search(db: AsyncSession, *, terms, search_fields, limit, offset, **f) -> dict:
    """Driver: the trigram GIN index over the identity expression.

    Only the typed terms lead; every other filter refines the candidates the
    index produced. Keeping them out of the scan matters — fused into the same
    WHERE, a broad term plus a rare attribute walks the term's entire posting
    list (hundreds of thousands of heap fetches) looking for cap matches.
    """
    params: dict = {"user_id": LLOYD_USER_ID}
    scan_ref = _Refinements(alias="", params=params, search_fields=search_fields)
    scan_ref.search_terms(terms)
    refine = _build_refinements(alias="s", params=params, terms=[], search_fields=search_fields, **f)
    scan_sql = f"""
        SELECT {LIST_COLUMNS}
        FROM contacts
        WHERE user_id = :user_id AND {scan_ref.sql()}
    """
    # Unrefined, the cap only buys heap reads the page needs anyway. Refined,
    # every scanned candidate also pays a probe (campaign, response, engagement)
    # or a column test, so the scan shrinks to the budget: a specific term still
    # resolves exactly, a broad one returns bounded, flagged-partial results.
    return await _scanned_page(
        db, mode="search", scan_sql=scan_sql, refine_sql=refine, params=params,
        limit=limit, offset=offset,
        scan_cap=SCAN_BUDGET if refine else SEARCH_MATCH_CAP,
    )


async def _list_by_engagement(db: AsyncSession, *, engagement, search_fields, limit, offset, **f) -> dict:
    """Driver: the small attendance / booking tables.

    ~29k distinct WebinarGeek subscriber emails and ~2k attributed booking
    contacts, so the driver side is trivial; the cost is the per-row hop into
    contacts, which _scanned_page bounds.

    WebinarGeek subscriptions are email-keyed (no contact FK), so the join back
    to contacts goes through lower(email) — served by ix_contacts_lower_email.
    """
    params: dict = {"user_id": LLOYD_USER_ID}
    campaign_id = f.get("campaign_id")
    scope = ""
    if campaign_id:
        params["eng_webinar_id"] = campaign_id
        # The campaign is enforced by the driver itself; drop it as a refinement
        # unless a response filter still needs the invite probe.
        f = {**f, "campaign_id": campaign_id if f.get("response") else None}

    if engagement in ("booked", "won"):
        won_clause = " AND b.won" if engagement == "won" else ""
        if campaign_id:
            scope = " AND b.webinar_id = :eng_webinar_id"
        source = f"""
            SELECT DISTINCT b.contact_id AS cid
            FROM webinar_booking_attribution b
            WHERE b.contact_id IS NOT NULL{won_clause}{scope}
        """
        join = "JOIN driver d ON d.cid = c.id"
        count_sql = f"SELECT count(*) FROM ({source}) t"
    else:
        watched = {
            "attended": " AND (s.watched_live OR s.watched_replay)",
            "live": " AND s.watched_live",
            "replay": " AND s.watched_replay",
            "registered": "",
        }[engagement]
        if campaign_id:
            scope = (
                " AND s.broadcast_id IN (SELECT w.broadcast_id FROM webinars w "
                "WHERE w.id = :eng_webinar_id AND w.broadcast_id IS NOT NULL)"
            )
        source = f"""
            SELECT DISTINCT lower(s.email) AS lemail
            FROM webinargeek_subscribers s
            WHERE s.email IS NOT NULL{watched}{scope}
        """
        join = "JOIN driver d ON d.lemail = lower(c.email)"
        count_sql = f"SELECT count(*) FROM ({source}) t"

    refine = _build_refinements(alias="s", params=params, terms=[], search_fields=search_fields, **f)
    scan_sql = f"""
        WITH driver AS MATERIALIZED ({source})
        SELECT {LIST_COLUMNS_PREFIXED}
        FROM contacts c
        {join}
        WHERE c.user_id = :user_id
    """
    return await _scanned_page(
        db, mode="engagement", scan_sql=scan_sql, refine_sql=refine,
        count_sql=count_sql, params=params, limit=limit, offset=offset,
    )


async def _list_by_response(
    db: AsyncSession, *, webinar_id, response, search_fields, limit, offset, **f
) -> dict:
    """Driver: ix_wci_webinar_response.

    Reads the calendar invites rather than the memberships, so contacts released
    back to the pool after the invite are still found. (webinar_id, email) is
    unique on that table, so one webinar yields at most one row per contact.
    """
    params: dict = {
        "user_id": LLOYD_USER_ID,
        "drv_webinar_id": webinar_id,
        "drv_response": _response_variants(response),
    }
    driver_where = (
        "i.webinar_id = :drv_webinar_id "
        "AND i.calendar_invite_response = ANY(CAST(:drv_response AS text[])) "
        "AND i.matched_contact_id IS NOT NULL"
    )
    refine = _build_refinements(
        alias="s", params=params, terms=[], search_fields=search_fields,
        campaign_id=None, response=None, **f
    )
    scan_sql = f"""
        SELECT {LIST_COLUMNS_PREFIXED}
        FROM webinar_calendar_invites i
        JOIN contacts c ON c.id = i.matched_contact_id
        WHERE {driver_where} AND c.user_id = :user_id
    """
    count_sql = f"SELECT count(*) FROM webinar_calendar_invites i WHERE {driver_where}"
    return await _scanned_page(
        db, mode="response", scan_sql=scan_sql, refine_sql=refine,
        count_sql=count_sql, params=params, limit=limit, offset=offset,
    )


async def _list_by_campaign(
    db: AsyncSession, *, webinar_id, status, search_fields, limit, cursor, **f
) -> dict:
    """Driver: keyset walk of uq_wcm_webinar_contact (webinar_id, contact_id).

    A webinar holds ~250k memberships and each one costs a random contacts_pkey
    heap fetch, so this never materialises the whole cohort: it walks the index
    in contact_id order, one page at a time. `status` filters the MEMBERSHIP
    (assigned/used *in this campaign*) — it lives in the index, so it costs
    nothing and answers the question the campaign filter actually poses.

    When a refinement can reject rows the walk is budgeted (SCAN_BUDGET)
    so a rare value can't turn one page into a 250k-row crawl; `scan_capped`
    then tells the client the page is partial and paging continues from where
    the scan stopped.
    """
    params: dict = {
        "user_id": LLOYD_USER_ID,
        "webinar_id": webinar_id,
        "cursor": cursor or "00000000-0000-0000-0000-000000000000",
    }
    status_clause = ""
    if status:
        params["m_status"] = status
        status_clause = " AND m.status = :m_status"

    where = _build_refinements(
        alias="c", params=params, terms=[], search_fields=search_fields,
        status=None, campaign_id=None, response=None, **f
    )
    tail = f" AND {where}" if where else ""
    budget = SCAN_BUDGET if where else limit + 1
    params["budget"] = budget
    params["limit"] = limit + 1

    sql = sa_text(f"""
        WITH scan AS MATERIALIZED (
            SELECT m.contact_id
            FROM webinar_contact_memberships m
            WHERE m.webinar_id = :webinar_id
              AND m.contact_id > CAST(:cursor AS uuid){status_clause}
            ORDER BY m.contact_id
            LIMIT :budget
        ),
        scan_meta AS (
            SELECT (SELECT count(*) FROM scan) AS scanned,
                   (SELECT contact_id FROM scan ORDER BY contact_id DESC LIMIT 1) AS scan_end
        )
        SELECT (SELECT scanned FROM scan_meta) AS scanned,
               (SELECT scan_end FROM scan_meta)::text AS scan_end,
               {LIST_COLUMNS_PREFIXED}
        FROM scan s
        JOIN contacts c ON c.id = s.contact_id
        WHERE c.user_id = :user_id{tail}
        ORDER BY c.id
        LIMIT :limit
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    scanned = rows[0]["scanned"] if rows else None
    scan_end = rows[0]["scan_end"] if rows else None
    if scanned is None:
        meta = (await db.execute(sa_text(f"""
            WITH scan AS MATERIALIZED (
                SELECT m.contact_id FROM webinar_contact_memberships m
                WHERE m.webinar_id = :webinar_id
                  AND m.contact_id > CAST(:cursor AS uuid){status_clause}
                ORDER BY m.contact_id LIMIT :budget
            )
            SELECT (SELECT count(*) FROM scan) AS scanned,
                   (SELECT contact_id FROM scan ORDER BY contact_id DESC LIMIT 1)::text AS scan_end
        """), params)).mappings().first()
        scanned, scan_end = (meta["scanned"], meta["scan_end"]) if meta else (0, None)

    page_full = len(rows) > limit
    rows = rows[:limit]
    if page_full:
        next_cursor = rows[-1]["id"]
        scan_capped = False
    elif scanned >= budget:
        # Budget exhausted mid-cohort: resume the walk from the last id scanned.
        next_cursor = scan_end
        scan_capped = True
    else:
        next_cursor = None
        scan_capped = False

    total = (await db.execute(sa_text(f"""
        SELECT count(*) FROM webinar_contact_memberships m
        WHERE m.webinar_id = :webinar_id{status_clause}
    """), params)).scalar() or 0

    return {
        "mode": "campaign",
        "contacts": [_summary_dict(r) for r in rows],
        "total": int(total),
        # The cohort size is exact; any refinement narrows it by an unknown amount.
        "total_kind": "cohort" if where else "exact",
        "next_cursor": next_cursor,
        "scan_capped": scan_capped,
    }


async def _list_by_bucket(
    db: AsyncSession, *, bucket_id, search_fields, limit, offset, **f
) -> dict:
    """Driver: ix_contacts_bucket_id / ix_contacts_bucket_filters.

    A bucket can hold six figures of contacts and the index only narrows to the
    bucket — every row still has to be read from the heap — so this goes through
    the budgeted scanner rather than the fused cap. Unfiltered pages read only
    the rows they show; the exact bucket size comes from the index alone.
    """
    params: dict = {"user_id": LLOYD_USER_ID, "drv_bucket_id": bucket_id}
    refine = _build_refinements(
        alias="s", params=params, terms=[], search_fields=search_fields,
        bucket_id=None, campaign_id=None, **f
    )
    scan_sql = f"""
        SELECT {LIST_COLUMNS}
        FROM contacts
        WHERE user_id = :user_id AND bucket_id = :drv_bucket_id
    """
    # count(*) over a six-figure bucket is a ~30s index walk per page view;
    # outreach_buckets.total_contacts is maintained at write time and exact.
    count_sql = (
        "SELECT total_contacts FROM outreach_buckets "
        "WHERE id = :drv_bucket_id AND user_id = :user_id"
    )
    return await _scanned_page(
        db, mode="bucket", scan_sql=scan_sql, refine_sql=refine,
        count_sql=count_sql, params=params, limit=limit, offset=offset,
    )


async def _list_by_blocklist(
    db: AsyncSession, *, search_fields, limit, offset, **f
) -> dict:
    """Driver: the partial is_blocklisted indexes — ~12k rows, small enough to
    scan the whole cohort under the cap and refine it in place."""
    params: dict = {"user_id": LLOYD_USER_ID}
    refine = _build_refinements(
        alias="s", params=params, terms=[], search_fields=search_fields,
        blocklisted=None, bucket_id=None, campaign_id=None, **f
    )
    scan_sql = f"""
        SELECT {LIST_COLUMNS}
        FROM contacts
        WHERE user_id = :user_id AND is_blocklisted
    """
    return await _scanned_page(
        db, mode="blocklist", scan_sql=scan_sql, refine_sql=refine, params=params,
        limit=limit, offset=offset, scan_cap=SEARCH_MATCH_CAP,
    )


def _summary_columns():
    return (
        Contact.id, Contact.email, Contact.first_name, Contact.last_name,
        Contact.title, Contact.seniority, Contact.company_website, Contact.industry,
        Contact.bucket_name, Contact.lead_list_name, Contact.country,
        Contact.list_location, Contact.employee_range, Contact.employee_count,
        Contact.outreach_status, Contact.is_blocklisted, Contact.times_invited,
        Contact.last_invited_at, Contact.created_at,
    )


def _summary_dict(r) -> dict:
    # Works for both ORM rows (browse) and raw-SQL mappings (every other mode).
    # Raw rows keep id as uuid; str() is a no-op on the ORM's UUID(as_uuid=False).
    get = r.get if hasattr(r, "get") else lambda k: getattr(r, k)
    return {
        "id": str(get("id")),
        "email": get("email"),
        "first_name": get("first_name"),
        "last_name": get("last_name"),
        "title": get("title"),
        "seniority": get("seniority"),
        "company_website": get("company_website"),
        "industry": get("industry"),
        "bucket_name": get("bucket_name"),
        "lead_list_name": get("lead_list_name"),
        "country": get("country") or get("list_location"),
        "employee_range": get("employee_range"),
        "employee_count": get("employee_count"),
        "outreach_status": get("outreach_status"),
        "is_blocklisted": get("is_blocklisted"),
        "times_invited": get("times_invited"),
        "last_invited_at": _iso(get("last_invited_at")),
        "created_at": _iso(get("created_at")),
    }


# ── Per-page engagement rollups ──────────────────────────────────────────────

class ContactEngagementRequest(BaseModel):
    contact_ids: list[str] = Field(..., min_length=1, max_length=MAX_PAGE_SIZE)


@router.post("/contacts/engagement")
async def contact_engagement(
    body: ContactEngagementRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Campaign / response / attendance / booking counters for one page of the
    directory.

    Deliberately a second request rather than extra columns on the listing: the
    rollups are four index-driven lookups per contact and the table should paint
    before they land. Every join is a per-contact index hit (ix_wcm_contact,
    ix_wci_matched_contact_id, ix_wg_subs_email, ix_wba_app_contact), so the
    cost is bounded by the page size, not the table size.
    """
    ids = []
    for cid in body.contact_ids:
        try:
            uuid_mod.UUID(cid)
        except ValueError:
            continue
        ids.append(cid)
    if not ids:
        return {"engagement": {}}

    emails = [
        e for (e,) in (await db.execute(
            select(Contact.email).where(
                Contact.id.in_(ids), Contact.user_id == LLOYD_USER_ID, Contact.email.isnot(None)
            )
        )).all()
    ]
    # ix_wg_subs_email is on the raw column, so probe both casings rather than
    # wrapping it in lower() and losing the index.
    email_variants = sorted({e for e in emails} | {e.lower() for e in emails})

    sql = sa_text("""
        WITH ids AS (SELECT unnest(CAST(:ids AS uuid[])) AS id),
        c AS (
            SELECT ct.id, ct.email FROM contacts ct
            JOIN ids ON ids.id = ct.id
            WHERE ct.user_id = :user_id
        ),
        mem AS (
            SELECT m.contact_id AS id,
                   count(*) AS campaigns,
                   count(*) FILTER (WHERE m.status = 'used') AS used,
                   count(*) FILTER (WHERE m.status = 'assigned') AS assigned
            FROM webinar_contact_memberships m
            JOIN c ON c.id = m.contact_id
            GROUP BY 1
        ),
        last_w AS (
            SELECT DISTINCT ON (m.contact_id)
                   m.contact_id AS id, w.number, w.variant_label, w.date
            FROM webinar_contact_memberships m
            JOIN c ON c.id = m.contact_id
            JOIN webinars w ON w.id = m.webinar_id
            ORDER BY m.contact_id, w.date DESC, w.number DESC
        ),
        inv AS (
            SELECT i.matched_contact_id AS id,
                   count(*) AS invited,
                   count(*) FILTER (WHERE lower(i.calendar_invite_response) = 'yes') AS accepted,
                   count(*) FILTER (WHERE lower(i.calendar_invite_response) = 'maybe') AS maybe,
                   count(*) FILTER (WHERE lower(i.calendar_invite_response) = 'no') AS declined,
                   count(*) FILTER (WHERE lower(i.calendar_invite_response) IN ('deleted', 'spam')) AS negative,
                   max(i.calendar_invite_response) FILTER (
                       WHERE lower(i.calendar_invite_response) IN ('yes', 'maybe')
                   ) AS best_response
            FROM webinar_calendar_invites i
            JOIN c ON c.id = i.matched_contact_id
            GROUP BY 1
        ),
        att AS (
            SELECT lower(s.email) AS lemail,
                   count(*) AS registered,
                   count(*) FILTER (WHERE s.watched_live) AS live,
                   count(*) FILTER (WHERE s.watched_replay) AS replay,
                   coalesce(sum(s.minutes_viewing), 0) AS minutes
            FROM webinargeek_subscribers s
            WHERE s.email = ANY(CAST(:emails AS text[]))
            GROUP BY 1
        ),
        bk AS (
            SELECT b.contact_id AS id,
                   count(*) AS bookings,
                   count(*) FILTER (WHERE b.won) AS won,
                   max(b.booked_at) AS last_booked_at
            FROM webinar_booking_attribution b
            JOIN c ON c.id = b.contact_id
            GROUP BY 1
        )
        SELECT c.id::text AS id,
               coalesce(mem.campaigns, 0) AS campaigns,
               coalesce(mem.used, 0) AS used,
               coalesce(mem.assigned, 0) AS assigned,
               last_w.number AS last_webinar_number,
               last_w.variant_label AS last_webinar_variant,
               last_w.date AS last_webinar_date,
               coalesce(inv.invited, 0) AS invited,
               coalesce(inv.accepted, 0) AS accepted,
               coalesce(inv.maybe, 0) AS maybe,
               coalesce(inv.declined, 0) AS declined,
               coalesce(inv.negative, 0) AS negative,
               inv.best_response,
               coalesce(att.registered, 0) AS registered,
               coalesce(att.live, 0) AS live,
               coalesce(att.replay, 0) AS replay,
               coalesce(att.minutes, 0) AS minutes,
               coalesce(bk.bookings, 0) AS bookings,
               coalesce(bk.won, 0) AS won,
               bk.last_booked_at
        FROM c
        LEFT JOIN mem ON mem.id = c.id
        LEFT JOIN last_w ON last_w.id = c.id
        LEFT JOIN inv ON inv.id = c.id
        LEFT JOIN att ON att.lemail = lower(c.email)
        LEFT JOIN bk ON bk.id = c.id
    """)
    rows = (await db.execute(
        sql, {"ids": ids, "user_id": LLOYD_USER_ID, "emails": email_variants}
    )).mappings().all()

    return {
        "engagement": {
            r["id"]: {
                "campaigns": r["campaigns"],
                "used": r["used"],
                "assigned": r["assigned"],
                "last_webinar_number": r["last_webinar_number"],
                "last_webinar_variant": r["last_webinar_variant"],
                "last_webinar_date": _iso(r["last_webinar_date"]),
                "invited": r["invited"],
                "accepted": r["accepted"],
                "maybe": r["maybe"],
                "declined": r["declined"],
                "negative": r["negative"],
                "best_response": r["best_response"],
                "registered": r["registered"],
                "live": r["live"],
                "replay": r["replay"],
                "minutes": r["minutes"],
                "bookings": r["bookings"],
                "won": r["won"],
                "last_booked_at": _iso(r["last_booked_at"]),
            }
            for r in rows
        }
    }


@router.get("/contacts/{contact_id}")
async def get_contact_detail(
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Everything known about one contact: profile fields, per-webinar history
    (membership + calendar response + attendance + booking + the copy they were
    sent), attributed bookings, release history, blocklist entry and the CRM
    record matched by email.

    All lookups are per-contact index hits (pkey, ix_wcm_contact,
    ix_wci_matched_contact_id, ix_wba_app_contact, ix_release_log_contact,
    ix_ghl_contact_email, per-webinar email keys) — no scans, safe at directory
    scale.
    """
    try:
        uuid_mod.UUID(contact_id)
    except ValueError:
        raise HTTPException(404, "Contact not found")

    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != LLOYD_USER_ID:
        raise HTTPException(404, "Contact not found")

    m = WebinarContactMembership
    title_copy = BucketCopy.__table__.alias("title_copy")
    desc_copy = BucketCopy.__table__.alias("desc_copy")
    mem_rows = (await db.execute(
        select(
            m, Webinar, WebinarListAssignment, OutreachBucket, OutreachSender,
            title_copy.c.text.label("title_text"), desc_copy.c.text.label("desc_text"),
        )
        .join(Webinar, Webinar.id == m.webinar_id)
        .outerjoin(WebinarListAssignment, WebinarListAssignment.id == m.assignment_id)
        .outerjoin(OutreachBucket, OutreachBucket.id == m.bucket_id)
        .outerjoin(OutreachSender, OutreachSender.id == WebinarListAssignment.sender_id)
        .outerjoin(title_copy, title_copy.c.id == WebinarListAssignment.title_copy_id)
        .outerjoin(desc_copy, desc_copy.c.id == WebinarListAssignment.desc_copy_id)
        .where(m.contact_id == contact_id, m.user_id == LLOYD_USER_ID)
    )).all()
    member_webinar_ids = [row[0].webinar_id for row in mem_rows]

    # Calendar responses: rows matched to this contact at import time, plus (for
    # the webinars they belong to) rows that share the raw email but were never
    # matched. Both paths are index hits; merged by webinar with matched-rows
    # priority.
    invites: dict[str, WebinarCalendarInvite] = {}
    if contact.email and member_webinar_ids:
        for inv in (await db.execute(
            select(WebinarCalendarInvite).where(
                WebinarCalendarInvite.webinar_id.in_(member_webinar_ids),
                WebinarCalendarInvite.email == contact.email,
            )
        )).scalars():
            invites[inv.webinar_id] = inv
    for inv in (await db.execute(
        select(WebinarCalendarInvite).where(WebinarCalendarInvite.matched_contact_id == contact_id)
    )).scalars():
        invites[inv.webinar_id] = inv

    # Non-joiner re-invites live in their own table (email-keyed, no contact FK);
    # they label the response on nonjoiner memberships.
    nj_invites: dict[str, WebinarNonjoinerInvite] = {}
    if contact.email and member_webinar_ids:
        for inv in (await db.execute(
            select(WebinarNonjoinerInvite).where(
                WebinarNonjoinerInvite.webinar_id.in_(member_webinar_ids),
                WebinarNonjoinerInvite.email == contact.email,
            )
        )).scalars():
            nj_invites[inv.webinar_id] = inv

    booking_rows = (await db.execute(
        select(WebinarBookingAttribution, Webinar)
        .outerjoin(Webinar, Webinar.id == WebinarBookingAttribution.webinar_id)
        .where(WebinarBookingAttribution.contact_id == contact_id)
    )).all()

    # WebinarGeek attendance: subscriber rows by email (ix_wg_subs_email), tied
    # to webinars through webinars.broadcast_id. Exact-match on both the raw and
    # lowercased email so the index stays usable either way.
    subs_by_broadcast: dict[str, WebinarGeekSubscriber] = {}
    email_variants = {e for e in {contact.email, (contact.email or "").lower()} if e}
    if email_variants:
        for s in (await db.execute(
            select(WebinarGeekSubscriber).where(WebinarGeekSubscriber.email.in_(email_variants))
        )).scalars():
            subs_by_broadcast[s.broadcast_id] = s

    # History rows keyed by webinar: memberships first, then invite-only rows
    # (e.g. the contact was since released — membership deleted, invite kept),
    # then registration-only rows (a WG subscription with no invite footprint).
    webinars_by_id: dict[str, Webinar] = {row[1].id: row[1] for row in mem_rows}
    extra_ids = set(invites) - set(webinars_by_id)
    if extra_ids:
        for w in (await db.execute(select(Webinar).where(Webinar.id.in_(extra_ids)))).scalars():
            webinars_by_id[w.id] = w
    if subs_by_broadcast:
        known_bids = {w.broadcast_id for w in webinars_by_id.values() if w.broadcast_id}
        missing_bids = set(subs_by_broadcast) - known_bids
        if missing_bids:
            for w in (await db.execute(
                select(Webinar).where(
                    Webinar.user_id == LLOYD_USER_ID, Webinar.broadcast_id.in_(missing_bids)
                )
            )).scalars():
                webinars_by_id.setdefault(w.id, w)

    # Sender fallback for rows without an assignment sender: the per-webinar
    # calendar-account → sender mapping maintained on Account Health.
    cas_sender: dict[tuple[str, str], str] = {}
    inv_wids = {inv.webinar_id for inv in invites.values() if inv.calendar_account}
    if inv_wids:
        for wid, acct, name in (await db.execute(
            select(CalendarAccountSender.webinar_id, CalendarAccountSender.calendar_account, OutreachSender.name)
            .join(OutreachSender, OutreachSender.id == CalendarAccountSender.sender_id)
            .where(CalendarAccountSender.webinar_id.in_(inv_wids))
        )).all():
            cas_sender[(wid, acct)] = name

    # One booking summary per attributed webinar for the history table; the
    # full per-appointment list still ships in `bookings` below.
    booking_by_webinar: dict[str, dict] = {}
    for (b, _w) in booking_rows:
        if not b.webinar_id:
            continue
        cur = booking_by_webinar.get(b.webinar_id)
        if cur is None or (b.booked_at and (cur["booked_at"] or "") < _iso(b.booked_at)):
            booking_by_webinar[b.webinar_id] = {
                "booked_at": _iso(b.booked_at),
                "call_at": _iso(b.call_at),
                "call_status": b.call_status,
                "won": (cur or {}).get("won") or b.won,
                "disqualified": (cur or {}).get("disqualified") or b.disqualified,
            }
        else:
            cur["won"] = cur["won"] or b.won
            cur["disqualified"] = cur["disqualified"] or b.disqualified

    def _attendance_for(w: Webinar) -> dict | None:
        s = subs_by_broadcast.get(w.broadcast_id) if w.broadcast_id else None
        if s is None:
            return None
        return {
            "subscribed_at": _iso(s.subscribed_at),
            "watched_live": s.watched_live,
            "watched_replay": s.watched_replay,
            "minutes_viewing": s.minutes_viewing,
            "unsubscribed_at": _iso(s.unsubscribed_at),
            "unsubscribe_source": s.unsubscribe_source,
            "registration_source": s.registration_source,
            "viewing_device": s.viewing_device,
            "viewing_country": s.viewing_country,
        }

    def _sender_for(w: Webinar, asgn_sender_name, inv) -> str | None:
        if asgn_sender_name:
            return asgn_sender_name
        if inv is not None and inv.calendar_account:
            return cas_sender.get((w.id, inv.calendar_account))
        return None

    history = []
    seen_webinars = set()
    for (mem, w, asgn, bucket, sender, title_text, desc_text) in mem_rows:
        seen_webinars.add(w.id)
        if asgn is not None and asgn.list_name:
            list_label = asgn.list_name
        elif bucket is not None:
            list_label = bucket.name
        elif asgn is not None and asgn.is_nonjoiners:
            list_label = "Nonjoiners"
        else:
            list_label = None
        inv = invites.get(w.id)
        nj = nj_invites.get(w.id)
        history.append({
            "webinar_id": w.id,
            "webinar_number": w.number,
            "variant_label": w.variant_label,
            "webinar_date": _iso(w.date),
            "webinar_status": w.status,
            "list_label": list_label,
            "bucket_name": bucket.name if bucket is not None else None,
            "is_nonjoiners": bool(asgn.is_nonjoiners) if asgn is not None else False,
            "membership_status": mem.status,
            "assigned_date": _iso(mem.assigned_date),
            "used_at": _iso(mem.used_at),
            "calendar_response": (inv.calendar_invite_response if inv else None)
                or (nj.calendar_invite_response if nj else None),
            "calendar_invited_date": _iso(inv.calendar_invited_date) if inv else None,
            "calendar_account": inv.calendar_account if inv else None,
            "sender_name": _sender_for(w, sender.name if sender else None, inv),
            "invite_title": title_text,
            "invite_description": desc_text,
            "attendance": _attendance_for(w),
            "booking": booking_by_webinar.get(w.id),
        })
    for wid, inv in invites.items():
        if wid in seen_webinars:
            continue
        w = webinars_by_id.get(wid)
        if w is None:
            continue
        seen_webinars.add(wid)
        history.append({
            "webinar_id": w.id,
            "webinar_number": w.number,
            "variant_label": w.variant_label,
            "webinar_date": _iso(w.date),
            "webinar_status": w.status,
            "list_label": None,
            "bucket_name": None,
            "is_nonjoiners": False,
            "membership_status": None,
            "assigned_date": None,
            "used_at": None,
            "calendar_response": inv.calendar_invite_response,
            "calendar_invited_date": _iso(inv.calendar_invited_date),
            "calendar_account": inv.calendar_account,
            "sender_name": _sender_for(w, None, inv),
            "invite_title": None,
            "invite_description": None,
            "attendance": _attendance_for(w),
            "booking": booking_by_webinar.get(w.id),
        })
    # Registration-only rows: a WebinarGeek subscription for a webinar with no
    # membership or calendar invite (e.g. self-registered via the funnel).
    for w in webinars_by_id.values():
        if w.id in seen_webinars or not w.broadcast_id or w.broadcast_id not in subs_by_broadcast:
            continue
        seen_webinars.add(w.id)
        history.append({
            "webinar_id": w.id,
            "webinar_number": w.number,
            "variant_label": w.variant_label,
            "webinar_date": _iso(w.date),
            "webinar_status": w.status,
            "list_label": None,
            "bucket_name": None,
            "is_nonjoiners": False,
            "membership_status": None,
            "assigned_date": None,
            "used_at": None,
            "calendar_response": None,
            "calendar_invited_date": None,
            "calendar_account": None,
            "sender_name": None,
            "invite_title": None,
            "invite_description": None,
            "attendance": _attendance_for(w),
            "booking": booking_by_webinar.get(w.id),
        })
    history.sort(
        key=lambda h: (h["webinar_date"] or "", h["webinar_number"] or 0),
        reverse=True,
    )

    bookings = [
        {
            "appointment_id": b.appointment_id,
            "booked_at": _iso(b.booked_at),
            "call_at": _iso(b.call_at),
            "call_status": b.call_status,
            "lead_quality": b.lead_quality,
            "won": b.won,
            "disqualified": b.disqualified,
            "attribution_source": b.attribution_source,
            "webinar_number": w.number if w else None,
            "variant_label": w.variant_label if w else None,
        }
        for (b, w) in booking_rows
    ]
    bookings.sort(key=lambda b: b["booked_at"] or "", reverse=True)

    upload = None
    if contact.upload_id:
        up = await db.get(UploadHistory, contact.upload_id)
        if up:
            upload = {
                "file_name": up.file_name,
                "uploaded_at": _iso(up.created_at),
                "list_location": up.list_location,
                "custom_list_name": up.custom_list_name,
            }

    # Releases: the contact was pulled back out of a webinar list after being
    # claimed. Invisible everywhere else, and the reason a webinar can appear in
    # the history with no membership row.
    release_rows = (await db.execute(
        select(ContactReleaseLog, Webinar)
        .outerjoin(Webinar, Webinar.id == ContactReleaseLog.webinar_id)
        .where(ContactReleaseLog.contact_id == contact_id)
        .order_by(ContactReleaseLog.released_at.desc())
        .limit(50)
    )).all()
    releases = [
        {
            "released_at": _iso(r.released_at),
            "prior_status": r.prior_status,
            "prior_used_at": _iso(r.prior_used_at),
            "webinar_number": w.number if w else None,
            "variant_label": w.variant_label if w else None,
        }
        for (r, w) in release_rows
    ]

    blocklist = None
    if contact.email:
        entry = (await db.execute(
            select(BlocklistEntry).where(
                BlocklistEntry.user_id == LLOYD_USER_ID,
                BlocklistEntry.email == contact.email,
            )
        )).scalar_one_or_none()
        if entry:
            blocklist = {
                "source": entry.source,
                "reason": entry.reason,
                "source_ref": entry.source_ref,
                "created_at": _iso(entry.created_at),
            }

    # CRM record, matched by email (ix_ghl_contact_email). Carries the signals
    # that never reach the contacts table: tags, unsubscribe date, self-registration
    # and the campaign attribution GHL captured at booking time.
    crm = None
    if contact.email:
        g = (await db.execute(
            select(GHLContact).where(GHLContact.email == contact.email.lower()).limit(1)
        )).scalar_one_or_none()
        if g is None:
            g = (await db.execute(
                select(GHLContact).where(GHLContact.email == contact.email).limit(1)
            )).scalar_one_or_none()
        if g is not None:
            crm = {
                "ghl_contact_id": g.ghl_contact_id,
                "date_added": _iso(g.date_added),
                "tags": g.tags or [],
                "is_booked_call": g.is_booked_call,
                "booked_call_webinar_series": g.booked_call_webinar_series,
                "self_registered_at": _iso(g.webinar_registration_in_form_date),
                "unsubscribed_at": _iso(g.cold_calendar_unsubscribe_date),
                "has_sms_click_tag": g.has_sms_click_tag,
                "invite_response_history": g.calendar_invite_response_history,
                "webinar_series_history": g.calendar_webinar_series_history,
                "nonjoiner_series_history": g.calendar_webinar_series_non_joiners,
                "registration_campaign_source": g.registration_campaign_source,
                "registration_campaign_medium": g.registration_campaign_medium,
                "registration_campaign_name": g.registration_campaign_name,
                "book_campaign_source": g.book_campaign_source,
                "book_campaign_medium": g.book_campaign_medium,
                "book_campaign_name": g.book_campaign_name,
                "minutes_watched_total": g.zoom_viewing_time_in_minutes_total,
                "sessions_attended": g.zoom_webinar_series_attended_total_count,
                "sessions_registered": g.zoom_webinar_series_registered_total_count,
                "synced_at": _iso(g.synced_at),
            }

    return {
        "contact": {
            "id": contact.id,
            "email": contact.email,
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "title": contact.title,
            "seniority": contact.seniority,
            "company_website": contact.company_website,
            "industry": contact.industry,
            "sector": contact.sector,
            "country": contact.country,
            "list_location": contact.list_location,
            "company_country": contact.company_country,
            "employee_range": contact.employee_range,
            "employee_count": contact.employee_count,
            "company_founded_year": contact.company_founded_year,
            "company_annual_revenue": contact.company_annual_revenue,
            "company_total_funding": contact.company_total_funding,
            "bucket_name": contact.bucket_name,
            "lead_list_name": contact.lead_list_name,
            "segment_name": contact.segment_name,
            "classification": contact.classification,
            "enrichment_classification": contact.enrichment_classification,
            "primary_identity": contact.primary_identity,
            "sub_identity": contact.sub_identity,
            "database_provider": contact.database_provider,
            "scraper": contact.scraper,
            "source_contact_id": contact.contact_id,
            "source_created_date": contact.created_date,
            "outreach_status": contact.outreach_status,
            "is_blocklisted": contact.is_blocklisted,
            "times_invited": contact.times_invited,
            "assigned_membership_count": contact.assigned_membership_count,
            "last_invited_at": _iso(contact.last_invited_at),
            "created_at": _iso(contact.created_at),
            "updated_at": _iso(contact.updated_at),
            "custom_data": contact.custom_data or {},
            "upload": upload,
        },
        "webinar_history": history,
        "bookings": bookings,
        "releases": releases,
        "blocklist": blocklist,
        "crm": crm,
    }


@router.get("/custom-fields")
async def list_custom_fields(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(
        select(ContactCustomField)
        .where(ContactCustomField.user_id == LLOYD_USER_ID)
        .order_by(ContactCustomField.display_order)
    )
    fields = result.scalars().all()
    return {
        "fields": [
            {
                "id": f.id,
                "field_name": f.field_name,
                "field_type": f.field_type,
                "display_order": f.display_order,
            }
            for f in fields
        ]
    }


@router.post("/custom-fields", status_code=201)
async def create_custom_field(
    body: CustomFieldCreate,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    existing = await db.execute(
        select(ContactCustomField).where(
            ContactCustomField.user_id == LLOYD_USER_ID,
            ContactCustomField.field_name == body.field_name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Custom field '{body.field_name}' already exists")

    max_order = await db.execute(
        select(sa_func.max(ContactCustomField.display_order))
        .where(ContactCustomField.user_id == LLOYD_USER_ID)
    )
    next_order = (max_order.scalar() or 0) + 1

    field = ContactCustomField(
        user_id=LLOYD_USER_ID,
        field_name=body.field_name,
        field_type=body.field_type,
        display_order=next_order,
    )
    db.add(field)
    await db.flush()

    return {
        "id": field.id,
        "field_name": field.field_name,
        "field_type": field.field_type,
        "display_order": field.display_order,
    }
