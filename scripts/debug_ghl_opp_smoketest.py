"""End-to-end smoke test: fetch a small batch of opps from GHL, write them
   through _upsert_opps_batch, then verify the DB has populated values.

Usage:
    python -m scripts.debug_ghl_opp_smoketest [N]   # default N=50
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.session import AsyncSessionLocal
from integrations.ghl_client import GHLClient
from services.ghl_sync import _build_opp_row, _upsert_opps_batch


async def main(n: int = 50) -> None:
    client = await GHLClient.create()

    rows: list[dict] = []
    async for opp in client.stream_opportunities():
        rows.append(_build_opp_row(opp))
        if len(rows) >= n:
            break

    ids = [r["ghl_opportunity_id"] for r in rows]
    print(f"Built {len(rows)} opp rows. First 3 IDs: {ids[:3]}")

    async with AsyncSessionLocal() as db:
        # Baseline: how many of these IDs currently have populated status/date?
        r = await db.execute(sa_text("""
            SELECT
                COUNT(*) AS total,
                COUNT(call1_appointment_status) FILTER (WHERE TRIM(call1_appointment_status) != '') AS with_status,
                COUNT(call1_appointment_date) AS with_date,
                COUNT(webinar_source_number) AS with_wsn,
                COUNT(lead_quality) AS with_lq
            FROM ghl_opportunity
            WHERE ghl_opportunity_id = ANY(:ids)
        """).bindparams(ids=ids))
        before = r.mappings().one()
        print(f"BEFORE upsert: {dict(before)}")

        await _upsert_opps_batch(db, rows)
        await db.commit()

        r = await db.execute(sa_text("""
            SELECT
                COUNT(*) AS total,
                COUNT(call1_appointment_status) FILTER (WHERE TRIM(call1_appointment_status) != '') AS with_status,
                COUNT(call1_appointment_date) AS with_date,
                COUNT(webinar_source_number) AS with_wsn,
                COUNT(lead_quality) AS with_lq
            FROM ghl_opportunity
            WHERE ghl_opportunity_id = ANY(:ids)
        """).bindparams(ids=ids))
        after = r.mappings().one()
        print(f"AFTER  upsert: {dict(after)}")

        # Distribution of statuses we just wrote
        r = await db.execute(sa_text("""
            SELECT call1_appointment_status, COUNT(*) AS n
            FROM ghl_opportunity
            WHERE ghl_opportunity_id = ANY(:ids)
              AND call1_appointment_status IS NOT NULL
              AND TRIM(call1_appointment_status) != ''
            GROUP BY 1
            ORDER BY n DESC
        """).bindparams(ids=ids))
        print("\nStatus distribution in the smoke batch:")
        for row in r.mappings().all():
            print(f"  {row['call1_appointment_status']!r:<20} {row['n']}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    asyncio.run(main(n))
