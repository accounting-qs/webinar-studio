"""One-off (and re-runnable) backfill for contacts.is_blocklisted.

Run once after migration 060 to flag existing blocklisted contacts, and any
time you suspect drift — it's idempotent and self-healing (a full reconcile:
sets the flag where it should be set, clears it where it shouldn't).

Two steps, both safe to re-run:
  1. Create ix_contacts_lower_email CONCURRENTLY (outside a txn, no write lock)
     so the flag-maintenance UPDATEs and this backfill match contacts by email
     fast on a large table.
  2. Chunked reconcile keyed by contact id, committing every batch so each
     statement stays well under the 120s statement_timeout. Only rows whose
     flag actually differs are written, so re-runs after the first are cheap.

Usage:
    python -m scripts.backfill_contact_blocklist [--chunk N] [--skip-index]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.session import AsyncSessionLocal, engine

# Match blocklist emails (stored lowercased) against lower(email); a contact is
# blocklisted when a same-user blocklist row exists for its normalized email.
_SHOULD_BE_BLOCKLISTED = (
    "EXISTS (SELECT 1 FROM blocklist b "
    "WHERE b.user_id = c.user_id AND b.email = lower(c.email))"
)


async def _create_index() -> None:
    # CONCURRENTLY cannot run inside a transaction block → AUTOCOMMIT connection.
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        print("Creating ix_contacts_lower_email CONCURRENTLY (may take a while)…")
        await conn.exec_driver_sql(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_lower_email "
            "ON contacts (user_id, lower(email))"
        )
        print("  index ready.")


async def _reconcile(chunk: int) -> None:
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    flipped_on = 0
    flipped_off = 0
    while True:
        async with AsyncSessionLocal() as db:
            # Keyset page of contact ids so each UPDATE scans a bounded slice.
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts WHERE id > :last ORDER BY id LIMIT :lim"
            ), {"last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break

            on = (await db.execute(sa_text(
                f"UPDATE contacts c SET is_blocklisted = true "
                f"WHERE c.id = ANY(:ids) AND c.is_blocklisted = false "
                f"AND c.email IS NOT NULL AND {_SHOULD_BE_BLOCKLISTED}"
            ), {"ids": ids})).rowcount or 0
            off = (await db.execute(sa_text(
                f"UPDATE contacts c SET is_blocklisted = false "
                f"WHERE c.id = ANY(:ids) AND c.is_blocklisted = true "
                f"AND (c.email IS NULL OR NOT {_SHOULD_BE_BLOCKLISTED})"
            ), {"ids": ids})).rowcount or 0
            await db.commit()

            scanned += len(ids)
            flipped_on += on
            flipped_off += off
            last_id = ids[-1]
            if scanned % (chunk * 20) == 0:
                print(f"  scanned={scanned} set={flipped_on} cleared={flipped_off}")

    print(f"Done. scanned={scanned} set={flipped_on} cleared={flipped_off}")


async def main(chunk: int, skip_index: bool) -> None:
    if not skip_index:
        await _create_index()
    await _reconcile(chunk)
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=5000, help="ids per batch (default 5000)")
    ap.add_argument("--skip-index", action="store_true", help="don't (re)create the email index")
    args = ap.parse_args()
    asyncio.run(main(args.chunk, args.skip_index))
