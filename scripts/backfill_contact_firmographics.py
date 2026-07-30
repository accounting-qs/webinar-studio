"""Fill-blanks backfill of contact firmographics from a normalized staging CSV.

Enriches EXISTING contacts (matched by lowercased email) with firmographic values
that were captured after those columns were added (migrations 058/059). It is a
strict fill-blanks backfill:

  * Only writes a column when the contact's value is currently NULL/empty AND the
    staging row has a value — never overwrites data a contact already has.
  * Touches ONLY the firmographic columns + custom_data. It never reads or writes
    bucket_id, outreach_status, is_blocklisted, assignment_id, upload_id, etc.
  * Skips staging emails with no matching contact (no inserts).
  * `first_name`/`last_name` are intentionally not written.

Also creates three app custom fields (Phone, LinkedIn, Company Name) in
contact_custom_fields and backfills their values into contacts.custom_data (same
fill-blanks rule), so they render in the app.

Batched by contact id (keyset), committing every chunk so each statement stays
well under the prod 120s statement_timeout.

Build the staging CSV first with scripts/build_firmo_staging.py.

Usage:
    python -m scripts.backfill_contact_firmographics --csv PATH [--dry-run]
                                                     [--chunk N] [--skip-index]
                                                     [--keep-staging]
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
STAGING_TABLE = "_firmo_staging"

# Staging CSV column order (must match scripts/build_firmo_staging.py OUT_COLUMNS).
STAGING_COLUMNS = [
    "email", "title", "seniority", "industry", "country",
    "employee_count", "employee_range",
    "company_annual_revenue", "company_total_funding", "company_founded_year",
    "company_country", "company_website",
    "phone", "linkedin", "company_name",
]

# Standard contact columns to fill: (contact_col, staging_col, is_int).
STD_COLS = [
    ("title", "title", False),
    ("seniority", "seniority", False),
    ("industry", "industry", False),
    ("country", "country", False),
    ("employee_count", "employee_count", True),
    ("employee_range", "employee_range", False),
    ("company_annual_revenue", "company_annual_revenue", False),
    ("company_total_funding", "company_total_funding", False),
    ("company_founded_year", "company_founded_year", False),
    ("company_country", "company_country", False),
    ("company_website", "company_website", False),
]

# App custom fields: (field_name in contact_custom_fields / custom_data key, staging_col).
# NB: company name uses the pre-existing lowercase 'company name' field/key so we
# don't recreate the 'Company Name' duplicate that had to be consolidated before.
CUSTOM_FIELDS = [
    ("Phone", "phone"),
    ("LinkedIn", "linkedin"),
    ("company name", "company_name"),
]


def _build_update_sql() -> str:
    """One fill-blanks UPDATE per batch, touching only firmographic cols + custom_data."""
    set_parts = []
    where_change = []
    for col, scol, is_int in STD_COLS:
        if is_int:
            set_parts.append(f"{col} = COALESCE(c.{col}, NULLIF(s.{scol}, '')::int)")
            where_change.append(f"(c.{col} IS NULL AND NULLIF(s.{scol}, '') IS NOT NULL)")
        else:
            set_parts.append(f"{col} = COALESCE(NULLIF(c.{col}, ''), NULLIF(s.{scol}, ''))")
            where_change.append(f"(NULLIF(c.{col}, '') IS NULL AND NULLIF(s.{scol}, '') IS NOT NULL)")

    # custom_data: append each key only when currently blank and staging has a value.
    cd_parts = ["COALESCE(c.custom_data, '{}'::jsonb)"]
    for key, scol in CUSTOM_FIELDS:
        cd_parts.append(
            f"(CASE WHEN COALESCE(c.custom_data->>'{key}', '') = '' "
            f"AND NULLIF(s.{scol}, '') IS NOT NULL "
            f"THEN jsonb_build_object('{key}', s.{scol}) ELSE '{{}}'::jsonb END)"
        )
        where_change.append(
            f"(COALESCE(c.custom_data->>'{key}', '') = '' AND NULLIF(s.{scol}, '') IS NOT NULL)"
        )
    set_parts.append("custom_data = " + "\n        || ".join(cd_parts))
    set_parts.append("updated_at = now()")

    return (
        f"UPDATE contacts c SET\n        " + ",\n        ".join(set_parts) + "\n"
        f"      FROM {STAGING_TABLE} s\n"
        f"      WHERE c.user_id = :uid AND c.id = ANY(:ids)\n"
        f"        AND c.email IS NOT NULL AND lower(c.email) = s.email\n"
        f"        AND (\n          " + "\n          OR ".join(where_change) + "\n        )"
    )


async def _create_email_index() -> None:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        print("Ensuring ix_contacts_lower_email (CONCURRENTLY, IF NOT EXISTS)…")
        await conn.exec_driver_sql(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_contacts_lower_email "
            "ON contacts (user_id, lower(email))"
        )
        print("  index ready.")


async def _load_staging(csv_path: str) -> int:
    cols_ddl = ",\n            ".join(f"{c} text" for c in STAGING_COLUMNS)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {STAGING_TABLE}")
        await conn.exec_driver_sql(
            f"CREATE TABLE {STAGING_TABLE} (\n            {cols_ddl}\n        )"
        )
        # asyncpg binary COPY straight from the CSV file.
        raw = await conn.get_raw_connection()
        apg = raw.driver_connection
        with open(csv_path, "rb") as f:
            await apg.copy_to_table(
                STAGING_TABLE, source=f, format="csv", header=True,
                columns=STAGING_COLUMNS,
            )
        await conn.exec_driver_sql(
            f"CREATE INDEX ix_{STAGING_TABLE}_email ON {STAGING_TABLE} (email)"
        )
        await conn.exec_driver_sql(f"ANALYZE {STAGING_TABLE}")
        n = (await conn.exec_driver_sql(
            f"SELECT count(*) FROM {STAGING_TABLE}"
        )).scalar()
    return n or 0


async def _preflight(staging_rows: int) -> int:
    """Verify columns exist and report scope. Returns the match count."""
    firmo_cols = [c for c, _, _ in STD_COLS] + ["list_location", "custom_data"]
    async with AsyncSessionLocal() as db:
        present = set((await db.execute(sa_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contacts' AND column_name = ANY(:cols)"
        ), {"cols": firmo_cols})).scalars().all())
        missing = [c for c in firmo_cols if c not in present]
        if missing:
            raise SystemExit(
                f"ABORT: contacts is missing columns {missing} — "
                f"prod has not run migrations 058/059. Apply them first."
            )

        total = (await db.execute(sa_text(
            "SELECT count(*) FROM contacts WHERE user_id = :uid"
        ), {"uid": LLOYD_USER_ID})).scalar()
        matched = (await db.execute(sa_text(
            f"SELECT count(*) FROM contacts c JOIN {STAGING_TABLE} s "
            f"ON c.email IS NOT NULL AND lower(c.email) = s.email "
            f"WHERE c.user_id = :uid"
        ), {"uid": LLOYD_USER_ID})).scalar()

    print("\n── Pre-flight ─────────────────────────────")
    print(f"  staging unique emails : {staging_rows:,}")
    print(f"  contacts (Lloyd)      : {total:,}")
    print(f"  emails matching a contact: {matched:,}"
          f"  ({(matched/staging_rows*100 if staging_rows else 0):.1f}% of staging,"
          f" {(matched/total*100 if total else 0):.1f}% of contacts)")
    print("───────────────────────────────────────────")
    return matched or 0


async def _ensure_custom_fields() -> None:
    async with AsyncSessionLocal() as db:
        existing = set((await db.execute(sa_text(
            "SELECT field_name FROM contact_custom_fields WHERE user_id = :uid"
        ), {"uid": LLOYD_USER_ID})).scalars().all())
        max_order = (await db.execute(sa_text(
            "SELECT COALESCE(max(display_order), 0) FROM contact_custom_fields "
            "WHERE user_id = :uid"
        ), {"uid": LLOYD_USER_ID})).scalar() or 0
        created = []
        for name, _ in CUSTOM_FIELDS:
            if name in existing:
                continue
            max_order += 1
            await db.execute(sa_text(
                "INSERT INTO contact_custom_fields (id, user_id, field_name, field_type, display_order) "
                "VALUES (gen_random_uuid(), :uid, :name, 'text', :ord)"
            ), {"uid": LLOYD_USER_ID, "name": name, "ord": max_order})
            created.append(name)
        await db.commit()
    print(f"Custom fields: created {created or '[]'} "
          f"(already present: {sorted(existing & {n for n, _ in CUSTOM_FIELDS})})")


async def _backfill(chunk: int) -> None:
    update_sql = _build_update_sql()
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    updated = 0
    while True:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(sa_text(
                "SELECT id FROM contacts WHERE user_id = :uid AND id > :last "
                "ORDER BY id LIMIT :lim"
            ), {"uid": LLOYD_USER_ID, "last": last_id, "lim": chunk})).scalars().all()
            if not ids:
                break
            n = (await db.execute(sa_text(update_sql),
                                  {"uid": LLOYD_USER_ID, "ids": ids})).rowcount or 0
            await db.commit()
            scanned += len(ids)
            updated += n
            last_id = ids[-1]
            if scanned % (chunk * 10) == 0:
                print(f"  scanned={scanned:,} updated={updated:,}")
    print(f"Backfill done. scanned={scanned:,} contacts, updated={updated:,}")


async def _drop_staging() -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {STAGING_TABLE}")
    print(f"Dropped {STAGING_TABLE}.")


async def main(args) -> None:
    print(f"Loading staging CSV: {args.csv}")
    staging_rows = await _load_staging(args.csv)
    print(f"  loaded {staging_rows:,} rows into {STAGING_TABLE}")

    matched = await _preflight(staging_rows)

    if args.dry_run:
        print("\nDRY-RUN: no contacts written. Dropping staging table.")
        await _drop_staging()
        await engine.dispose()
        return

    if matched == 0:
        print("\nNo matching contacts — nothing to backfill. Dropping staging.")
        await _drop_staging()
        await engine.dispose()
        return

    if not args.skip_index:
        await _create_email_index()
    await _ensure_custom_fields()
    print("\nBackfilling (fill-blanks, batched)…")
    await _backfill(args.chunk)

    if not args.keep_staging:
        await _drop_staging()
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="path to the normalized staging CSV")
    ap.add_argument("--dry-run", action="store_true",
                    help="load staging + report match counts, write nothing")
    ap.add_argument("--chunk", type=int, default=5000, help="contact ids per batch")
    ap.add_argument("--skip-index", action="store_true",
                    help="don't (re)create ix_contacts_lower_email")
    ap.add_argument("--keep-staging", action="store_true",
                    help="don't drop the _firmo_staging table at the end")
    asyncio.run(main(ap.parse_args()))
