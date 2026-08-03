"""One-off: consolidate the duplicate 'Company Name' custom field into the
pre-existing 'company name' field.

The firmographics backfill created a 'Company Name' custom field because its
exact-case existence check missed the already-present lowercase 'company name'.
This merges the values back (fill-blanks: existing 'company name' wins), removes
the 'Company Name' key from custom_data, and deletes the duplicate field def.

Batched by contact id (keyset), committing every chunk to stay under the prod
120s statement_timeout.

Usage:
    python -m scripts.fix_company_name_custom_field [--chunk N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.session import AsyncSessionLocal, engine

LLOYD_USER_ID = "9baf8117-db65-4f30-87a5-a76cf4f23d82"

# For each row carrying the duplicate 'Company Name' key: drop that key, and when
# the canonical 'company name' is blank/absent, fill it from the dropped value.
_UPDATE = sa_text(
    """
    UPDATE contacts c SET
      custom_data = (c.custom_data - 'Company Name')
        || (CASE WHEN COALESCE(c.custom_data->>'company name', '') = ''
                 THEN jsonb_build_object('company name', c.custom_data->>'Company Name')
                 ELSE '{}'::jsonb END),
      updated_at = now()
    WHERE c.user_id = :uid AND c.id = ANY(:ids)
      AND c.custom_data ? 'Company Name'
    """
)


async def main(chunk: int) -> None:
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    fixed = 0
    while True:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts WHERE user_id = :uid AND id > :last "
                "ORDER BY id LIMIT :lim"
            ), {"uid": LLOYD_USER_ID, "last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break
            n = (await db.execute(_UPDATE, {"uid": LLOYD_USER_ID, "ids": ids})).rowcount or 0
            await db.commit()
            scanned += len(ids)
            fixed += n
            last_id = ids[-1]
            if scanned % (chunk * 20) == 0:
                print(f"  scanned={scanned:,} fixed={fixed:,}")

    # Remove the duplicate field definition now that no custom_data uses its key.
    async with AsyncSessionLocal() as db:
        deleted = (await db.execute(sa_text(
            "DELETE FROM contact_custom_fields WHERE user_id = :uid AND field_name = 'Company Name'"
        ), {"uid": LLOYD_USER_ID})).rowcount or 0
        await db.commit()

    print(f"Done. scanned={scanned:,} rows_merged={fixed:,} field_defs_deleted={deleted}")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=10000)
    asyncio.run(main(ap.parse_args().chunk))
