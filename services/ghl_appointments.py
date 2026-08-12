"""Calendar-appointment classification + per-opportunity call derivation.

Pure, unit-testable logic that turns a contact's GHL calendar appointments into
the 1st/2nd call date + status for their opportunity. This is the source of
truth that replaces the unreliable GHL opportunity custom fields.

Two entry points:
- classify_calendar(name)  -> (calendar_class, funnel_tag)
- derive_calls(appointments) -> dict  (call1/call2 fields, or the custom-field
  fallback sentinel when the contact has no first-call appointment)
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Optional

# Calendar classes
CLASS_FIRST = "first"
CLASS_FOLLOWUP = "followup"
CLASS_EXCLUDE = "exclude"

# Canonical status labels. Chosen so that LOWER(status) matches the existing
# metric predicates in services/statistics_metric_filters.py and
# services/ghl_statistics_source.py without any query change:
#   'showed' | 'no show' (in the noshow set) | 'cancelled' | 'confirmed'.
STATUS_SHOWED = "Showed"
STATUS_NOSHOW = "No Show"
STATUS_CANCELLED = "Cancelled"
STATUS_CONFIRMED = "Confirmed"

# Follow-up "Nth meeting" phrases (checked before first-call patterns).
_FOLLOWUP_MEETINGS = ("2nd meeting", "3rd meeting", "4th meeting", "5th meeting")


def classify_calendar(name: Optional[str]) -> tuple[str, Optional[str]]:
    """Classify a calendar by NAME (case-insensitive) into first/followup/exclude.

    Returns (calendar_class, funnel_tag). funnel_tag is only set for first-call
    calendars ('webinar' | 'outreach' | 'referral'), else None.

    Follow-up patterns are tested FIRST so "Custom Demo" (which contains "demo")
    classifies as a follow-up, not a first call.
    """
    n = (name or "").strip().lower()
    if not n:
        return CLASS_EXCLUDE, None

    # ── Follow-up (test first) ────────────────────────────────────────
    if (
        "follow up" in n
        or "follow-up" in n
        or any(m in n for m in _FOLLOWUP_MEETINGS)
        or "enrollment call into quantumscaling" in n
        or "custom demo" in n
    ):
        return CLASS_FOLLOWUP, None

    # ── First sales call ──────────────────────────────────────────────
    if "business evaluation" in n:
        return CLASS_FIRST, "webinar"
    if "quantumscale" in n and "demo" in n:
        return CLASS_FIRST, "outreach"
    if "referral call" in n:
        return CLASS_FIRST, "referral"

    # ── Everything else is post-sale / non-sales → excluded ───────────
    return CLASS_EXCLUDE, None


def status_category(status: Optional[str]) -> Optional[str]:
    """Map a raw GHL appointmentStatus to one of showed/noshow/cancelled/confirmed.

    Tolerant of casing and the several no-show spellings GHL emits.
    """
    s = re.sub(r"[\s_-]+", " ", (status or "").strip().lower())
    if not s:
        return None
    if s == "showed":
        return "showed"
    if s in ("no show", "noshow"):
        return "noshow"
    if s in ("cancelled", "canceled"):
        return "cancelled"
    if s in ("confirmed", "booked", "new"):
        return "confirmed"
    return None


_CATEGORY_TO_LABEL = {
    "showed": STATUS_SHOWED,
    "noshow": STATUS_NOSHOW,
    "cancelled": STATUS_CANCELLED,
    "confirmed": STATUS_CONFIRMED,
}

# Outcome precedence across all first-call attempts (handles reschedules).
_STATUS_PRECEDENCE = ("showed", "noshow", "cancelled", "confirmed")


def _start(appt: dict) -> datetime:
    return appt.get("start_time") or datetime.max


def derive_calls(appointments: Iterable[dict]) -> dict:
    """Derive call1/call2 date + status for one opportunity's contact.

    `appointments` is an iterable of normalized appointment dicts with keys:
      calendar_id (str|None), calendar_class, start_time (datetime|None),
      status (str|None), booked_at (datetime|None), deleted (bool).

    Returns a dict. When the contact HAS a first-call appointment:
      {
        has_call1: True, call1_source: 'calendar',
        call1_appointment_status, call1_appointment_date, call1_booking_date,
        call1_calendar_id, call2_appointment_date, call2_appointment_status,
      }
    When the contact has NO first-call appointment (custom-field fallback):
      { has_call1: False, call1_source: 'custom_field' }
    The caller leaves the existing call1_* custom-field values untouched in that
    case — never overwrite the copied field with NULL.
    """
    live = [a for a in appointments if not a.get("deleted")]
    firsts = sorted(
        (a for a in live if a.get("calendar_class") == CLASS_FIRST), key=_start
    )
    followups = sorted(
        (a for a in live if a.get("calendar_class") == CLASS_FOLLOWUP), key=_start
    )

    if not firsts:
        return {"has_call1": False, "call1_source": "custom_field"}

    categories = [status_category(a.get("status")) for a in firsts]

    # Outcome-aware status across ALL first-call attempts.
    outcome = next((c for c in _STATUS_PRECEDENCE if c in categories), "confirmed")
    call1_status = _CATEGORY_TO_LABEL[outcome]

    # Attribution: the showed attempt (earliest showed) if one showed; otherwise
    # the LATEST attempt (reschedules move forward). The chosen attempt drives
    # both the date and the booking calendar.
    if outcome == "showed":
        showed = [a for a, c in zip(firsts, categories) if c == "showed"]
        chosen = min(showed, key=_start)
    else:
        chosen = max(firsts, key=_start)
    call1_date = _start(chosen)
    # _start returns the datetime.max sentinel when an attempt lacks a
    # start_time — treat that as unknown.
    if call1_date == datetime.max:
        call1_date = None
    call1_calendar_id = chosen.get("calendar_id")

    booking_dates = [a.get("booked_at") for a in firsts if a.get("booked_at")]
    call1_booking_date = min(booking_dates) if booking_dates else None

    # call2 = earliest follow-up at/after call1_date, else earliest follow-up.
    call2 = None
    if followups:
        if call1_date is not None:
            call2 = next(
                (a for a in followups if (a.get("start_time") or datetime.max) >= call1_date),
                None,
            )
        if call2 is None:
            call2 = followups[0]

    call2_status = None
    if call2 is not None:
        cat = status_category(call2.get("status"))
        call2_status = _CATEGORY_TO_LABEL.get(cat) if cat else call2.get("status")

    return {
        "has_call1": True,
        "call1_source": "calendar",
        "call1_appointment_status": call1_status,
        "call1_appointment_date": call1_date,
        "call1_booking_date": call1_booking_date,
        "call1_calendar_id": call1_calendar_id,
        "call2_appointment_date": (call2.get("start_time") if call2 else None),
        "call2_appointment_status": call2_status,
    }


# ── Per-booking → webinar attribution ─────────────────────────────────────────
#
# GHL keeps one opportunity per contact and overwrites `webinar_source_number` on
# each new booking; appointments carry no webinar tag and calendars are shared. So
# to keep per-webinar sales history we resolve which webinar drove each booked call
# here — authoritatively at sync time (source number is fresh for the just-booked
# call), best-effort for the past. Pure/testable: callers pass normalized data.
#
# A `webinar` here is a dict: {webinar_id, number, variant_label, date (a date),
# broadcast_id}. All datetimes are compared by calendar date against booked_at.

ATTR_SOURCE_NUMBER = "source_number"
ATTR_ATTENDED = "attended"
ATTR_INVITED = "invited"
ATTR_DATE_WINDOW = "date_window"
ATTR_RESCHEDULE = "reschedule_inherit"
ATTR_UNKNOWN = "unknown"


def _most_recent_on_or_before(webinars: Iterable[dict], when: datetime) -> Optional[dict]:
    """The webinar with the latest date at/before `when` (booking date)."""
    d = when.date() if hasattr(when, "date") else when
    eligible = [w for w in webinars if w.get("date") is not None and w["date"] <= d]
    if not eligible:
        return None
    return max(eligible, key=lambda w: w["date"])


def _pick_variant(candidates: list[dict], member_ids: set) -> Optional[dict]:
    """Resolve a webinar-number match to a single webinar. Prefer a variant the
    contact is a member of; otherwise the earliest (stable) variant."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    member_hits = [w for w in candidates if w.get("webinar_id") in member_ids]
    pool = member_hits or candidates
    return min(pool, key=lambda w: (w.get("date") or _DATE_MAX, str(w.get("variant_label") or "")))


