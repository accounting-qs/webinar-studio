"""Outreach sub-router: Custom Fields + the Contacts directory (search/detail)."""
import uuid as uuid_mod

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text as sa_text, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import LLOYD_USER_ID
from api.schemas import CustomFieldCreate
from db.models import (
    Contact,
    ContactCustomField,
    OutreachBucket,
    UploadHistory,
    Webinar,
    WebinarBookingAttribution,
    WebinarCalendarInvite,
    WebinarContactMembership,
    WebinarListAssignment,
    WebinarNonjoinerInvite,
)
from db.session import get_db

router = APIRouter()

# Must match the expression of ix_contacts_search_trgm (migration 075)
# structurally, or the planner will not use the index and every search becomes
# a full heap scan of contacts.
SEARCH_EXPR = (
    "coalesce(email, '') || ' ' || coalesce(first_name, '') || ' ' || "
    "coalesce(last_name, '') || ' ' || coalesce(company_website, '') || ' ' || "
    "coalesce(bucket_name, '') || ' ' || coalesce(lead_list_name, '')"
)

# Stop collecting matches past this many rows: bounds the sort, the count and —
# critically — how far a broad term's bitmap/seq scan runs. filtered_total ==
# the cap means "more than cap-1 matches", surfaced as total_kind='capped'.
SEARCH_MATCH_CAP = 10_001

MAX_PAGE_SIZE = 200


