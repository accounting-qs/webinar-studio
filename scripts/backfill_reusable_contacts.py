"""One-off (and re-runnable) backfill for the reusable-contacts membership model.

Run once after migration 064 to populate webinar_contact_memberships from the
current live contacts slot and recompute the three cache columns, and any time
you suspect drift — every step is idempotent and self-healing.

Three steps, all safe to re-run:
  1. Backfill memberships from the CURRENT live slot only — contacts with
     assignment_id set and outreach_status in ('assigned','used'). One membership
     per such contact, its webinar taken from the contact's assignment. Released
     contacts (slot already wiped to 'available') are correctly NOT resurrected,
     so they don't count toward times_invited. ON CONFLICT (webinar_id,contact_id)
     DO NOTHING makes re-runs cheap and safe.
  2. Recompute contacts.{assigned_membership_count, times_invited, last_invited_at}
     from the memberships — a full reconcile (sets 0/0/NULL where there are none).
  3. Create ix_contacts_claimable CONCURRENTLY (partial, over fresh/reusable rows)
     so the reuse-filter eligible-count and claim scans stay cheap on a big table.

All data steps are keyset-chunked by contact id with per-batch commits so each
statement stays well under the prod 120s statement_timeout.

Usage:
    python -m scripts.backfill_reusable_contacts [--chunk N] [--skip-index]

Verify after running:
    SELECT sum(times_invited) FROM contacts;                       -- == count(used)
    SELECT count(*) FROM contacts WHERE outreach_status='used';
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from config import settings
from db.session import AsyncSessionLocal, engine

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


async def _backfill_memberships(chunk: int) -> None:
    """Insert one membership per contact that currently occupies the live slot."""
    last_id = _ZERO_UUID
    scanned = 0
    inserted = 0
    while True:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts "
                "WHERE id > :last AND assignment_id IS NOT NULL "
                "AND outreach_status IN ('assigned','used') "
                "ORDER BY id LIMIT :lim"
            ), {"last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break

            n = (await db.execute(sa_text(
                "INSERT INTO webinar_contact_memberships "
                "  (user_id, contact_id, webinar_id, assignment_id, bucket_id, status, assigned_date, used_at) "
                "SELECT c.user_id, c.id, wla.webinar_id, c.assignment_id, c.bucket_id, "
                "       c.outreach_status, c.assigned_date, c.used_at "
                "FROM contacts c "
                "JOIN webinar_list_assignments wla ON wla.id = c.assignment_id "
                "WHERE c.id = ANY(:ids) "
                "  AND c.assignment_id IS NOT NULL "
                "  AND c.outreach_status IN ('assigned','used') "
                "ON CONFLICT (webinar_id, contact_id) DO NOTHING"
            ), {"ids": ids})).rowcount or 0
            await db.commit()

            scanned += len(ids)
            inserted += n
            last_id = ids[-1]
    print(f"Step 1 memberships: scanned={scanned} inserted={inserted}")


async def _recompute_caches(chunk: int) -> None:
    """Full reconcile of the three cache columns from memberships (all contacts)."""
    last_id = _ZERO_UUID
    scanned = 0
    changed = 0
    while True:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts WHERE id > :last ORDER BY id LIMIT :lim"
            ), {"last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break

            n = (await db.execute(sa_text(
                "UPDATE contacts c SET "
                "  assigned_membership_count = COALESCE(agg.a, 0), "
                "  times_invited = COALESCE(agg.u, 0), "
                "  last_invited_at = agg.mx "
                "FROM unnest(CAST(:ids AS uuid[])) AS k(cid) "
                "LEFT JOIN LATERAL ( "
                "  SELECT count(*) FILTER (WHERE m.status='assigned') AS a, "
                "         count(*) FILTER (WHERE m.status='used') AS u, "
                "         max(m.used_at) FILTER (WHERE m.status='used') AS mx "
                "  FROM webinar_contact_memberships m WHERE m.contact_id = k.cid "
                ") agg ON true "
                "WHERE c.id = k.cid "
                "  AND ( c.assigned_membership_count IS DISTINCT FROM COALESCE(agg.a, 0) "
                "     OR c.times_invited IS DISTINCT FROM COALESCE(agg.u, 0) "
                "     OR c.last_invited_at IS DISTINCT FROM agg.mx )"
            ), {"ids": ids})).rowcount or 0
            await db.commit()

            scanned += len(ids)
            changed += n
            last_id = ids[-1]
    print(f"Step 2 caches: scanned={scanned} changed={changed}")


async def _create_index() -> None:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        print("Creating ix_contacts_claimable CONCURRENTLY (may take a while)…")
        await conn.exec_driver_sql(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_claimable "
            "ON contacts (bucket_id, last_invited_at) "
            "WHERE is_blocklisted = false AND assigned_membership_count = 0"
        )
        print("  index ready.")


async def main(chunk: int, skip_index: bool) -> None:
    # Surface the target host so an accidental prod run is visible before it writes.
    url = settings.DATABASE_URL
    host = url.split("@", 1)[1].split("/", 1)[0] if "@" in url else url
    print(f"Target DB host: {host}")
    await _backfill_memberships(chunk)
    await _recompute_caches(chunk)
    if not skip_index:
        await _create_index()
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=5000, help="ids per batch (default 5000)")
    ap.add_argument("--skip-index", action="store_true", help="don't (re)create ix_contacts_claimable")
    args = ap.parse_args()
    asyncio.run(main(args.chunk, args.skip_index))
