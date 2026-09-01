"""Mailbox-provider x cohort breakdown of invite volume and Yes/Maybe response.

Answers "which mailbox providers actually respond to our calendar invites" —
invited volume, Yes count, Maybe count and the rates, per provider, at four
widening cohort scopes:

    assigned    the planned lists for the webinar
    nonjoiners  the non-joiner pool (registrants of the last N webinars who
                never joined any of them)
    newJoiners  assigned + NO LIST DATA, non-joiners excluded
    overall     everything, non-joiners included

`assigned` and `noListData` are queried; `newJoiners` and `overall` are summed
from them, so the four scopes cannot disagree with each other.

Provider labels come from the email_domain_provider MX cache — see
services/email_providers. A domain with no cache row is reported as
"Not resolved yet" rather than being dropped, so the invited volumes always
add up to the real audience even mid-backfill.

Computed live (not stored in the statistics snapshot) and memoised per webinar
for the process lifetime of a page session, so the Home aggregate and the
per-webinar report can share one implementation without a recompute.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Label used when the MX backfill has not reached a domain yet.
UNRESOLVED_LABEL = "Not resolved yet"

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 600.0
_LOCKS: dict[str, asyncio.Lock] = {}

# Rows kept out of the "assigned" cohort: the synthetic Nonjoiners / No List
# Data lists, and memberships whose list was deleted. Mirrors webinar_report._COLD.
_COLD = (
    "wla.id IS NOT NULL "
    "AND COALESCE(wla.is_nonjoiners, false) = false "
    "AND COALESCE(wla.is_no_list_data, false) = false"
)

_PROVIDER_EXPR = (
    f"COALESCE(p.provider, '{UNRESOLVED_LABEL}')"
)


def _blank() -> dict[str, int]:
    return {"invited": 0, "yes": 0, "maybe": 0}


def _add(dst: dict[str, dict[str, int]], provider: str, row: dict[str, int]) -> None:
    slot = dst.setdefault(provider, _blank())
    for k in ("invited", "yes", "maybe"):
        slot[k] += int(row.get(k) or 0)


async def _webinar_head(db, webinar_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text as sa_text
    r = await db.execute(sa_text(
        "SELECT id::text AS id, number, date::text AS date, variant_label, main_title "
        "FROM webinars WHERE id = CAST(:wid AS uuid)"
    ).bindparams(wid=webinar_id))
    row = r.mappings().one_or_none()
    return dict(row) if row else None


async def _one_webinar(webinar_id: str) -> dict[str, Any]:
    """Provider x cohort counts for a single webinar."""
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal
    from services.ghl_statistics_source import (
        _csv_mode_for_webinar, _invite_response_regex, _webinar_series_regex,
    )
    from services.nonjoiners import nonjoiner_pool_emails

    scopes: dict[str, dict[str, dict[str, int]]] = {
        "assigned": {}, "noListData": {}, "nonjoiners": {},
    }

    async with AsyncSessionLocal() as db:
        head = await _webinar_head(db, webinar_id)
        if head is None:
            raise ValueError(f"webinar {webinar_id} not found")
        n = int(head["number"] or 0)

        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
        await db.execute(sa_text("SET LOCAL work_mem = '256MB'"))

        # Yes/Maybe source: an uploaded Added-to-Calendar CSV when one exists,
        # otherwise GHL's response-history regex — the same precedence the
        # Statistics rows use, so the numbers tie out.
        csv_mode = await _csv_mode_for_webinar(db, webinar_id)
        params: dict[str, Any] = {"wid": webinar_id}

        # The responder set is tiny (hundreds), the membership set is huge
        # (hundreds of thousands). Materialising the responders ONCE and hash
        # joining them beats testing a predicate against ghl_contact for every
        # member — that join is the documented ~30s cost on this schema, and
        # here it would be paid per cohort.
        if csv_mode:
            resp_cte = """
                resp AS (
                    SELECT LOWER(email) AS email,
                           BOOL_OR(LOWER(calendar_invite_response) = 'yes')   AS is_yes,
                           BOOL_OR(LOWER(calendar_invite_response) = 'maybe') AS is_maybe
                    FROM webinar_calendar_invites
                    WHERE webinar_id = CAST(:wid AS uuid)
                      AND LOWER(calendar_invite_response) IN ('yes', 'maybe')
                    GROUP BY 1
                )
            """
        else:
            resp_cte = """
                resp AS (
                    SELECT LOWER(email) AS email,
                           BOOL_OR(calendar_invite_response_history ~* :yes_re)   AS is_yes,
                           BOOL_OR(calendar_invite_response_history ~* :maybe_re) AS is_maybe
                    FROM ghl_contact
                    WHERE email IS NOT NULL
                      AND calendar_invite_response_history ~* :any_re
                    GROUP BY 1
                )
            """
            params["yes_re"] = _invite_response_regex(n, "Yes")
            params["maybe_re"] = _invite_response_regex(n, "Maybe")
            params["any_re"] = rf"\ye{n}-(Yes|Maybe)\y"

        # ── assigned: the planned lists ──────────────────────────────
        rows = (await db.execute(sa_text(f"""
            WITH {resp_cte}
            SELECT {_PROVIDER_EXPR} AS provider,
                   COUNT(DISTINCT LOWER(c.email)) AS invited,
                   COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE r.is_yes) AS yes,
                   COUNT(DISTINCT LOWER(c.email)) FILTER (WHERE r.is_maybe) AS maybe
            FROM contacts c
            JOIN webinar_contact_memberships m ON m.contact_id = c.id
            LEFT JOIN webinar_list_assignments wla ON wla.id = m.assignment_id
            LEFT JOIN email_domain_provider p
                   ON p.domain = SPLIT_PART(LOWER(c.email), '@', 2)
            LEFT JOIN resp r ON r.email = LOWER(c.email)
            WHERE m.webinar_id = CAST(:wid AS uuid) AND {_COLD}
              AND c.email IS NOT NULL AND c.email <> ''
            GROUP BY 1
        """).bindparams(**params))).mappings().all()
        for r in rows:
            _add(scopes["assigned"], r["provider"], r)

    # ── nonjoiners: the pool, matched against this webinar's responses ──
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        nj = await nonjoiner_pool_emails(db, webinar_id)

    if nj:
        async with AsyncSessionLocal() as db:
            await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
            # Non-joiners get their own invite upload (webinar_nonjoiner_invites),
            # separate from the planned-list calendar CSV. When one exists it is
            # the authority for this cohort's Yes/Maybe; otherwise fall back to
            # GHL's response history. Matching is on the pool email, not on a
            # ghl_contact row, since a non-joiner need not have one.
            has_nj_csv = bool((await db.execute(sa_text(
                "SELECT 1 FROM webinar_nonjoiner_invites "
                "WHERE webinar_id = CAST(:wid AS uuid) LIMIT 1"
            ).bindparams(wid=webinar_id))).scalar())

            nj_params: dict[str, Any] = {"wid": webinar_id, "emails": nj}
            if has_nj_csv:
                nj_cte = """
                    WITH njcsv AS (
                        SELECT LOWER(email) AS email,
                               LOWER(calendar_invite_response) AS resp
                        FROM webinar_nonjoiner_invites
                        WHERE webinar_id = CAST(:wid AS uuid)
                    ),
                """
                nj_join = "LEFT JOIN njcsv ON njcsv.email = pool.email"
                nj_yes, nj_maybe = "njcsv.resp = 'yes'", "njcsv.resp = 'maybe'"
            else:
                nj_cte = "WITH "
                nj_join = "LEFT JOIN ghl_contact g ON LOWER(g.email) = pool.email"
                nj_yes = "g.calendar_invite_response_history ~* :yes_re"
                nj_maybe = "g.calendar_invite_response_history ~* :maybe_re"
                nj_params["yes_re"] = _invite_response_regex(n, "Yes")
                nj_params["maybe_re"] = _invite_response_regex(n, "Maybe")

            rows = (await db.execute(sa_text(f"""
                {nj_cte} pool AS (
                    SELECT DISTINCT e AS email FROM UNNEST(CAST(:emails AS text[])) AS e
                )
                SELECT {_PROVIDER_EXPR} AS provider,
                       COUNT(DISTINCT pool.email) AS invited,
                       COUNT(DISTINCT pool.email) FILTER (WHERE {nj_yes}) AS yes,
                       COUNT(DISTINCT pool.email) FILTER (WHERE {nj_maybe}) AS maybe
                FROM pool
                LEFT JOIN email_domain_provider p
                       ON p.domain = SPLIT_PART(pool.email, '@', 2)
                {nj_join}
                GROUP BY 1
            """).bindparams(**nj_params))).mappings().all()
            for r in rows:
                _add(scopes["nonjoiners"], r["provider"], r)

    # ── no list data: carries this webinar's signal, on no planned list ──
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("SET LOCAL statement_timeout = '280s'"))
        await db.execute(sa_text("SET LOCAL random_page_cost = 8"))
        nld_params = dict(params)
        nld_params["series_re"] = _webinar_series_regex(n)
        nld_params["nj"] = nj or []
        rows = (await db.execute(sa_text(f"""
            WITH {resp_cte},
            planned AS (
                SELECT DISTINCT LOWER(c.email) AS email
                FROM contacts c
                JOIN webinar_contact_memberships m ON m.contact_id = c.id
                WHERE m.webinar_id = CAST(:wid AS uuid) AND c.email IS NOT NULL
            ),
            nj AS (
                SELECT DISTINCT e AS email FROM UNNEST(CAST(:nj AS text[])) AS e
            )
            SELECT {_PROVIDER_EXPR} AS provider,
                   COUNT(DISTINCT LOWER(g.email)) AS invited,
                   COUNT(DISTINCT LOWER(g.email)) FILTER (WHERE r.is_yes) AS yes,
                   COUNT(DISTINCT LOWER(g.email)) FILTER (WHERE r.is_maybe) AS maybe
            FROM ghl_contact g
            LEFT JOIN planned pl ON pl.email = LOWER(g.email)
            LEFT JOIN nj ON nj.email = LOWER(g.email)
            LEFT JOIN email_domain_provider p
                   ON p.domain = SPLIT_PART(LOWER(g.email), '@', 2)
            LEFT JOIN resp r ON r.email = LOWER(g.email)
            WHERE g.email IS NOT NULL AND g.email <> ''
              AND (g.calendar_webinar_series_history ~* :series_re
                   OR g.calendar_invite_response_history ~* :series_re)
              AND pl.email IS NULL AND nj.email IS NULL
            GROUP BY 1
        """).bindparams(**nld_params))).mappings().all()
        for r in rows:
            _add(scopes["noListData"], r["provider"], r)

    # Composite scopes are sums of the queried ones — never re-queried, so they
    # cannot drift from the cohorts they are made of.
    scopes["newJoiners"] = {}
    for src in ("assigned", "noListData"):
        for prov, row in scopes[src].items():
            _add(scopes["newJoiners"], prov, row)
    scopes["overall"] = {}
    for src in ("assigned", "noListData", "nonjoiners"):
        for prov, row in scopes[src].items():
            _add(scopes["overall"], prov, row)

    return {
        "webinarId": head["id"],
        "number": head["number"],
        "variantLabel": head["variant_label"],
        "date": head["date"],
        "title": head["main_title"],
        "scopes": scopes,
    }


async def get_provider_breakdown(webinar_ids: list[str]) -> dict[str, Any]:
    """Provider x cohort breakdown for the given webinars, plus the sum.

    Per-webinar results are memoised (10 min) because the underlying scan is a
    full membership pass; the Home aggregate re-reads the same webinars the
    report page just looked at.
    """
    per_webinar: list[dict[str, Any]] = []
    for wid in webinar_ids:
        cached = _CACHE.get(wid)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            per_webinar.append(cached[1])
            continue
        lock = _LOCKS.setdefault(wid, asyncio.Lock())
        async with lock:
            cached = _CACHE.get(wid)
            if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
                per_webinar.append(cached[1])
                continue
            try:
                data = await _one_webinar(wid)
            except Exception as exc:
                logger.warning("provider breakdown failed for %s: %s", wid, exc)
                continue
            _CACHE[wid] = (time.monotonic(), data)
            per_webinar.append(data)

    totals: dict[str, dict[str, dict[str, int]]] = {
        k: {} for k in ("assigned", "noListData", "nonjoiners", "newJoiners", "overall")
    }
    for w in per_webinar:
        for scope, provs in w["scopes"].items():
            for prov, row in provs.items():
                _add(totals[scope], prov, row)

    def shape(provs: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        return sorted(
            (
                {"provider": prov, **row,
                 "yesPct": (row["yes"] / row["invited"]) if row["invited"] else None,
                 "maybePct": (row["maybe"] / row["invited"]) if row["invited"] else None,
                 "respondedPct": ((row["yes"] + row["maybe"]) / row["invited"])
                                 if row["invited"] else None}
                for prov, row in provs.items()
            ),
            key=lambda r: -r["invited"],
        )

    return {
        "webinars": [
            {k: v for k, v in w.items() if k != "scopes"} | {
                "scopes": {s: shape(p) for s, p in w["scopes"].items()}
            }
            for w in per_webinar
        ],
        "totals": {s: shape(p) for s, p in totals.items()},
        "includedWebinarIds": [w["webinarId"] for w in per_webinar],
    }


async def resolution_status() -> dict[str, Any]:
    """How far the MX backfill has got — surfaced so a partial cache reads as
    partial rather than as a provider called "Not resolved yet"."""
    from sqlalchemy import text as sa_text
    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sa_text(
            "SELECT COUNT(*) AS domains, "
            "COUNT(*) FILTER (WHERE status = 'ok') AS resolved, "
            "MAX(resolved_at)::text AS last_resolved_at "
            "FROM email_domain_provider"
        ))).mappings().one()
    return dict(row)
