"""Persist per-booking → webinar attribution (webinar_booking_attribution).

Wraps the pure `attribute_booking` cascade (services/ghl_appointments.py) with the
DB loads it needs and upserts one row per first-call ghl_appointment. Shared by:
- the GHL sync (capture-forward: lock each booking's webinar while the opp's
  webinar_source_number is still fresh), and
- scripts/backfill_booking_attribution.py (best-effort past; never re-attributes
  a locked row's webinar).

A locked row freezes its *attribution* (which webinar), because GHL overwrites the
opp's webinar_source_number; the *outcome* columns (won / disqualified /
lead_quality / call_status) still refresh on every sync, since a deal closes or a
call shows long after the booking.

The sales funnel reads this table instead of GHL's single, overwritten opportunity.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.routers.outreach._helpers import LLOYD_USER_ID
from db.models import WebinarBookingAttribution
from services.ghl_appointments import attribute_booking, status_category
from services.ghl_statistics_source import DEAL_WON_STAGE_ID, DISQUALIFIED_STAGE_ID

CLASS_FIRST = "first"


async def _load_webinar_context(db: AsyncSession) -> dict:
    """Global webinar lookups (small table): by number, by date (asc), by broadcast."""
    rows = (await db.execute(sa_text(
        "SELECT id, number, variant_label, date, broadcast_id FROM webinars WHERE user_id = :u"
    ), {"u": LLOYD_USER_ID})).all()
    webs = [
        {"webinar_id": r[0], "number": r[1], "variant_label": r[2], "date": r[3], "broadcast_id": r[4]}
        for r in rows
    ]
    by_number: dict[int, list[dict]] = {}
    by_broadcast: dict[str, dict] = {}
    for w in webs:
        if w["number"] is not None:
            by_number.setdefault(w["number"], []).append(w)
        if w["broadcast_id"]:
            by_broadcast[w["broadcast_id"]] = w
    by_date = sorted((w for w in webs if w["date"] is not None), key=lambda w: w["date"])
    by_id = {w["webinar_id"]: w for w in webs}
    return {"by_number": by_number, "by_broadcast": by_broadcast, "by_date": by_date, "by_id": by_id}


async def rebuild_attributions(
    db: AsyncSession,
    ghl_contact_ids: list[str],
    *,
    lock: bool,
    webinar_ctx: dict | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Compute (and unless dry_run, upsert) attribution rows for the given GHL
    contacts. Returns the computed rows (one per first-call appointment).

    lock=True  → capture-forward: rows are stamped locked_at=now and future runs
                 won't move them (the opp's webinar_source_number is fresh now).
    lock=False → backfill: upserts unlocked rows; a row already locked is left as-is.
    Either way the ON CONFLICT never overwrites a locked row.
    """
    if not ghl_contact_ids:
        return []
    ctx = webinar_ctx or await _load_webinar_context(db)
    by_number, by_broadcast, by_date, by_id = ctx["by_number"], ctx["by_broadcast"], ctx["by_date"], ctx["by_id"]

    # First-call appointments for these contacts.
    appt_rows = (await db.execute(sa_text(
        "SELECT appointment_id, ghl_contact_id, start_time, status, "
        "COALESCE(booked_at, start_time) AS effective_booked "
        "FROM ghl_appointment "
        "WHERE ghl_contact_id = ANY(:ids) AND calendar_class = :first AND NOT deleted"
    ), {"ids": ghl_contact_ids, "first": CLASS_FIRST})).mappings().all()
    if not appt_rows:
        return []
    appts_by_contact: dict[str, list[dict]] = {}
    for a in appt_rows:
        appts_by_contact.setdefault(a["ghl_contact_id"], []).append(dict(a))

    present_ids = list(appts_by_contact.keys())

    # Emails (GHL) → app contact + attendance identity.
    email_by_gcid = {
        r[0]: (r[1] or "").strip().lower()
        for r in (await db.execute(sa_text(
            "SELECT ghl_contact_id, email FROM ghl_contact WHERE ghl_contact_id = ANY(:ids)"
        ), {"ids": present_ids})).all()
        if r[1]
    }
    emails = list({e for e in email_by_gcid.values() if e})

    # Opportunity signals per contact: webinar_source_number, lead_quality, won.
    opp_by_gcid: dict[str, dict] = {}
    for r in (await db.execute(sa_text(
        "SELECT ghl_contact_id, webinar_source_number, lead_quality, pipeline_stage_id "
        "FROM ghl_opportunity WHERE ghl_contact_id = ANY(:ids)"
    ), {"ids": present_ids})).all():
        cur = opp_by_gcid.setdefault(r[0], {"wsn": None, "lead_quality": None, "won": False, "dq": False})
        if r[1] is not None and cur["wsn"] is None:
            cur["wsn"] = r[1]
        if r[2] and not cur["lead_quality"]:
            cur["lead_quality"] = r[2]
        if r[3] == DEAL_WON_STAGE_ID:
            cur["won"] = True
        if r[3] == DISQUALIFIED_STAGE_ID:
            cur["dq"] = True

    # App contact ids by email.
    contact_by_email: dict[str, str] = {}
    if emails:
        for r in (await db.execute(sa_text(
            "SELECT id, lower(email) FROM contacts WHERE user_id = :u AND lower(email) = ANY(:emails)"
        ), {"u": LLOYD_USER_ID, "emails": emails})).all():
            contact_by_email[r[1]] = r[0]

    # Memberships → member webinars per app contact.
    member_webs_by_cid: dict[str, list[dict]] = {}
    contact_ids = list(contact_by_email.values())
    if contact_ids:
        for r in (await db.execute(sa_text(
            "SELECT contact_id, webinar_id FROM webinar_contact_memberships WHERE contact_id = ANY(:ids)"
        ), {"ids": contact_ids})).all():
            w = by_id.get(r[1])
            if w:
                member_webs_by_cid.setdefault(r[0], []).append(w)

    # Attended webinars per email (watched live or any viewing).
    attended_by_email: dict[str, list[dict]] = {}
    if emails:
        for r in (await db.execute(sa_text(
            "SELECT lower(email), broadcast_id FROM webinargeek_subscribers "
            "WHERE lower(email) = ANY(:emails) AND (watched_live = TRUE OR COALESCE(minutes_viewing,0) > 0)"
        ), {"emails": emails})).all():
            w = by_broadcast.get(r[1])
            if w:
                attended_by_email.setdefault(r[0], []).append(w)

    now = datetime.now(timezone.utc)
    out_rows: list[dict] = []

    for gcid, appts in appts_by_contact.items():
        email = email_by_gcid.get(gcid, "")
        cid = contact_by_email.get(email)
        member_webs = member_webs_by_cid.get(cid, []) if cid else []
        attended_webs = attended_by_email.get(email, [])
        opp = opp_by_gcid.get(gcid, {"wsn": None, "lead_quality": None, "won": False, "dq": False})

        # Sort ascending by effective booking time; the latest call may trust the
        # (fresh) opp webinar_source_number.
        appts_sorted = sorted(appts, key=lambda a: a["effective_booked"] or datetime.max.replace(tzinfo=timezone.utc))
        latest = appts_sorted[-1]["appointment_id"] if appts_sorted else None

        origin: str | None = None
        contact_rows: list[dict] = []
        for a in appts_sorted:
            wid, src = attribute_booking(
                booked_at=a["effective_booked"],
                opp_webinar_number=opp["wsn"],
                trust_source_number=(a["appointment_id"] == latest),
                member_webinars=member_webs,
                attended_webinars=attended_webs,
                webinars_by_number=by_number,
                webinars_by_date=by_date,
                origin_webinar_id=origin,
            )
            if origin is None and wid is not None:
                origin = wid
            contact_rows.append({
                "appointment_id": a["appointment_id"],
                "ghl_contact_id": gcid,
                "contact_id": cid,
                "webinar_id": wid,
                "attribution_source": src,
                "booked_at": a["effective_booked"],
                "call_at": a["start_time"],
                "call_status": status_category(a["status"]),
                "lead_quality": None,
                "won": False,
                "disqualified": False,
                "locked_at": now if lock else None,
                "synced_at": now,
            })

        # Won / lead_quality are opp-level (one per contact) → credit the webinar of
        # the call that showed (earliest showed), else leave off.
        showed = next((r for r in contact_rows if r["call_status"] == "showed"), None)
        target = showed or (contact_rows[-1] if contact_rows else None)
        if target is not None:
            target["lead_quality"] = opp["lead_quality"]
            target["won"] = bool(opp["won"])
            target["disqualified"] = bool(opp["dq"])

        out_rows.extend(contact_rows)

    if out_rows and not dry_run:
        tbl = WebinarBookingAttribution
        # asyncpg caps a statement at 32,767 bind params; each row carries 13
        # columns, so chunk well under 32,767/13 ≈ 2,520 rows per statement (a
        # rescheduling contact contributes several first-call rows, so a big
        # contact-chunk can otherwise blow the cap).
        BATCH = 1500
        for i in range(0, len(out_rows), BATCH):
            stmt = pg_insert(tbl).values(out_rows[i:i + BATCH])
            frozen = tbl.locked_at.isnot(None)  # existing row was locked by capture-forward
            set_ = {
                "ghl_contact_id": stmt.excluded.ghl_contact_id,
                "contact_id": stmt.excluded.contact_id,
                # Attribution identity is frozen once locked — GHL overwrites the
                # opp's webinar_source_number, so a later sync must not let the
                # captured webinar drift. Unlocked rows (backfill) still refine.
                "webinar_id": case((frozen, tbl.webinar_id), else_=stmt.excluded.webinar_id),
                "attribution_source": case((frozen, tbl.attribution_source), else_=stmt.excluded.attribution_source),
                "booked_at": case((frozen, tbl.booked_at), else_=stmt.excluded.booked_at),
                # Outcomes legitimately evolve after booking (the call shows, the
                # deal closes/DQs, lead quality is set) — always refresh them, even
                # on a locked row, so wins aren't frozen at pre-call False.
                "call_at": stmt.excluded.call_at,
                "call_status": stmt.excluded.call_status,
                "lead_quality": stmt.excluded.lead_quality,
                "won": stmt.excluded.won,
                "disqualified": stmt.excluded.disqualified,
                # Preserve an existing lock; adopt the incoming one only if unset.
                "locked_at": func.coalesce(tbl.locked_at, stmt.excluded.locked_at),
                "synced_at": stmt.excluded.synced_at,
            }
            stmt = stmt.on_conflict_do_update(index_elements=["appointment_id"], set_=set_)
            await db.execute(stmt)

    return out_rows
