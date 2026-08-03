"""Re-derive contacts.employee_range from employee_count using the finer canonical
buckets, overwriting existing range values, and clean up the ZoomInfo
'4/20/2025' export-date garbage that a bad mapping wrote into employee_range.

Per-row target:
  * employee_count present  -> bucket(employee_count)   (overwrite)
  * employee_range = '4/20/2025' and no count -> NULL   (garbage cleanup)
  * otherwise                -> unchanged

Only employee_range (+ updated_at) is touched — no operational columns. Batched
by contact id (keyset), committing every chunk to stay under the 120s timeout.

Usage:
    python -m scripts.rederive_employee_range [--chunk N] [--dry-run]
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

# Mirrors _EMPLOYEE_BUCKETS in api/routers/outreach/uploads.py (finer scheme).
# Upper bound inclusive.
_EMPLOYEE_BUCKETS = (
    (2, "0 - 2"),
    (5, "3 - 5"),
    (10, "6 - 10"),
    (20, "11 - 20"),
    (50, "21 - 50"),
    (100, "51 - 100"),
    (200, "101 - 200"),
    (500, "201 - 500"),
    (1000, "501 - 1000"),
    (2000, "1001 - 2000"),
    (5000, "2001 - 5000"),
    (10000, "5001 - 10000"),
)


def _bucket_case(col: str) -> str:
    whens = "\n            ".join(
        f"WHEN {col} <= {ceil} THEN '{label}'" for ceil, label in _EMPLOYEE_BUCKETS
    )
    return f"CASE\n            {whens}\n            ELSE '10000+'\n        END"


# The new employee_range value for a row.
_NEW_RANGE = (
    "CASE\n"
    "        WHEN c.employee_count IS NOT NULL THEN " + _bucket_case("c.employee_count") + "\n"
    "        WHEN c.employee_range = '4/20/2025' THEN NULL\n"
    "        ELSE c.employee_range\n"
    "      END"
)

_UPDATE = sa_text(
    f"""
    UPDATE contacts c SET
      employee_range = {_NEW_RANGE},
      updated_at = now()
    WHERE c.user_id = :uid AND c.id = ANY(:ids)
      AND c.employee_range IS DISTINCT FROM ({_NEW_RANGE})
    """
)


async def _dry_run() -> None:
    case = _bucket_case("employee_count")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(
            f"SELECT {case} AS bucket, count(*) FROM contacts "
            f"WHERE user_id = :uid AND employee_count IS NOT NULL GROUP BY 1 ORDER BY min(employee_count)"
        ), {"uid": LLOYD_USER_ID})).all()
        garbage = (await db.execute(sa_text(
            "SELECT count(*) FROM contacts WHERE user_id = :uid "
            "AND employee_range = '4/20/2025' AND employee_count IS NULL"
        ), {"uid": LLOYD_USER_ID})).scalar()
    print("Projected employee_range from employee_count (new buckets):")
    for b, c in rows:
        print(f"  {c:>10,}  {b}")
    print(f"Garbage '4/20/2025' rows to blank (no count): {garbage:,}")


async def _run(chunk: int) -> None:
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    changed = 0
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
            changed += n
            last_id = ids[-1]
            if scanned % (chunk * 20) == 0:
                print(f"  scanned={scanned:,} changed={changed:,}")
    print(f"Done. scanned={scanned:,} employee_range_changed={changed:,}")


async def main(args) -> None:
    if args.dry_run:
        await _dry_run()
    else:
        await _run(args.chunk)
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", type=int, default=15000)
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args()))
