"""Dry-run: fetch real opps from GHL and confirm the new parser populates
   call1_appointment_status / date / webinar_source_number / lead_quality.
   Does NOT write anything to the DB.

Usage:
    python -m scripts.debug_ghl_opp_dryrun
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from integrations.ghl_client import GHLClient
from services.ghl_sync import _build_opp_row


async def main() -> None:
    client = await GHLClient.create()
    count = 0
    populated = {
        "call1_appointment_status": 0,
        "call1_appointment_date": 0,
        "webinar_source_number": 0,
        "lead_quality": 0,
        "projected_deal_size_value": 0,
    }
    sample_rows = []
    async for opp in client.stream_opportunities():
        row = _build_opp_row(opp)
        for k in populated:
            if row.get(k) not in (None, ""):
                populated[k] += 1
        if len(sample_rows) < 5 and any(row.get(k) for k in populated):
            sample_rows.append({k: row.get(k) for k in ["ghl_opportunity_id", *populated]})
        count += 1
        if count >= 200:
            break

    print(f"Processed {count} opps from GHL")
    print()
    print("Populated counts (after fix):")
    for k, v in populated.items():
        print(f"  {k:<32} {v} / {count}")
    print()
    print("Sample populated rows:")
    for r in sample_rows:
        print(f"  {r}")


if __name__ == "__main__":
    asyncio.run(main())