def _iso(v):
    return v.isoformat() if v is not None else None


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/contacts")
async def list_contacts(
    search: str = Query("", max_length=200),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(None, max_length=500),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Contacts directory: browse everything, or substring-search across
    email / name / company website / bucket / lead list.

    Two modes with different pagination, because of the table's scale (5.6M):
    - browse (no search): keyset on email via uq_contacts_user_email — cheap at
      any depth, `cursor` = last email of the previous page. `total` is the
      planner's row estimate, not a count.
    - search: one bounded scan materialises at most SEARCH_MATCH_CAP matches,
      which are then ordered/paged by offset. `total` is exact below the cap.
    """
    terms = [t for t in search.strip().split() if t]

    if not terms:
        # ── Browse mode: keyset by email ────────────────────────────────
        cols = _summary_columns()
        q = select(*cols).where(Contact.user_id == LLOYD_USER_ID)
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
        }

    # ── Search mode ─────────────────────────────────────────────────────
    # A pattern under 3 chars yields no trigram, degrading the GIN lookup to a
    # full-index recheck (i.e. a table scan). Require one indexable term;
    # shorter extra terms are fine — they only filter the bounded candidates.
    if not any(len(t) >= 3 for t in terms):
        raise HTTPException(422, "Search needs at least one term of 3+ characters")
    terms = terms[:5]

    where_parts = ["user_id = :user_id"]
    params: dict = {
        "user_id": LLOYD_USER_ID,
        "cap": SEARCH_MATCH_CAP,
        "limit": limit,
        "offset": offset,
    }
    for i, term in enumerate(terms):
        where_parts.append(f"({SEARCH_EXPR}) ILIKE :term_{i}")
        params[f"term_{i}"] = _like_pattern(term)

    # MATERIALIZED keeps the planner from inlining the search into an ordered
    # index walk over (user_id, email) — which under LIMIT turns a selective
    # search into an unbounded random-order heap crawl. The CTE also carries the
    # display columns so ordering never re-visits the heap.
    sql = sa_text(f"""
        WITH matches AS MATERIALIZED (
            SELECT id::text AS id, email, first_name, last_name, company_website,
                   bucket_name, lead_list_name, country, list_location,
                   employee_range, outreach_status, is_blocklisted,
                   times_invited, last_invited_at, created_at
            FROM contacts
            WHERE {' AND '.join(where_parts)}
            LIMIT :cap
        )
        SELECT (SELECT count(*) FROM matches) AS filtered_total, m.*
        FROM matches m
        ORDER BY m.email
        LIMIT :limit OFFSET :offset
    """)
    rows = (await db.execute(sql, params)).mappings().all()

    if rows:
        filtered_total = rows[0]["filtered_total"]
    elif offset > 0:
        # Paged past the end; re-count the bounded match set for an exact total.
        count_sql = sa_text(f"""
            SELECT count(*) FROM (
                SELECT 1 FROM contacts
                WHERE {' AND '.join(where_parts)}
                LIMIT :cap
            ) t
        """)
        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        filtered_total = (await db.execute(count_sql, count_params)).scalar() or 0
    else:
        filtered_total = 0

    capped = filtered_total >= SEARCH_MATCH_CAP
    return {
        "mode": "search",
        "contacts": [_summary_dict(r) for r in rows],
        "total": (SEARCH_MATCH_CAP - 1) if capped else filtered_total,
        "total_kind": "capped" if capped else "exact",
        "next_cursor": None,
    }


def _summary_columns():
    return (
        Contact.id, Contact.email, Contact.first_name, Contact.last_name,
        Contact.company_website, Contact.bucket_name, Contact.lead_list_name,
        Contact.country, Contact.list_location, Contact.employee_range,
        Contact.outreach_status, Contact.is_blocklisted, Contact.times_invited,
        Contact.last_invited_at, Contact.created_at,
    )


def _summary_dict(r) -> dict:
    # Works for both ORM rows (browse) and raw-SQL mappings (search). Raw rows
    # already cast id::text; ORM columns are str via UUID(as_uuid=False).
    get = r.get if hasattr(r, "get") else lambda k: getattr(r, k)
    return {
        "id": get("id"),
        "email": get("email"),
        "first_name": get("first_name"),
        "last_name": get("last_name"),
        "company_website": get("company_website"),
        "bucket_name": get("bucket_name"),
        "lead_list_name": get("lead_list_name"),
        "country": get("country") or get("list_location"),
        "employee_range": get("employee_range"),
        "outreach_status": get("outreach_status"),
        "is_blocklisted": get("is_blocklisted"),
        "times_invited": get("times_invited"),
        "last_invited_at": _iso(get("last_invited_at")),
        "created_at": _iso(get("created_at")),
    }


@router.get("/contacts/{contact_id}")
async def get_contact_detail(
    contact_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Everything known about one contact: profile fields, per-webinar history
    (membership + calendar response), and attributed bookings.

    All lookups are per-contact index hits (pkey, ix_wcm_contact,
    ix_wci_matched_contact_id, ix_wba_app_contact, per-webinar email keys) —
    no scans, safe at directory scale.
    """
    try:
        uuid_mod.UUID(contact_id)
    except ValueError:
        raise HTTPException(404, "Contact not found")

    contact = await db.get(Contact, contact_id)
    if not contact or contact.user_id != LLOYD_USER_ID:
        raise HTTPException(404, "Contact not found")

    m = WebinarContactMembership
    mem_rows = (await db.execute(
        select(m, Webinar, WebinarListAssignment, OutreachBucket)
        .join(Webinar, Webinar.id == m.webinar_id)
        .outerjoin(WebinarListAssignment, WebinarListAssignment.id == m.assignment_id)
        .outerjoin(OutreachBucket, OutreachBucket.id == m.bucket_id)
        .where(m.contact_id == contact_id, m.user_id == LLOYD_USER_ID)
    )).all()
    member_webinar_ids = [mem.webinar_id for (mem, _w, _a, _b) in mem_rows]

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

    # History rows keyed by webinar: memberships first, then invite-only rows
    # (e.g. the contact was since released — membership deleted, invite kept).
    webinars_by_id: dict[str, Webinar] = {w.id: w for (_m, w, _a, _b) in mem_rows}
    extra_ids = set(invites) - set(webinars_by_id)
    if extra_ids:
        for w in (await db.execute(select(Webinar).where(Webinar.id.in_(extra_ids)))).scalars():
            webinars_by_id[w.id] = w

    history = []
    seen_webinars = set()
    for (mem, w, asgn, bucket) in mem_rows:
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
            "list_label": list_label,
            "is_nonjoiners": bool(asgn.is_nonjoiners) if asgn is not None else False,
            "membership_status": mem.status,
            "assigned_date": _iso(mem.assigned_date),
            "used_at": _iso(mem.used_at),
            "calendar_response": (inv.calendar_invite_response if inv else None)
                or (nj.calendar_invite_response if nj else None),
            "calendar_invited_date": _iso(inv.calendar_invited_date) if inv else None,
            "calendar_account": inv.calendar_account if inv else None,
        })
    for wid, inv in invites.items():
        if wid in seen_webinars:
            continue
        w = webinars_by_id.get(wid)
        if w is None:
            continue
        history.append({
            "webinar_id": w.id,
            "webinar_number": w.number,
            "variant_label": w.variant_label,
            "webinar_date": _iso(w.date),
            "list_label": None,
            "is_nonjoiners": False,
            "membership_status": None,
            "assigned_date": None,
            "used_at": None,
            "calendar_response": inv.calendar_invite_response,
            "calendar_invited_date": _iso(inv.calendar_invited_date),
            "calendar_account": inv.calendar_account,
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
            upload = {"file_name": up.file_name, "uploaded_at": _iso(up.created_at)}

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
            "outreach_status": contact.outreach_status,
            "is_blocklisted": contact.is_blocklisted,
            "times_invited": contact.times_invited,
            "last_invited_at": _iso(contact.last_invited_at),
            "created_at": _iso(contact.created_at),
            "custom_data": contact.custom_data or {},
            "upload": upload,
        },
        "webinar_history": history,
        "bookings": bookings,
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