_DATE_MAX = datetime.max.date()


def attribute_booking(
    *,
    booked_at: Optional[datetime],
    opp_webinar_number: Optional[int],
    trust_source_number: bool,
    member_webinars: list[dict],
    attended_webinars: list[dict],
    webinars_by_number: dict,
    webinars_by_date: list[dict],
    origin_webinar_id: Optional[str],
) -> tuple[Optional[str], str]:
    """Attribute one first-call booking to a webinar. Returns (webinar_id | None,
    attribution_source). Priority cascade (see plan):

    1. source_number  — the opp's current webinar_source_number, but only when
       `trust_source_number` (this is the just-booked / latest call). A/B same
       number resolves to the variant the contact is a member of.
    2. attended       — the most recent webinar the contact ATTENDED on/before the
       booking date.
    3. invited        — the most recent webinar the contact was a MEMBER of on/before
       the booking date.
    4. reschedule     — no fresh engagement signal but the contact ALREADY has an
       originating booking → treat this as a reschedule/follow-up and inherit that
       webinar (so a re-book weeks later stays with the lead's original webinar
       instead of falling into a later webinar's date window).
    5. date_window    — first booking with no engagement signal → best-guess the
       webinar whose [date, next-webinar-date) contains the booking date.
    6. unknown        — nothing resolved (e.g. booked under a different email).
    """
    member_ids = {w.get("webinar_id") for w in member_webinars}

    # 1. Source number (authoritative only for the latest/just-booked call).
    if trust_source_number and opp_webinar_number is not None:
        w = _pick_variant(webinars_by_number.get(opp_webinar_number, []), member_ids)
        if w:
            return w["webinar_id"], ATTR_SOURCE_NUMBER

    if booked_at is not None:
        # 2. Attended-in-window.
        w = _most_recent_on_or_before(attended_webinars, booked_at)
        if w:
            return w["webinar_id"], ATTR_ATTENDED
        # 3. Invited-in-window.
        w = _most_recent_on_or_before(member_webinars, booked_at)
        if w:
            return w["webinar_id"], ATTR_INVITED

    # 4. Reschedule/follow-up: no fresh signal, but we already know this lead's
    #    origin webinar → keep it there rather than guess by date.
    if origin_webinar_id is not None:
        return origin_webinar_id, ATTR_RESCHEDULE

    if booked_at is not None:
        # 5. Date-window across ALL webinars (first one whose window contains booked_at).
        d = booked_at.date()
        dated = [w for w in webinars_by_date if w.get("date") is not None]
        for i, w in enumerate(dated):
            nxt = dated[i + 1]["date"] if i + 1 < len(dated) else _DATE_MAX
            if w["date"] <= d < nxt:
                return w["webinar_id"], ATTR_DATE_WINDOW

    # 6. Unattributable.
    return None, ATTR_UNKNOWN
