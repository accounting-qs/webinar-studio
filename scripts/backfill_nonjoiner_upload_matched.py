"""Backfill matched_count / unmatched_count for Non-joiners calendar uploads.

Non-joiner CSV imports used to hard-return (0, 0) because the calendar matcher
only knows about `webinar_contact_memberships`, and the non-joiner group is
derived (services/nonjoiners.py) rather than materialised into memberships — so
every historical Non-joiners upload reads "Matched 0" in the Calendar Uploads
tab even though its rows landed fine.

The import worker now intersects the uploaded emails with the webinar's
non-joiner group instead. This backfills the same number for the uploads that
predate that change, reusing the worker's own `_nonjoiner_match_counts` so the
two can't drift.

The counts are inherently as-of-now: contacts leave the group permanently when
they book, convert or get blocklisted, so an older webinar's backfilled matched
count reads a little lower than it would have on upload day.

Usage:
    python -m scripts.backfill_nonjoiner_upload_matched [--dry-run] [--webinar N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from api.routers.calendar_uploads import _nonjoiner_match_counts
from db.session import AsyncSessionLocal, engine


async def main(dry_run: bool, only_webinar: int | None) -> None:
    params: dict = {}
    number_filter = ""
    if only_webinar is not None:
        number_filter = "AND w.number = :num"
        params["num"] = only_webinar

    async with AsyncSessionLocal() as db:
        uploads = (await db.execute(sa_text(f"""
            SELECT u.id, u.webinar_id, w.number, u.total_rows,
                   u.matched_count, u.unmatched_count
            FROM webinar_calendar_uploads u
            JOIN webinars w ON w.id = u.webinar_id
            WHERE u.kind = 'nonjoiner' AND u.status = 'complete' {number_filter}
            ORDER BY w.number
        """), params)).mappings().all()

    if not uploads:
        print("No completed Non-joiners uploads found.")
        await engine.dispose()
        return
    print(f"{len(uploads)} upload(s) to process\n")

    for u in uploads:
        matched, unmatched = await _nonjoiner_match_counts(str(u["id"]), str(u["webinar_id"]))
        print(f"W{u['number']}: rows={u['total_rows']} → matched={matched} "
              f"unmatched={unmatched} (was {u['matched_count']}/{u['unmatched_count']})")

        if not dry_run:
            async with AsyncSessionLocal() as db:
                await db.execute(sa_text(
                    "UPDATE webinar_calendar_uploads "
                    "SET matched_count = :m, unmatched_count = :u "
                    "WHERE id = CAST(:uid AS uuid)"
                ), {"m": matched, "u": unmatched, "uid": str(u["id"])})
                await db.commit()

    await engine.dispose()
    print("\nDry run — nothing written." if dry_run else "\nDone.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print the counts without writing")
    ap.add_argument("--webinar", type=int, default=None, help="only this webinar number")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run, args.webinar))
