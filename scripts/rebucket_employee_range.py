"""Re-derive contacts.employee_range from the stored numeric employee_count.

When the canonical company-size bucket mapping changes (see _EMPLOYEE_BUCKETS in
api/routers/outreach/uploads.py), contacts imported under the old mapping keep
their old range label. This backfill re-buckets EXISTING contacts that carry a
raw numeric employee_count so the Employee count statistics tab reflects the new
buckets on historical data.

  * Scope: only rows WHERE employee_count IS NOT NULL — those are safely
    re-derivable from the headcount. Range-source-only contacts (no numeric
    count) keep whatever employee_range their source list provided.
  * Idempotent: only writes rows whose re-derived label differs from the stored
    one, so re-runs are no-ops and the reported "updated" count is real changes.
  * Touches ONLY employee_range (+ updated_at). Never reads/writes bucket_id,
    outreach_status, is_blocklisted, assignment_id, etc.

Bucketing is done as a single SQL CASE per batch (built from _EMPLOYEE_BUCKETS so
it can't drift), keyset-batched by contact id and committed every chunk so each
statement stays well under the prod 120s statement_timeout.

After this completes, rebuild the statistics snapshots (POST /statistics/recompute
or the in-app Recompute control) so precomputed employeeRows reflect the new
buckets.

Usage:
    python -m scripts.rebucket_employee_range [--dry-run] [--chunk N] [--all-users]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sa_text

from db.session import AsyncSessionLocal, engine

# Kept in sync with api/routers/outreach/uploads.py _EMPLOYEE_BUCKETS.
from api.routers.outreach.uploads import _EMPLOYEE_BUCKETS, _employee_bucket

LLOYD_USER_ID = "9baf8117-db65-4f30-87a5-a76cf4f23d82"

# Label the fallthrough (above the top ceiling) exactly as _employee_bucket does.
_OVERFLOW_LABEL = _employee_bucket(_EMPLOYEE_BUCKETS[-1][0] + 1)


def _bucket_case_sql() -> str:
    """A SQL CASE mapping employee_count -> range label, mirroring _EMPLOYEE_BUCKETS
    (inclusive upper bound, ascending ceilings, fallthrough overflow label)."""
    whens = "\n            ".join(
        f"WHEN c.employee_count <= {ceiling} THEN '{label}'"
        for ceiling, label in _EMPLOYEE_BUCKETS
    )
    return (
        "CASE\n"
        f"            {whens}\n"
        f"            ELSE '{_OVERFLOW_LABEL}'\n"
        "        END"
    )


def _build_update_sql(scoped: bool) -> str:
    case_sql = _bucket_case_sql()
    user_clause = "c.user_id = :uid AND " if scoped else ""
    return (
        "UPDATE contacts c SET\n"
        f"        employee_range = {case_sql},\n"
        "        updated_at = now()\n"
        f"      WHERE {user_clause}c.id = ANY(:ids)\n"
        "        AND c.employee_count IS NOT NULL\n"
        # only real changes — keeps re-runs no-ops and the count honest.
        f"        AND COALESCE(c.employee_range, '') IS DISTINCT FROM ({case_sql})"
    )


async def _preflight(scoped: bool, uid: str | None) -> int:
    """Confirm the target column exists and report how many rows are in scope."""
    async with AsyncSessionLocal() as db:
        present = set((await db.execute(sa_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'contacts' AND column_name = ANY(:cols)"
        ), {"cols": ["employee_count", "employee_range"]})).scalars().all())
        missing = [c for c in ("employee_count", "employee_range") if c not in present]
        if missing:
            raise SystemExit(f"ABORT: contacts is missing columns {missing}.")

        where = "employee_count IS NOT NULL"
        params: dict = {}
        if scoped:
            where += " AND user_id = :uid"
            params["uid"] = uid
        in_scope = (await db.execute(sa_text(
            f"SELECT count(*) FROM contacts WHERE {where}"
        ), params)).scalar()

    print("\n── Pre-flight ─────────────────────────────")
    print(f"  scope                 : {'user ' + uid if scoped else 'ALL users'}")
    print(f"  new buckets           : "
          f"{[lbl for _, lbl in _EMPLOYEE_BUCKETS] + [_OVERFLOW_LABEL]}")
    print(f"  contacts w/ headcount : {in_scope:,}")
    print("───────────────────────────────────────────")
    return in_scope or 0


async def _rebucket(chunk: int, scoped: bool, uid: str | None) -> None:
    update_sql = _build_update_sql(scoped)
    last_id = "00000000-0000-0000-0000-000000000000"
    scanned = 0
    updated = 0
    while True:
        async with AsyncSessionLocal() as db:
            sel = "SELECT id FROM contacts WHERE id > :last"
            sel_params: dict = {"last": last_id, "lim": chunk}
            if scoped:
                sel = "SELECT id FROM contacts WHERE user_id = :uid AND id > :last"
                sel_params["uid"] = uid
            sel += " ORDER BY id LIMIT :lim"
            ids = (await db.execute(sa_text(sel), sel_params)).scalars().all()
            if not ids:
                break
            up_params: dict = {"ids": ids}
            if scoped:
                up_params["uid"] = uid
            n = (await db.execute(sa_text(update_sql), up_params)).rowcount or 0
            await db.commit()
            scanned += len(ids)
            updated += n
            last_id = ids[-1]
            if scanned % (chunk * 10) == 0:
                print(f"  scanned={scanned:,} updated={updated:,}")
    print(f"Re-bucket done. scanned={scanned:,} contacts, updated={updated:,}")


async def _report_distribution(scoped: bool, uid: str | None) -> None:
    where = "employee_count IS NOT NULL"
    params: dict = {}
    if scoped:
        where += " AND user_id = :uid"
        params["uid"] = uid
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(
            f"SELECT COALESCE(employee_range, '(null)') AS b, count(*) AS n "
            f"FROM contacts WHERE {where} GROUP BY 1 ORDER BY 2 DESC"
        ), params)).mappings().all()
    print("\n── employee_range distribution (rows with a headcount) ──")
    for r in rows:
        print(f"  {r['b']:>16} : {r['n']:,}")
    print("─────────────────────────────────────────────────────────")


async def main(args) -> None:
    scoped = not args.all_users
    uid = None if args.all_users else LLOYD_USER_ID

    in_scope = await _preflight(scoped, uid)
    await _report_distribution(scoped, uid)

    if args.dry_run:
        print("\nDRY-RUN: no contacts written.")
        await engine.dispose()
        return
    if in_scope == 0:
        print("\nNo contacts with a headcount in scope — nothing to do.")
        await engine.dispose()
        return

    print("\nRe-bucketing (batched)…")
    await _rebucket(args.chunk, scoped, uid)
    await _report_distribution(scoped, uid)
    print("\nNext: trigger POST /statistics/recompute (or the in-app Recompute "
          "control) to rebuild snapshots with the new buckets.")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report scope + distribution, write nothing")
    ap.add_argument("--chunk", type=int, default=5000, help="contact ids per batch")
    ap.add_argument("--all-users", action="store_true",
                    help="re-bucket every user's contacts (default: Lloyd only)")
    asyncio.run(main(ap.parse_args()))
