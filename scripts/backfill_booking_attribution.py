"""Best-effort backfill of webinar_booking_attribution for EXISTING bookings.

The GHL sync captures each new booking's webinar going forward (locked). This
script populates the past: for every first-call ghl_appointment, it runs the same
attribution cascade (services.booking_attribution.rebuild_attributions with
lock=False) so historical per-webinar sales history is recovered as accurately as
the data allows. Idempotent and never clobbers a capture-forward-locked row.

Chunked by GHL contact id so each commit stays well under the 120s statement
timeout. `--dry-run` computes without writing and reports the attribution-source
mix (so you can sanity-check how much resolves via source_number/attended/etc.).

Usage:
    python -m scripts.backfill_booking_attribution --dry-run
    python -m scripts.backfill_booking_attribution
    python -m scripts.backfill_booking_attribution --chunk 500
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.session import AsyncSessionLocal, engine
from services.booking_attribution import rebuild_attributions, _load_webinar_context


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute + report, no writes")
    ap.add_argument("--chunk", type=int, default=1000, help="GHL contacts per batch")
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        contact_ids = [
            r[0] for r in (await db.execute(sa_text(
                "SELECT DISTINCT ghl_contact_id FROM ghl_appointment "
                "WHERE ghl_contact_id IS NOT NULL AND calendar_class = 'first' AND NOT deleted"
            ))).all()
        ]
    print(f"{len(contact_ids)} contacts with first-call appointments")

    source_mix: Counter = Counter()
    no_webinar = 0
    total = 0
    for i in range(0, len(contact_ids), args.chunk):
        chunk = contact_ids[i:i + args.chunk]
        async with AsyncSessionLocal() as db:
            ctx = await _load_webinar_context(db)
            rows = await rebuild_attributions(db, chunk, lock=False, webinar_ctx=ctx, dry_run=args.dry_run)
            if not args.dry_run:
                await db.commit()
        for r in rows:
            source_mix[r["attribution_source"]] += 1
            if r["webinar_id"] is None:
                no_webinar += 1
        total += len(rows)
        print(f"[{i + len(chunk)}/{len(contact_ids)}] bookings so far: {total}", flush=True)

    print("\n=== Summary ===")
    print(f"mode:              {'DRY-RUN (no writes)' if args.dry_run else 'APPLIED'}")
    print(f"first-call bookings: {total}")
    print(f"unattributed (no webinar): {no_webinar}")
    print("attribution source mix:")
    for src, n in source_mix.most_common():
        print(f"  {src:20s} {n}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
