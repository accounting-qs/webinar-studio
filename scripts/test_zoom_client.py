"""Pure-logic checks for integrations/zoom_client — no DB or API.

Same shape as scripts/test_ghl_appointments.py: run it directly, non-zero exit
on the first failure.

    python scripts/test_zoom_client.py

Covers the parts that would fail silently rather than loudly: watch-time
merging (a 2x error feeds the 10/30-minute thresholds) and instance-UUID
encoding (wrong encoding returns a different instance, not an error).
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from integrations.zoom_client import (  # noqa: E402
    ZoomScopeError, duration_units_suspect, encode_uuid, merge_watch_seconds,
    parse_dt, parse_missing_scopes,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok  {name}")


def dt(h, m, s=0):
    return datetime(2026, 9, 22, h, m, s, tzinfo=timezone.utc)


print("merge_watch_seconds")
check("empty", merge_watch_seconds([]), 0)
check("single 30m", merge_watch_seconds([(dt(14, 0), dt(14, 30))]), 1800)

# The rejoin case: 10 min, away 5, then 20 more. Honest answer is 30 min of
# watching -- NOT 35 (max-leave minus min-join, crediting the gap).
check(
    "rejoin with gap excludes the gap",
    merge_watch_seconds([(dt(14, 0), dt(14, 10)), (dt(14, 15), dt(14, 35))]),
    1800,
)

# Two devices at once. Naive SUM(duration) would say 60 min; only 30 elapsed.
check(
    "concurrent devices are not double-counted",
    merge_watch_seconds([(dt(14, 0), dt(14, 30)), (dt(14, 0), dt(14, 30))]),
    1800,
)
check(
    "partial overlap coalesces",
    merge_watch_seconds([(dt(14, 0), dt(14, 20)), (dt(14, 10), dt(14, 40))]),
    2400,
)
check(
    "unsorted input is handled",
    merge_watch_seconds([(dt(14, 15), dt(14, 35)), (dt(14, 0), dt(14, 10))]),
    1800,
)
check("zero-length segment ignored", merge_watch_seconds([(dt(14, 0), dt(14, 0))]), 0)
check("reversed segment ignored", merge_watch_seconds([(dt(14, 30), dt(14, 0))]), 0)
check("None endpoints ignored", merge_watch_seconds([(dt(14, 0), None), (None, None)]), 0)

# The 10-minute threshold boundary, since it is a hardcoded stats cutoff.
check(
    "9m59s floors to 9 minutes",
    merge_watch_seconds([(dt(14, 0), dt(14, 9, 59))]) // 60,
    9,
)
check("10m00s is 10 minutes", merge_watch_seconds([(dt(14, 0), dt(14, 10))]) // 60, 10)

print("encode_uuid")
check("plain uuid encoded once", encode_uuid("abc123=="), "abc123%3D%3D")
check("leading slash double-encoded", encode_uuid("/abc123"), "%252Fabc123")
check("embedded // double-encoded", encode_uuid("ab//cd"), "ab%252F%252Fcd")
check("slash but not leading/double is single", encode_uuid("ab/cd"), "ab%2Fcd")

print("parse_dt")
check("zulu", parse_dt("2026-09-22T14:00:00Z"), dt(14, 0))
check("offset", parse_dt("2026-09-22T16:00:00+02:00"), dt(14, 0))
check("none", parse_dt(None), None)
check("empty", parse_dt(""), None)
check("garbage", parse_dt("not-a-date"), None)
naive = parse_dt("2026-09-22T14:00:00")
check("naive assumed utc", naive, dt(14, 0))

print("duration_units_suspect")
check("agreement is fine", duration_units_suspect(1800, 1800), False)
check("small drift is fine", duration_units_suspect(1800, 1750), False)
check("minutes-for-seconds caught", duration_units_suspect(30, 1800), True)
check("zero reported is not flagged", duration_units_suspect(0, 1800), False)

print("parse_missing_scopes (Zoom reports missing scopes as HTTP 400 / code 4711)")
# The exact body prod returned when the app lacked the scope for /users/me.
real = '{"code":4711,"message":"Invalid access token, does not contain scopes:[user:read:user:admin, user:read:user]."}'
check("real 4711 body", parse_missing_scopes(real), ["user:read:user:admin", "user:read:user"])
check("single scope", parse_missing_scopes('does not contain scopes:[webinar:read:admin].'), ["webinar:read:admin"])
check("no brackets -> empty", parse_missing_scopes("some other failure"), [])
check("empty body -> empty", parse_missing_scopes(""), [])
check("none -> empty", parse_missing_scopes(None), [])
check("spacing tolerated", parse_missing_scopes("does not contain scopes : [ a:b , c:d ]"), ["a:b", "c:d"])

# The message must name the scope, since that IS the fix the user has to apply.
e = ZoomScopeError("/users/me", real, parse_missing_scopes(real))
check("error names the scopes", "user:read:user:admin" in str(e), True)
check("error carries them structurally", e.missing_scopes[0], "user:read:user:admin")
check("scope-less fallback still readable", "missing a scope" in str(ZoomScopeError("/x")), True)

if failures:
    print("\nFAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nAll zoom_client logic checks passed.")
