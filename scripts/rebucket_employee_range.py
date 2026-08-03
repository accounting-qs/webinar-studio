"""Canonicalize contacts.employee_range to the agreed company-size buckets.

Rewrites every contact's employee_range so it is always either a canonical bucket
(see _EMPLOYEE_BUCKETS in api/routers/outreach/uploads.py) or NULL — matching how
uploads now stores it. This both re-buckets contacts imported under an older
mapping and strips foreign/junk labels that range-source imports let through
(Apollo-style "5 - 50" ranges that span buckets, ZoomInfo export-date artifacts
like "4/20/2025", legacy labels).

Per contact, the new value is:
  * the headcount bucketed, when employee_count IS NOT NULL (count is authoritative);
  * otherwise the stored employee_range if it is already canonical;
  * otherwise NULL (shown as "(no size)" in the stats tab).

  * Idempotent: only writes rows whose value actually changes, so re-runs are
    no-ops and the reported "updated" count is real changes.
  * Touches ONLY employee_range (+ updated_at). Never reads/writes bucket_id,
    outreach_status, is_blocklisted, assignment_id, etc.

The mapping is a single SQL CASE per batch (built from _EMPLOYEE_BUCKETS so it
can't drift), keyset-batched by contact id and committed every chunk so each
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
# The only non-NULL strings allowed in employee_range.
_CANONICAL_LABELS = [lbl for _, lbl in _EMPLOYEE_BUCKETS] + [_OVERFLOW_LABEL]


def _canonical_case_sql() -> str:
    """SQL expression resolving each contact to a canonical bucket or NULL,
    mirroring _canonical_employee_range() in uploads.py: headcount wins, else a
    stored canonical label, else NULL. Built from _EMPLOYEE_BUCKETS so it can't
    drift."""
    whens = "\n              ".join(
        f"WHEN c.employee_count <= {ceiling} THEN '{label}'"
        for ceiling, label in _EMPLOYEE_BUCKETS
    )
    canon_in = ", ".join(f"'{lbl}'" for lbl in _CANONICAL_LABELS)
    return (
        "CASE\n"
        "          WHEN c.employee_count IS NOT NULL THEN\n"
        "            CASE\n"
        f"              {whens}\n"
        f"              ELSE '{_OVERFLOW_LABEL}'\n"
        "            END\n"
        f"          WHEN c.employee_range IN ({canon_in}) THEN c.employee_range\n"
        "          ELSE NULL\n"
        "        END"
    )


def _build_update_sql(scoped: bool) -> str:
    case_sql = _canonical_case_sql()
    user_clause = "c.user_id = :uid AND " if scoped else ""
    return (
        "UPDATE contacts c SET\n"
        f"        employee_range = {case_sql},\n"
        "        updated_at = now()\n"
        f"      WHERE {user_clause}c.id = ANY(:ids)\n"
        # only real changes — NULL-safe compare keeps re-runs no-ops.
        f"        AND c.employee_range IS DISTINCT FROM ({case_sql})"
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

        params: dict = {}
        uscope = ""
        if scoped:
            uscope = " AND user_id = :uid"
            params["uid"] = uid
        with_count = (await db.execute(sa_text(
            f"SELECT count(*) FROM contacts WHERE employee_count IS NOT NULL{uscope}"
        ), params)).scalar()
        canon_in = ", ".join(f"'{lbl}'" for lbl in _CANONICAL_LABELS)
        to_null = (await db.execute(sa_text(
            f"SELECT count(*) FROM contacts WHERE employee_count IS NULL "
            f"AND employee_range IS NOT NULL AND employee_range <> '' "
            f"AND employee_range NOT IN ({canon_in}){uscope}"
        ), params)).scalar()

    print("\n── Pre-flight ─────────────────────────────")
    print(f"  scope                     : {'user ' + uid if scoped else 'ALL users'}")
    print(f"  canonical buckets         : {_CANONICAL_LABELS}")
    print(f"  contacts w/ headcount     : {with_count:,}  (bucketed from count)")
    print(f"  junk ranges → NULL        : {to_null:,}  (non-canonical, no count)")
    print("───────────────────────────────────────────")
    return (with_count or 0) + (to_null or 0)


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
    where = "TRUE"
    params: dict = {}
    if scoped:
        where = "user_id = :uid"
        params["uid"] = uid
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text(
            f"SELECT COALESCE(NULLIF(employee_range,''),'(no size / NULL)') AS b, "
            f"count(*) AS n FROM contacts WHERE {where} GROUP BY 1 ORDER BY 2 DESC"
        ), params)).mappings().all()
    print("\n── employee_range distribution (all contacts in scope) ──")
    for r in rows:
        canon = r["b"] in _CANONICAL_LABELS or r["b"] == "(no size / NULL)"
        tag = "" if canon else "  <-- non-canonical"
        print(f"  {r['b']:>18} : {r['n']:>10,}{tag}")
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
        print("\nNothing to canonicalize in scope — already clean.")
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
