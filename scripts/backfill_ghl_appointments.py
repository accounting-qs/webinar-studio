"""One-off backfill: fetch real calendar appointments for every opportunity-linked
contact and re-derive call1/call2 onto ghl_opportunity.

Corrects historical months that the unreliable custom fields under-counted
(e.g. 26 first-calls shown vs ~133 real) without a full contact/opp re-sync.
Reuses the exact sync-stage functions from services.ghl_sync so behavior matches
the live incremental/full sync.

Usage:
    python -m scripts.backfill_ghl_appointments [--limit N] [--no-recompute]

    --limit N       only process the first N opportunity-linked contacts (dry run)
    --no-recompute  skip the statistics snapshot recompute at the end
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.models._common import gen_uuid
from db.session import AsyncSessionLocal
from integrations.ghl_client import GHLClient
from services.ghl_sync import (
    _SyncState, _derive_calls_for_contacts, _fetch_and_store_appointments,
    _opp_contact_ids, _sync_calendars,
)


async def _report(label: str) -> None:
    async with AsyncSessionLocal() as db:
        opp = (await db.execute(sa_text("""
            SELECT
                COUNT(*)                                                                   AS opps,
                COUNT(*) FILTER (WHERE call1_source = 'calendar')                          AS from_calendar,
                COUNT(*) FILTER (WHERE call1_source = 'custom_field')                      AS from_custom_field,
                COUNT(*) FILTER (WHERE call1_source IS NULL)                               AS untagged,
                COUNT(call1_appointment_status) FILTER (WHERE TRIM(call1_appointment_status) != '') AS with_status
            FROM ghl_opportunity
        """))).mappings().one()
        appt = (await db.execute(sa_text("""
            SELECT COUNT(*) AS appts,
                   COUNT(*) FILTER (WHERE calendar_class = 'first')    AS firsts,
                   COUNT(*) FILTER (WHERE calendar_class = 'followup') AS followups,
                   COUNT(*) FILTER (WHERE calendar_class = 'exclude')  AS excluded
            FROM ghl_appointment
        """))).mappings().one()
    print(f"\n[{label}]")
    print(f"  opportunities: {dict(opp)}")
    print(f"  appointments:  {dict(appt)}")


async def main(limit: int | None, recompute: bool) -> None:
    client = await GHLClient.create()
    # Real UUID so _heartbeat's UPDATE is a silent no-op (0 rows) rather than a
    # noisy DataError — the backfill isn't tracked as a ghl_sync_run.
    state = _SyncState(run_id=gen_uuid(), started_at=datetime.now(timezone.utc))

    await _report("BEFORE")

    print("\nFetching + classifying calendars...")
    cal_map = await _sync_calendars(client)
    n_first = sum(1 for c, _ in cal_map.values() if c == "first")
    n_follow = sum(1 for c, _ in cal_map.values() if c == "followup")
    print(f"  {len(cal_map)} calendars: {n_first} first, {n_follow} followup, "
          f"{len(cal_map) - n_first - n_follow} excluded")

    contact_ids = await _opp_contact_ids(None)
    if limit is not None:
        contact_ids = contact_ids[:limit]
    print(f"\nBackfilling appointments for {len(contact_ids)} opportunity-linked contacts...")

    await _fetch_and_store_appointments(client, state, contact_ids, cal_map)
    print("  appointments stored. Deriving call1/call2 onto opportunities...")
    await _derive_calls_for_contacts(state, contact_ids)

    await _report("AFTER")

    if state.errors:
        print(f"\n{len(state.errors)} error(s) recorded (showing first 10):")
        for e in state.errors[:10]:
            print(f"  {e}")

    if recompute:
        print("\nInvalidating stats cache + scheduling recompute...")
        from services.statistics import invalidate_stats_cache
        from services.statistics_snapshot import schedule_recompute
        invalidate_stats_cache()
        schedule_recompute()

    print("\nDone.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--no-recompute", action="store_true")
    args = p.parse_args()
    asyncio.run(main(limit=args.limit, recompute=not args.no_recompute))
