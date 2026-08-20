"""Patch `segmentEmployeeRows` into the EXISTING statistics snapshots.

The Segments-tab drill-down (segment x company-size) now reads a precomputed
cross out of each webinar's snapshot payload instead of computing it live (which
cost ~50s per webinar per segment and timed out the request). A full recompute
writes it, but that takes 25-40 min PER webinar; this script computes only the
employee cells (~2 min per webinar) and jsonb_set's them into the snapshot that
is already there, so the drill-down works without re-running everything.

Idempotent — rerunning simply overwrites the key with fresh numbers. Webinars
with no snapshot yet are skipped (a recompute will build them with the key).
Also refreshes `employeeRows` from the same scan, since the split query returns
both and they must agree.

Run it AFTER deploying the code that writes the key: a recompute by an older
build upserts a full payload without it, silently undoing a patched snapshot.
`--only-missing` makes the top-up run cheap (it skips what already has the key).

Usage:
    python -m scripts.backfill_segment_employee_rows --dry-run
    python -m scripts.backfill_segment_employee_rows
    python -m scripts.backfill_segment_employee_rows --only-missing
    python -m scripts.backfill_segment_employee_rows --webinar <uuid> [--webinar ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text as sa_text

from db.models import StatisticsSnapshot, Webinar
from db.session import AsyncSessionLocal, engine
from services.ghl_statistics_source import GoHighLevelStatisticsSource

SOURCE = "ghl"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute + report, no writes")
    ap.add_argument(
        "--webinar", action="append", default=[],
        help="webinar UUID to (re)build; repeatable. Default: every snapshot.",
    )
    ap.add_argument(
        "--only-missing", action="store_true",
        help="skip snapshots that already carry segmentEmployeeRows (top-up run)",
    )
    args = ap.parse_args()

    async with AsyncSessionLocal() as db:
        q = select(StatisticsSnapshot.webinar_id, StatisticsSnapshot.webinar_number).where(
            StatisticsSnapshot.source == SOURCE
        )
        if args.webinar:
            q = q.where(StatisticsSnapshot.webinar_id.in_(args.webinar))
        if args.only_missing:
            q = q.where(~StatisticsSnapshot.payload.has_key("segmentEmployeeRows"))
        targets = [(wid, num) for wid, num in (await db.execute(q)).all()]
    targets.sort(key=lambda t: (t[1] or 0), reverse=True)
    print(f"{len(targets)} snapshot(s) to patch")

    src = GoHighLevelStatisticsSource()
    done = failed = 0
    for wid, num in targets:
        t0 = time.time()
        try:
            async with AsyncSessionLocal() as db:
                w = (await db.execute(select(Webinar).where(Webinar.id == wid))).scalar_one_or_none()
                if w is None:
                    print(f"  W{num}: webinar row gone — skipped")
                    continue
                # Perf pragmas mirror the snapshot builder's transaction.
                await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
                await db.execute(sa_text("SET LOCAL work_mem = '128MB'"))
                cells = await src._compute_per_employee_cells(db, w, split_by_bucket=True)

            employee_rows = [c for c in cells if "bucketId" not in c]
            segment_rows: dict[str, list[dict]] = {}
            for c in cells:
                seg = c.get("bucketId")
                if seg is not None:
                    segment_rows.setdefault(seg, []).append(
                        {"bucket": c["bucket"], "metrics": c["metrics"]}
                    )
            dt = time.time() - t0
            print(
                f"  W{num}: {len(employee_rows)} size band(s), "
                f"{len(segment_rows)} segment(s), {sum(len(v) for v in segment_rows.values())} cells "
                f"({dt:.0f}s)"
            )
            if args.dry_run:
                continue

            async with AsyncSessionLocal() as db:
                await db.execute(
                    sa_text(
                        "UPDATE statistics_snapshot SET payload = "
                        "jsonb_set(jsonb_set(payload, '{segmentEmployeeRows}', CAST(:seg AS jsonb), true), "
                        "'{employeeRows}', CAST(:emp AS jsonb), true) "
                        "WHERE source = :src AND webinar_id = :wid"
                    ),
                    {
                        "seg": json.dumps(segment_rows),
                        "emp": json.dumps(employee_rows),
                        "src": SOURCE,
                        "wid": wid,
                    },
                )
                await db.commit()
            done += 1
        except Exception as exc:
            failed += 1
            print(f"  W{num}: FAILED after {time.time() - t0:.0f}s: {exc}")

    print(f"done: {done} patched, {failed} failed")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
