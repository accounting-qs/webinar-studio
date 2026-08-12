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
            # A single bulk chunk can legitimately exceed Supabase's default 120s
            # statement_timeout; lift it for this session so one heavy chunk can't
            # abort the whole (idempotent, resumable) backfill.
            await db.execute(sa_text("SET statement_timeout = 0"))
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
            # The per-contact LATERAL aggregate can push a chunk past Supabase's
            # 120s statement_timeout (this is what crashed the first prod run);
            # lift it for this session. Chunks stay idempotent + resumable.
            await db.execute(sa_text("SET statement_timeout = 0"))
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts WHERE id > :last ORDER BY id LIMIT :lim"
            ), {"last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break

            # last_invited_at = webinar date (midnight UTC) of the last used
            # membership; used_at is the fallback for rows missing assigned_date.
            # Mirrors recompute_contact_caches in api/routers/outreach/_helpers.py.
            n = (await db.execute(sa_text(
                "UPDATE contacts c SET "
                "  assigned_membership_count = COALESCE(agg.a, 0), "
                "  times_invited = COALESCE(agg.u, 0), "
                "  last_invited_at = agg.mx "
                "FROM unnest(CAST(:ids AS uuid[])) AS k(cid) "
                "LEFT JOIN LATERAL ( "
                "  SELECT count(*) FILTER (WHERE m.status='assigned') AS a, "
                "         count(*) FILTER (WHERE m.status='used') AS u, "
                "         max(COALESCE(m.assigned_date::timestamptz, m.used_at)) "
                "           FILTER (WHERE m.status='used') AS mx "
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


# (name, CREATE DDL) — both partial, built CONCURRENTLY. Names are hardcoded
# constants (safe to interpolate into the invalid-check / DROP statements).
_INDEXES: list[tuple[str, str]] = [
    (
        # NOT form: the claim/eligible queries emit `NOT is_blocklisted`, and
        # the planner only proves partial-index implication on an exact match.
        "ix_contacts_claimable",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_claimable "
        "ON contacts (bucket_id, last_invited_at) "
        "WHERE NOT is_blocklisted AND assigned_membership_count = 0",
    ),
    (
        # Serves GET /outreach/buckets/good-available, which runs on every
        # Planning load and groups fresh claimable contacts by location. Lead with
        # user_id (the query's equality filter) so the whole SELECT — user_id,
        # bucket_id, and the INCLUDEd group-by / emp-filter columns — comes from
        # the index and it can be an index-only scan (no heap-fetch of the ~fresh
        # population, which would risk the 120s cap). Without user_id in the index
        # the planner must hit the heap to check it, defeating index-only.
        "ix_contacts_good_avail",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_good_avail "
        "ON contacts (user_id, bucket_id) INCLUDE (country, list_location, employee_count) "
        "WHERE NOT is_blocklisted AND assigned_membership_count = 0 "
        "AND last_invited_at IS NULL",
    ),
    (
        # Serves /buckets/eligible's per-bucket TOTALS query when a country /
        # employee filter is active: it counts ALL non-blocklisted contacts (any
        # claim status), so the fresh-only partial indexes above can't cover it —
        # without this it heap-scans 4.5M rows and blows the 120s cap.
        # (Built manually on prod 2026-08-12; listed here for environment parity.)
        "ix_contacts_bucket_filters",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_bucket_filters "
        "ON contacts (user_id, bucket_id) INCLUDE (country, list_location, employee_count) "
        "WHERE NOT is_blocklisted",
    ),
]


async def _create_index() -> None:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        # Supabase caps statements at ~120s by default, which kills a large
        # CONCURRENTLY build; lift it for this DDL session (same as migration 017).
        await conn.exec_driver_sql("SET statement_timeout = 0")
        for name, ddl in _INDEXES:
            # A prior CONCURRENTLY build killed mid-flight leaves an INVALID index
            # of this name; a plain `IF NOT EXISTS` would then silently skip
            # forever. Drop the dead index first so this run actually rebuilds it.
            invalid = await conn.exec_driver_sql(
                "SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                f"WHERE c.relname = '{name}' AND NOT i.indisvalid"
            )
            if invalid.first():
                print(f"  found INVALID {name} from a prior failed build — dropping it")
                await conn.exec_driver_sql(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            print(f"Creating {name} CONCURRENTLY (may take a while)…")
            await conn.exec_driver_sql(ddl)
            print(f"  {name} ready.")


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
