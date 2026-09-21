"""Pure-logic checks for services/zoom_sync — no DB or API.

    python scripts/test_zoom_sync.py

Focused on the invariants that fail SILENTLY if broken:
  - emails must be lowercased (a CHECK constraint aborts the run otherwise)
  - watched_live / minutes_viewing must never be NULL (the stats predicate
    `watched_live = TRUE OR minutes_viewing > 0` goes NULL and drops the row)
  - rejoins must not double-count
  - a missing participants report must not overwrite good attendance
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from services.zoom_sync import (  # noqa: E402
    _ATTENDANCE_COLS, _clean_email, _registrant_row, _overlay_attendance,
    _participant_only_row, aggregate_participants, build_rows, is_zoom,
    make_broadcast_id, parse_broadcast_id,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok  {name}")


def iso(h, m):
    return f"2026-09-22T{h:02d}:{m:02d}:00Z"


def dt(h, m):
    return datetime(2026, 9, 22, h, m, tzinfo=timezone.utc)


print("broadcast id round-trip")
check("non-recurring", make_broadcast_id("84512345678"), "zoom:84512345678")
check("occurrence", make_broadcast_id("84512345678", "1731628800000"),
      "zoom:84512345678:1731628800000")
check("parse non-recurring", parse_broadcast_id("zoom:84512345678"), ("84512345678", None))
check("parse occurrence", parse_broadcast_id("zoom:84512345678:1731628800000"),
      ("84512345678", "1731628800000"))
check("int id accepted", make_broadcast_id(84512345678), "zoom:84512345678")
# The prefix is what keeps Zoom rows invisible to WebinarGeek's queries.
check("zoom id detected", is_zoom("zoom:123"), True)
check("bare WG numeric not zoom", is_zoom("4815162342"), False)
check("none not zoom", is_zoom(None), False)

print("email normalisation (migration 069 CHECK)")
check("uppercase lowered", _clean_email("Foo.Bar@Example.COM"), "foo.bar@example.com")
check("whitespace stripped", _clean_email("  a@b.com  "), "a@b.com")
check("none -> empty", _clean_email(None), "")

print("registrant row: no NULLs in the attendance predicate columns")
row = _registrant_row("zoom:1", {
    "id": "abc", "email": "Person@Example.com", "first_name": "Ada",
    "last_name": "Lovelace", "org": "Analytical", "job_title": "Engineer",
    "create_time": iso(9, 0), "join_url": "https://zoom.us/w/1",
})
check("email lowercased", row["email"], "person@example.com")
check("watched_live is False not None", row["watched_live"], False)
check("minutes_viewing is 0 not None", row["minutes_viewing"], 0)
check("watched_replay is None (unknowable on Zoom)", row["watched_replay"], None)
check("no unsubscribe concept", row["unsubscribed_at"], None)
check("provider stamped", row["provider"], "zoom")
check("company from org", row["company"], "Analytical")
check("subscribed_at parsed", row["subscribed_at"], dt(9, 0))

row2 = _registrant_row("zoom:1", {
    "id": "x", "email": "q@e.com",
    "custom_questions": [{"title": "Company Name", "value": "Acme"}],
})
check("company falls back to custom question", row2["company"], "Acme")

print("aggregate_participants")
# Rejoin: 10 min, gap, 20 min -> 30 min, not 35.
by_email, emailless, mismatches = aggregate_participants([
    {"user_email": "A@Example.com", "name": "Ada L", "id": "p1",
     "join_time": iso(14, 0), "leave_time": iso(14, 10), "duration": 600},
    {"user_email": "a@example.com", "name": "Ada L", "id": "p1",
     "join_time": iso(14, 15), "leave_time": iso(14, 35), "duration": 1200},
])
check("rejoin grouped under one lowercased email", sorted(by_email), ["a@example.com"])
check("rejoin gap excluded", by_email["a@example.com"]["seconds"], 1800)
check("start is earliest join", by_email["a@example.com"]["start_time"], dt(14, 0))
check("end is latest leave", by_email["a@example.com"]["end_time"], dt(14, 35))
check("no unit mismatch flagged", mismatches, 0)

# Two devices at once: SUM would say 60 min, truth is 30.
by_email2, _, _ = aggregate_participants([
    {"user_email": "b@e.com", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800},
    {"user_email": "b@e.com", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800},
])
check("concurrent devices not double-counted", by_email2["b@e.com"]["seconds"], 1800)

# Emailless attendees are excluded per-person but still counted for the total.
by_email3, emailless3, _ = aggregate_participants([
    {"user_email": "", "participant_user_id": "u1", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800},
    {"user_email": None, "participant_user_id": "u2", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800},
    {"user_email": "c@e.com", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800},
])
check("emailless excluded from per-person rows", sorted(by_email3), ["c@e.com"])
check("emailless still counted for the webinar total", emailless3, 2)

# Seconds-vs-minutes regression would be a 60x error in every 10/30m metric.
_, _, mismatches4 = aggregate_participants([
    {"user_email": "d@e.com", "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 30},
])
check("unit mismatch flagged", mismatches4, 1)

check("empty report aggregates to nothing", aggregate_participants([])[0], {})

print("attendance overlay")
r = _registrant_row("zoom:1", {"id": "1", "email": "a@example.com"})
_overlay_attendance(r, by_email["a@example.com"])
check("overlay sets watched_live", r["watched_live"], True)
check("overlay floors minutes", r["minutes_viewing"], 30)
check("overlay keeps raw participants", len(r["raw"]["participants"]), 2)

# A registrant who never showed keeps explicit FALSE/0, so the stats predicate
# evaluates FALSE (excluded) rather than NULL (also excluded, but by accident).
noshow = _registrant_row("zoom:1", {"id": "2", "email": "n@e.com"})
check("no-show watched_live False", noshow["watched_live"], False)
check("no-show minutes 0", noshow["minutes_viewing"], 0)

po = _participant_only_row("zoom:1", "p@e.com", {"name": "Grace Hopper", "participant_id": "p9"})
check("participant-only has no registration time", po["subscribed_at"], None)
check("participant-only source tagged", po["registration_source"], "zoom_participant_only")
check("participant-only name split", (po["first_name"], po["last_name"]), ("Grace", "Hopper"))

print("build_rows: registrant <-> participant matching")

# The motivating case: registered with a work address, joined signed into Zoom
# with a personal one. Matching on email alone would produce TWO rows -- a
# phantom no-show plus an orphan attendee -- inflating registrations.
regs = [{"id": "R1", "email": "Work@corp.com", "first_name": "Ada", "last_name": "L"}]
parts = [{"user_email": "personal@gmail.com", "registrant_id": "R1", "name": "Ada L",
          "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800}]
agg, _, _ = aggregate_participants(parts)
merged = build_rows("zoom:1", regs, agg)
check("cross-email attendee does not create a second row", len(merged), 1)
check("attendance lands on the registration", merged["work@corp.com"]["watched_live"], True)
check("minutes land on the registration", merged["work@corp.com"]["minutes_viewing"], 30)
check("registration identity preserved", merged["work@corp.com"]["first_name"], "Ada")

# Same email on both sides: the ordinary path.
regs2 = [{"id": "R2", "email": "same@e.com"}]
agg2, _, _ = aggregate_participants([
    {"user_email": "same@e.com", "registrant_id": "R2",
     "join_time": iso(14, 0), "leave_time": iso(14, 20), "duration": 1200}])
m2 = build_rows("zoom:1", regs2, agg2)
check("same-email match is one row", len(m2), 1)
check("same-email attendance applied", m2["same@e.com"]["minutes_viewing"], 20)

# A genuine outsider (panelist) with no matching registrant still gets a row.
agg3, _, _ = aggregate_participants([
    {"user_email": "panelist@e.com", "join_time": iso(14, 0), "leave_time": iso(14, 30),
     "duration": 1800}])
m3 = build_rows("zoom:1", [], agg3)
check("unmatched attendee still recorded", len(m3), 1)
check("unmatched attendee flagged participant-only",
      m3["panelist@e.com"]["registration_source"], "zoom_participant_only")

# A registrant who never showed keeps a row with explicit FALSE/0.
m4 = build_rows("zoom:1", [{"id": "R4", "email": "noshow@e.com"}], {})
check("no-show registrant kept", len(m4), 1)
check("no-show has watched_live False", m4["noshow@e.com"]["watched_live"], False)

# An unknown registrant_id must not silently swallow the attendee.
agg5, _, _ = aggregate_participants([
    {"user_email": "ghost@e.com", "registrant_id": "NOPE",
     "join_time": iso(14, 0), "leave_time": iso(14, 30), "duration": 1800}])
m5 = build_rows("zoom:1", [{"id": "R5", "email": "real@e.com"}], agg5)
check("stale registrant_id falls back to its own row", sorted(m5), ["ghost@e.com", "real@e.com"])

print("report-unavailable guard")
# This mirrors the set_cols filter in _sync_one: when no report was retrieved,
# attendance columns must be left out of the UPDATE entirely.
report_seen = False
set_cols = {
    k: v for k, v in r.items()
    if k not in ("broadcast_id", "email") and not (not report_seen and k in _ATTENDANCE_COLS)
}
for col in ("watched_live", "minutes_viewing", "start_time", "end_time"):
    check(f"{col} excluded when report missing", col in set_cols, False)
check("identity columns still updated", "first_name" in set_cols, True)
check("broadcast_id never in SET", "broadcast_id" in set_cols, False)

report_seen = True
set_cols_ok = {
    k: v for k, v in r.items()
    if k not in ("broadcast_id", "email") and not (not report_seen and k in _ATTENDANCE_COLS)
}
check("watched_live written when report present", "watched_live" in set_cols_ok, True)

if failures:
    print("\nFAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nAll zoom_sync logic checks passed.")
