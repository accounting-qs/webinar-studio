"""Standalone tests for services.ghl_appointments (classify_calendar + derive_calls).

Pure logic — no DB or API. Run:  python scripts/test_ghl_appointments.py
Exits non-zero on the first failed assertion.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ghl_appointments import (  # noqa: E402
    CLASS_EXCLUDE, CLASS_FIRST, CLASS_FOLLOWUP,
    STATUS_CANCELLED, STATUS_CONFIRMED, STATUS_NOSHOW, STATUS_SHOWED,
    classify_calendar, derive_calls,
)


def dt(y, m, d, h=0):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def appt(cls, start, status="Confirmed", booked=None, deleted=False, calendar_id=None):
    return {
        "calendar_id": calendar_id, "calendar_class": cls, "start_time": start,
        "status": status, "booked_at": booked, "deleted": deleted,
    }


def test_classify():
    cases = [
        ("Business Evaluation", CLASS_FIRST, "webinar"),
        ("John's Business Evaluation Call", CLASS_FIRST, "webinar"),
        ("QuantumScale Product Demo", CLASS_FIRST, "outreach"),
        ("Referral Call", CLASS_FIRST, "referral"),
        ("Custom Demo", CLASS_FOLLOWUP, None),
        ("QuantumScale Custom Demo", CLASS_FOLLOWUP, None),  # followup wins over demo
        ("2nd Meeting", CLASS_FOLLOWUP, None),
        ("Follow Up Call", CLASS_FOLLOWUP, None),
        ("Follow-up", CLASS_FOLLOWUP, None),
        ("Enrollment Call into QuantumScaling", CLASS_FOLLOWUP, None),
        ("Tech Call", CLASS_EXCLUDE, None),
        ("Strategy Call", CLASS_EXCLUDE, None),
        ("Onboarding", CLASS_EXCLUDE, None),
        ("", CLASS_EXCLUDE, None),
        (None, CLASS_EXCLUDE, None),
    ]
    for name, exp_cls, exp_tag in cases:
        cls, tag = classify_calendar(name)
        assert cls == exp_cls, f"{name!r}: class {cls} != {exp_cls}"
        assert tag == exp_tag, f"{name!r}: tag {tag} != {exp_tag}"
    print(f"  classify_calendar: {len(cases)} cases OK")


def test_no_first_call_falls_back():
    r = derive_calls([appt(CLASS_FOLLOWUP, dt(2026, 1, 5), "Showed")])
    assert r["has_call1"] is False
    assert r["call1_source"] == "custom_field"
    print("  no first call -> custom_field fallback OK")


def test_showed_wins_status_and_uses_showed_date():
    # A no-show attempt then a later showed attempt: status=Showed, date=showed date.
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 1, 3), "No Show", booked=dt(2026, 1, 1), calendar_id="cal_A"),
        appt(CLASS_FIRST, dt(2026, 1, 10), "Showed", booked=dt(2026, 1, 8), calendar_id="cal_B"),
    ])
    assert r["has_call1"] is True
    assert r["call1_source"] == "calendar"
    assert r["call1_appointment_status"] == STATUS_SHOWED
    assert r["call1_appointment_date"] == dt(2026, 1, 10)
    assert r["call1_booking_date"] == dt(2026, 1, 1)  # earliest booking
    assert r["call1_calendar_id"] == "cal_B"  # showed attempt's calendar
    print("  showed wins status + showed date + earliest booking + calendar OK")


def test_call1_calendar_uses_latest_when_not_showed():
    # No show: chosen attempt = latest, so its calendar wins.
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 2, 1), "Confirmed", calendar_id="cal_early"),
        appt(CLASS_FIRST, dt(2026, 2, 14), "Confirmed", calendar_id="cal_late"),
    ])
    assert r["call1_appointment_date"] == dt(2026, 2, 14)
    assert r["call1_calendar_id"] == "cal_late"
    print("  non-showed call1 calendar = latest attempt OK")


def test_reschedule_upcoming_uses_latest_date():
    # Two confirmed (upcoming) attempts, rescheduled forward: use LATEST start.
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 2, 1), "Confirmed", booked=dt(2026, 1, 20)),
        appt(CLASS_FIRST, dt(2026, 2, 14), "Confirmed", booked=dt(2026, 1, 25)),
    ])
    assert r["call1_appointment_status"] == STATUS_CONFIRMED
    assert r["call1_appointment_date"] == dt(2026, 2, 14)  # latest attempt
    assert r["call1_booking_date"] == dt(2026, 1, 20)      # earliest booking
    print("  upcoming reschedule -> latest attempt date OK")


def test_noshow_precedence_over_cancelled():
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 1, 3), "Cancelled"),
        appt(CLASS_FIRST, dt(2026, 1, 5), "No Show"),
    ])
    assert r["call1_appointment_status"] == STATUS_NOSHOW
    print("  no-show precedence over cancelled OK")


def test_cancelled_only():
    r = derive_calls([appt(CLASS_FIRST, dt(2026, 1, 3), "Cancelled")])
    assert r["call1_appointment_status"] == STATUS_CANCELLED
    print("  cancelled-only OK")


def test_call2_at_or_after_call1():
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 1, 5), "Showed", booked=dt(2026, 1, 1)),
        appt(CLASS_FOLLOWUP, dt(2026, 1, 2), "Showed"),   # before call1 -> not preferred
        appt(CLASS_FOLLOWUP, dt(2026, 1, 12), "Confirmed"),  # at/after call1 -> chosen
    ])
    assert r["call2_appointment_date"] == dt(2026, 1, 12)
    assert r["call2_appointment_status"] == STATUS_CONFIRMED
    print("  call2 earliest at/after call1 OK")


def test_call2_falls_back_to_earliest_when_all_before():
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 1, 20), "Showed", booked=dt(2026, 1, 1)),
        appt(CLASS_FOLLOWUP, dt(2026, 1, 5), "Showed"),
        appt(CLASS_FOLLOWUP, dt(2026, 1, 8), "Showed"),
    ])
    assert r["call2_appointment_date"] == dt(2026, 1, 5)  # earliest followup
    print("  call2 falls back to earliest followup OK")


def test_deleted_ignored():
    r = derive_calls([
        appt(CLASS_FIRST, dt(2026, 1, 3), "Showed", deleted=True),
        appt(CLASS_FIRST, dt(2026, 1, 5), "No Show"),
    ])
    assert r["call1_appointment_status"] == STATUS_NOSHOW
    assert r["call1_appointment_date"] == dt(2026, 1, 5)
    print("  deleted appointments ignored OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"Running {len(tests)} test groups...")
    for t in tests:
        t()
    print("ALL PASSED")
