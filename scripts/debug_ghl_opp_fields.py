"""Debug GHL opportunity custom fields.

Prints:
  1. All opportunity custom field DEFINITIONS for the location (id + name + dataType)
     so we can match them by name to the metrics the Statistics page needs.
  2. The raw JSON shape of one opportunity from /opportunities/search, focused on
     the customFields array — so we can see whether GHL uses `value`, `fieldValue`,
     `fieldValueString`, etc.
  3. The same opportunity fetched via /opportunities/{id} for comparison.

Usage:
    python -m scripts.debug_ghl_opp_fields
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from integrations.ghl_client import GHLClient, get_ghl_credentials


async def main() -> None:
    api_key, location_id, pipeline_id = await get_ghl_credentials()
    client = GHLClient(api_key=api_key, location_id=location_id, pipeline_id=pipeline_id)

    print(f"=== Location: {location_id} ===\n")

    # ---- 1. List all opportunity custom field definitions ----
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(
            f"{client._base_url}/locations/{location_id}/customFields",
            params={"model": "opportunity"},
            headers=client._headers,
        )
        r.raise_for_status()
        opp_fields = r.json().get("customFields", [])

    print(f"=== Opportunity custom field definitions ({len(opp_fields)} total) ===")
    print(f"{'ID':<26} {'DATA_TYPE':<18} NAME")
    print("-" * 100)
    for f in sorted(opp_fields, key=lambda x: (x.get("name") or "").lower()):
        fid = f.get("id", "")
        name = f.get("name", "")
        dtype = f.get("dataType", "")
        print(f"{fid:<26} {dtype:<18} {name}")
    print()

    # Highlight likely matches for the metrics we need
    targets = [
        ("Call 1 Appointment Status / Show / Confirmed", ["call", "appointment", "show", "status"]),
        ("Call 1 Appointment Date", ["call", "appointment", "date"]),
        ("Lead Quality", ["lead", "quality"]),
        ("Projected Deal Size", ["projected", "deal"]),
        ("Webinar Source Number", ["webinar", "source"]),
    ]
    print("=== Likely matches by keyword ===")
    for label, kws in targets:
        print(f"\n  [{label}]")
        for f in opp_fields:
            n = (f.get("name") or "").lower()
            if any(kw in n for kw in kws):
                print(f"    {f.get('id'):<26} {f.get('dataType','?'):<18} {f.get('name')}")
    print()

    # ---- 2. Fetch one opportunity from search and dump shape ----
    if not pipeline_id:
        print("No pipeline_id configured; skipping opportunity fetch.")
        return

    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(
            f"{client._base_url}/opportunities/search",
            params={"location_id": location_id, "pipeline_id": pipeline_id, "limit": 1},
            headers=client._headers,
        )
        r.raise_for_status()
        search_data = r.json()
    opps = search_data.get("opportunities", [])
    if not opps:
        print("No opportunities returned from search.")
        return

    opp = opps[0]
    opp_id = opp.get("id")
    print(f"=== Raw opportunity from /opportunities/search (id={opp_id}) ===")
    print("Top-level keys:", list(opp.keys()))
    print("\nFirst 5 customFields entries (raw):")
    for cf in (opp.get("customFields") or [])[:5]:
        print(" ", json.dumps(cf, default=str))
    print()

    # ---- 3. Same opp from /opportunities/{id} ----
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(
            f"{client._base_url}/opportunities/{opp_id}",
            headers=client._headers,
        )
        if r.status_code != 200:
            print(f"GET /opportunities/{opp_id} -> HTTP {r.status_code}: {r.text[:300]}")
            return
        single = r.json().get("opportunity", r.json())
    print(f"=== Same opp from /opportunities/{opp_id} ===")
    print("Top-level keys:", list(single.keys()))
    print("\nFirst 5 customFields entries (raw):")
    for cf in (single.get("customFields") or [])[:5]:
        print(" ", json.dumps(cf, default=str))
    print()

    # ---- 4. Look for any opp that actually has Call 1 Appt Status / Date set ----
    print("=== Scanning first 100 opps for any non-null call1 status/date ===")
    async with httpx.AsyncClient(timeout=30.0) as http:
        r = await http.get(
            f"{client._base_url}/opportunities/search",
            params={"location_id": location_id, "pipeline_id": pipeline_id, "limit": 100},
            headers=client._headers,
        )
        r.raise_for_status()
        opps100 = r.json().get("opportunities", [])
    keys_with_values: dict[str, int] = {}
    sample_with_value: dict | None = None
    for o in opps100:
        for cf in (o.get("customFields") or []):
            keys = [k for k in cf.keys() if k != "id"]
            for k in keys:
                v = cf.get(k)
                if v not in (None, "", [], {}):
                    keys_with_values[k] = keys_with_values.get(k, 0) + 1
                    if sample_with_value is None:
                        sample_with_value = {"opp_id": o.get("id"), "cf": cf}
    print(f"Scanned {len(opps100)} opps. Non-null value keys observed:")
    for k, c in sorted(keys_with_values.items(), key=lambda x: -x[1]):
        print(f"  {k}: {c}")
    if sample_with_value:
        print("\nSample populated customField entry:")
        print(json.dumps(sample_with_value, indent=2, default=str))
    else:
        print("\nNo populated customField values found in first 100 opps.")


if __name__ == "__main__":
    asyncio.run(main())
