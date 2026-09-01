"""Resolve the mailbox provider (via MX) for every email domain in a webinar's
audience, into the email_domain_provider cache.

    python scripts/resolve_email_providers.py --webinars 1        # newest only
    python scripts/resolve_email_providers.py --webinars 3        # newest three
    python scripts/resolve_email_providers.py --webinar-id <uuid>
    python scripts/resolve_email_providers.py --webinars 3 --retry-failed

Resumable: domains already cached are skipped, so an interrupted run is
restarted by re-issuing the same command. --retry-failed additionally re-tries
rows that previously timed out or errored (NXDOMAIN / no-MX are settled
answers and are never re-tried).

The audience spans every cohort the statistics rows use — planned lists, the
non-joiner pool and the GHL contacts carrying this webinar's invite signal —
so the resulting breakdown covers all four scopes without a second pass.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text as sa_text  # noqa: E402

from db.session import AsyncSessionLocal  # noqa: E402
from services.email_providers import RETRYABLE_STATUSES, resolve_domains  # noqa: E402


async def _target_webinars(count: int, webinar_id: str | None) -> list[str]:
    async with AsyncSessionLocal() as db:
        if webinar_id:
            return [webinar_id]
        rows = (await db.execute(sa_text(
            "SELECT id::text FROM webinars "
            "WHERE date IS NOT NULL AND date < CURRENT_DATE "
            "ORDER BY date DESC LIMIT :n"
        ).bindparams(n=count))).all()
    return [r[0] for r in rows]


async def _audience_domains(wids: list[str]) -> set[str]:
    """Every email domain attributable to these webinars.

    Planned membership is the bulk. GHL contacts carrying the webinar's invite
    signal are added so NO LIST DATA and non-joiners — who are not on any
    planned list — still resolve to a provider.
    """
    out: set[str] = set()
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        rows = (await db.execute(sa_text("""
            SELECT DISTINCT SPLIT_PART(LOWER(c.email), '@', 2) AS dom
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            WHERE m.webinar_id = ANY(CAST(:wids AS uuid[]))
              AND c.email IS NOT NULL AND c.email <> ''
        """).bindparams(wids=wids))).all()
        out.update(r[0] for r in rows if r[0])

        nums = (await db.execute(sa_text(
            "SELECT number FROM webinars WHERE id = ANY(CAST(:wids AS uuid[]))"
        ).bindparams(wids=wids))).all()
        for (n,) in nums:
            if n is None:
                continue
            # \y is a word boundary — "e14" must not match "e140".
            series_re = rf"\ye{int(n)}\y"
            rows = (await db.execute(sa_text("""
                SELECT DISTINCT SPLIT_PART(LOWER(g.email), '@', 2) AS dom
                FROM ghl_contact g
                WHERE g.email IS NOT NULL AND g.email <> ''
                  AND (g.calendar_webinar_series_history ~* :re
                       OR g.calendar_invite_response_history ~* :re)
            """).bindparams(re=series_re))).all()
            out.update(r[0] for r in rows if r[0])
    out.discard("")
    return out


async def _clear_retryable(domains: set[str]) -> int:
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text(
            "DELETE FROM email_domain_provider "
            "WHERE status = ANY(CAST(:st AS text[])) AND domain = ANY(CAST(:d AS text[]))"
        ).bindparams(st=list(RETRYABLE_STATUSES), d=sorted(domains)))
        await db.commit()
        return r.rowcount or 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webinars", type=int, default=1,
                    help="how many of the most recent passed webinars to cover")
    ap.add_argument("--webinar-id", type=str, default=None)
    ap.add_argument("--concurrency", type=int, default=150)
    ap.add_argument("--retry-failed", action="store_true",
                    help="also re-try domains whose last attempt timed out or errored")
    args = ap.parse_args()

    wids = await _target_webinars(args.webinars, args.webinar_id)
    if not wids:
        print("no target webinars"); return
    print(f"webinars: {len(wids)} -> {', '.join(wids)}", flush=True)

    t0 = time.monotonic()
    domains = await _audience_domains(wids)
    print(f"distinct domains in audience: {len(domains):,}  "
          f"({time.monotonic() - t0:.0f}s to collect)", flush=True)

    if args.retry_failed:
        cleared = await _clear_retryable(domains)
        print(f"cleared {cleared:,} previously-failed rows for retry", flush=True)

    t1 = time.monotonic()

    def progress(done: int, total: int, counts: dict[str, int]) -> None:
        el = time.monotonic() - t1
        rate = done / el if el > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  {done:,}/{total:,}  {rate:.0f}/s  eta {eta/60:.1f}m  "
              f"{ {k: v for k, v in counts.items() if k != 'cached'} }", flush=True)

    counts = await resolve_domains(domains, concurrency=args.concurrency,
                                   on_progress=progress)
    print(f"\ndone in {(time.monotonic() - t1)/60:.1f}m: {counts}", flush=True)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT provider, COUNT(*) AS n FROM email_domain_provider
            GROUP BY 1 ORDER BY n DESC LIMIT 20
        """))).all()
    print("\ncached domains by provider:")
    for p, n in rows:
        print(f"  {p:26} {n:>8,}")
    print("RESOLVE-DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
