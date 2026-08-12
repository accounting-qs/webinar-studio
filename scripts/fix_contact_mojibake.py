"""One-time repair of double-encoded UTF-8 (mojibake) in existing contacts.

Some source CSVs arrived with names/companies already mangled upstream — UTF-8
bytes mis-read as cp1252/latin-1, sometimes twice. The import stored them
verbatim, so the DB now holds strings like "ÃªÂ¹â‚¬Ã¬Â§â€žÃ­Ëœâ€¢" (the Korean
name "김진형"). The live import path now repairs this at write time
(api/routers/outreach/_helpers.fix_mojibake); this script cleans the rows that
were already imported.

Repairs only free-text human/company fields plus custom_data values, and only
strings carrying the 'Ã'/'Â' mojibake markers — clean rows are left untouched.
Uses the exact same fix_mojibake helper as the live import, so results match.

Keyset-paginated by contact id and committed per chunk, so each statement stays
well under the prod 120s statement_timeout.

Usage:
    python -m scripts.fix_contact_mojibake --dry-run          # report only
    python -m scripts.fix_contact_mojibake                    # apply
    python -m scripts.fix_contact_mojibake --chunk 2000 --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import bindparam, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB

from db.session import engine
from api.routers.outreach._helpers import LLOYD_USER_ID, fix_mojibake

# Free-text columns that can hold scraped people/company data. Deliberately
# excludes ids, email, website/urls, status enums, dates and numeric fields.
TEXT_COLS = [
    "first_name", "last_name", "title", "seniority",
    "industry", "sector", "country", "company_country",
    "primary_identity", "sub_identity",
    "segment_name", "bucket_name", "lead_list_name",
]

MARKERS = ("Ã", "Â")


def _has_markers(v) -> bool:
    return isinstance(v, str) and ("Ã" in v or "Â" in v)


def _repair_custom_data(cd) -> tuple[dict | None, bool]:
    """Return (new_dict, changed). Only string values are touched."""
    if not isinstance(cd, dict):
        return cd, False
    changed = False
    out = {}
    for k, v in cd.items():
        if _has_markers(v):
            fixed = fix_mojibake(v)
            if fixed != v:
                changed = True
            out[k] = fixed
        else:
            out[k] = v
    return (out if changed else cd), changed


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing")
    ap.add_argument("--chunk", type=int, default=50000, help="rows scanned per page (bounded so no statement hits the 120s timeout)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N pages (0 = all)")
    ap.add_argument(
        "--start-after",
        default="00000000-0000-0000-0000-000000000000",
        help="resume: only process contacts whose id is greater than this UUID "
        "(the already-clean prefix is skipped — safe because the repair is idempotent)",
    )
    args = ap.parse_args()

    select_cols = ", ".join(["id", "custom_data", *TEXT_COLS])
    # Marker filter pushed server-side: with 4.5M contacts we can't pull every
    # row to Python. Each page is a bounded keyset window (LIMIT :chunk over the
    # PK, fast + no timeout); the server returns only rows carrying the 'Ã'/'Â'
    # mojibake markers, PLUS the window's boundary row (max id) so we can always
    # advance the keyset even when a window has no bad rows.
    marker_conds = [
        f"({c} LIKE '%Ã%' OR {c} LIKE '%Â%')" for c in TEXT_COLS
    ]
    marker_conds.append("(custom_data::text LIKE '%Ã%' OR custom_data::text LIKE '%Â%')")
    marker_where = " OR ".join(marker_conds)
    page_sql = sa_text(
        f"WITH page AS (\n"
        f"    SELECT {select_cols} FROM contacts\n"
        f"    WHERE user_id = :uid AND id > (:last)::uuid\n"
        f"    ORDER BY id LIMIT :chunk\n"
        f")\n"
        f"SELECT * FROM page\n"
        f"WHERE id = (SELECT id FROM page ORDER BY id DESC LIMIT 1) OR {marker_where}"
    )

    # Per-row UPDATE touching only the repaired columns (+ custom_data), guarded
    # by user_id so it can never stray outside the tenant.
    set_clause = ", ".join(f"{c} = :{c}" for c in [*TEXT_COLS, "custom_data"])
    update_sql = sa_text(
        f"UPDATE contacts SET {set_clause} WHERE id = :id AND user_id = :uid"
    ).bindparams(bindparam("custom_data", type_=JSONB))

    last_id = args.start_after
    pages = 0
    candidates = 0
    fixed_rows = 0
    fixed_fields = 0
    samples: list[str] = []

    while True:
        async with engine.begin() as conn:
            rows = list(
                await conn.execute(page_sql, {"uid": LLOYD_USER_ID, "last": last_id, "chunk": args.chunk})
            )
            if not rows:
                break
            # rows = marker-matches + the window boundary row (max id); the outer
            # SELECT drops CTE ordering, so advance by the true max id.
            last_id = max(r.id for r in rows)
            candidates += len(rows)

            updates: list[dict] = []
            for r in rows:
                m = r._mapping
                changes: dict = {}
                for col in TEXT_COLS:
                    v = m[col]
                    if _has_markers(v):
                        fixed = fix_mojibake(v)
                        if fixed != v:
                            changes[col] = fixed
                new_cd, cd_changed = _repair_custom_data(m["custom_data"])
                if cd_changed:
                    changes["custom_data"] = new_cd

                if changes:
                    fixed_rows += 1
                    fixed_fields += len(changes)
                    if len(samples) < 15:
                        first_changed = next(iter(changes))
                        if first_changed != "custom_data":
                            samples.append(f"{m[first_changed]!r} -> {changes[first_changed]!r}")
                    # Build a full row payload (unchanged cols keep their value).
                    payload = {"id": m["id"], "uid": LLOYD_USER_ID, "custom_data": new_cd}
                    for col in TEXT_COLS:
                        payload[col] = changes.get(col, m[col])
                    updates.append(payload)

            if updates and not args.dry_run:
                await conn.execute(update_sql, updates)
            # dry-run: transaction commits nothing meaningful (only SELECTs ran).

        pages += 1
        print(
            f"[page {pages}] scanned≈{pages * args.chunk} up_to_id={last_id} "
            f"fixed_rows={fixed_rows} fixed_fields={fixed_fields}",
            flush=True,
        )
        if args.limit and pages >= args.limit:
            print(f"Stopping after --limit {args.limit} pages.")
            break

    print("\n=== Summary ===")
    print(f"mode:            {'DRY-RUN (no writes)' if args.dry_run else 'APPLIED'}")
    print(f"pages:           {pages}  (scanned≈{pages * args.chunk} rows)")
    print(f"candidate rows:  {candidates}  (marker-matches + page boundaries)")
    print(f"rows fixed:      {fixed_rows}")
    print(f"fields fixed:    {fixed_fields}")
    if samples:
        print("samples:")
        for s in samples:
            print("  ", s)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
