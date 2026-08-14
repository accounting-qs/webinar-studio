"""Non-joiner pool — the single source of truth for "who do we invite as a
non-joiner to webinar W".

Operationally the non-joiner sends go to registrants of the LAST SIX webinars
who never joined. The six-webinar window is really a max-six-invites cap: a
contact who registers for W146 and no-shows gets invited as a non-joiner to
W147-W152 — six invites — and by W153 has aged out of the window on its own, so
there is no per-contact counter to maintain.

Every registration restarts the counter, so what decides membership is a
contact's MOST RECENT registration in the window, not their whole history: if
they joined that one they're out, and if they no-showed it they're back in with
a fresh six-invite budget. Someone who no-shows W150, joins W151, then registers
for W152 and no-shows is a non-joiner again for W153.

Pool for webinar W:
  1. Window   — walk `nonjoiner_source_webinar_id` back six hops from W, falling
                back to the next-lower `number` when a link is missing. A
                webinar with no broadcast contributes no emails but still
                consumes one of the six slots; one that hasn't AIRED is stepped
                over entirely (see `_AIRED`).
  2. Stack    — every WebinarGeek registration across those broadcasts, deduped
                on LOWER(email), collapsed per webinar number.
  3. Latest   — keep only each contact's most recent registered number in the
                window; they're a non-joiner iff they did NOT join that one
                (watched live, watched the replay, or logged any viewing
                minutes all count as joining).
  4. Suppress — permanent, one-way exits that re-registering does NOT undo:
                the `blocklist` table (GHL unsubscribes, WebinarGeek
                unsubscribes, manual and CSV additions), `contacts.is_blocklisted`,
                any WebinarGeek unsubscribe in the window, and converted
                contacts — booked a call, deal won, or disqualified.
  5. Planned  — anyone already on a planned list for W, so each contact lands in
                exactly one cohort (planned > non-joiner > no-list-data).

Both `ghl_statistics_source` (Statistics page) and `webinar_report` (per-webinar
report + weekly email) consume this module, so the two agree by construction.
Before this existed the stats page derived non-joiners from the previous webinar
only and undercounted the pool roughly four-fold.
"""

from datetime import date
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

# Sourced from statistics_metric_filters (a dependency-free constants module)
# rather than ghl_statistics_source, which imports THIS module.
from services.statistics_metric_filters import (
    DEAL_WON_STAGE_ID, DISQUALIFIED_STAGE_ID,
)

# How many webinars back the pool reaches == how many times we invite a
# non-joiner before dropping them.
NONJOINER_WINDOW = 6

# "Joined" for pool-exclusion purposes. Deliberately wider than the stats
# ATT_PREDICATE (`watched_live = TRUE OR minutes_viewing > 0`): watching the
# replay also counts as joining, so replay-only viewers leave the pool.
#
# Written with IS TRUE / COALESCE rather than `= TRUE` because bool_or() ignores
# NULL inputs and returns NULL for an all-NULL group — `watched_live = TRUE`
# yields NULL on unsynced rows, and the resulting NULL would fail the `NOT
# joined` test and silently drop genuine non-joiners out of the pool. `IS TRUE`
# never returns NULL.
JOINED_SQL = (
    "(watched_live IS TRUE "
    "OR watched_replay IS TRUE "
    "OR COALESCE(minutes_viewing, 0) > 0)"
)

# Default planned-contact exclusion: memberships on the target webinar only.
# `ghl_statistics_source` overrides this with its pre-materialized
# tmp_nld_planned temp table, which also spans sibling A/B variants.
_DEFAULT_PLANNED_SQL = """
    SELECT 1 FROM contacts c
    JOIN webinar_contact_memberships m ON m.contact_id = c.id
    WHERE m.webinar_id = CAST(:nj_wid AS uuid)
      AND LOWER(c.email) = p.email
"""

_WEBINAR_COLS = (
    "id, user_id, number, broadcast_id, nonjoiner_source_webinar_id, date, "
    "broadcast_auto_synced_at"
)

# A webinar that hasn't aired can't have produced no-shows. Without this guard a
# broadcast synced ahead of time (wg_sync can be triggered manually) would dump
# every one of its registrants into the next pool as fake no-shows AND restart
# their invite counters. `broadcast_auto_synced_at` fires 2h after the broadcast
# starts; the date check covers webinars predating that column.
_AIRED = "(date < CURRENT_DATE OR broadcast_auto_synced_at IS NOT NULL)"


async def _load_webinar(db: AsyncSession, webinar_id: str) -> Any:
    return (await db.execute(sa_text(
        f"SELECT {_WEBINAR_COLS} FROM webinars WHERE id = CAST(:wid AS uuid)"
    ).bindparams(wid=webinar_id))).mappings().first()


async def window_webinar_numbers(
    db: AsyncSession, webinar_id: str, window: int = NONJOINER_WINDOW
) -> list[int]:
    """The `window` webinar numbers preceding `webinar_id`, nearest first.

    Prefers the explicit `nonjoiner_source_webinar_id` chain — that link is how
    the user records which webinar came after which — and falls back to plain
    number ordering wherever the chain is missing or points the wrong way.
    A/B variants sharing a number collapse into a single slot.
    """
    head = await _load_webinar(db, webinar_id)
    if head is None:
        return []

    uid = head["user_id"]
    seen_ids: set[str] = {str(head["id"])}
    numbers: list[int] = []
    cur_number: int = head["number"]
    link = head["nonjoiner_source_webinar_id"]

    # Each pass adds at most one number; variants of an already-collected number
    # burn an iteration without extending the window, so allow some slack before
    # giving up rather than looping on a pathological chain.
    for _ in range(window * 4):
        if len(numbers) >= window:
            break

        row = None
        if link is not None and str(link) not in seen_ids:
            row = await _load_webinar(db, str(link))
            # A link that doesn't move us backwards is mis-set; ignore it and
            # let the number fallback below take over.
            if row is not None and row["number"] >= cur_number:
                row = None

        if row is None:
            row = (await db.execute(sa_text(
                f"SELECT {_WEBINAR_COLS} FROM webinars "
                "WHERE user_id = CAST(:uid AS uuid) AND number < :n "
                "ORDER BY number DESC, variant_label ASC NULLS FIRST LIMIT 1"
            ).bindparams(uid=str(uid), n=cur_number))).mappings().first()

        if row is None:
            break

        seen_ids.add(str(row["id"]))
        aired = (
            (row["date"] is not None and row["date"] < date.today())
            or row["broadcast_auto_synced_at"] is not None
        )
        # An un-aired webinar is stepped OVER rather than counted: it can't have
        # produced no-shows, and burning a slot on it would silently shorten the
        # window to five real webinars.
        if aired and row["number"] not in numbers:
            numbers.append(row["number"])
        cur_number = row["number"]
        link = row["nonjoiner_source_webinar_id"]

    return numbers


async def window_broadcast_ids(
    db: AsyncSession, webinar_id: str, window: int = NONJOINER_WINDOW
) -> list[str]:
    """WebinarGeek broadcast ids for every webinar in the window (all variants).

    A window slot whose webinar has no linked broadcast simply contributes
    nothing — it still counts as one of the six. Un-aired webinars never reach
    this point (`window_webinar_numbers` steps over them); the `_AIRED` filter
    here also drops an un-aired variant of an otherwise-aired number.
    """
    head = await _load_webinar(db, webinar_id)
    if head is None:
        return []
    numbers = await window_webinar_numbers(db, webinar_id, window)
    if not numbers:
        return []
    rows = (await db.execute(sa_text(
        "SELECT DISTINCT broadcast_id FROM webinars "
        "WHERE user_id = CAST(:uid AS uuid) "
        "  AND number = ANY(CAST(:nums AS int[])) "
        "  AND broadcast_id IS NOT NULL "
        f"  AND {_AIRED}"
    ).bindparams(uid=str(head["user_id"]), nums=numbers))).all()
    return [r[0] for r in rows if r[0]]


async def nonjoiner_pool_emails(
    db: AsyncSession,
    webinar_id: str,
    *,
    planned_sql: str | None = None,
    planned_params: dict[str, Any] | None = None,
    window: int = NONJOINER_WINDOW,
) -> list[str]:
    """Lowercased emails of this webinar's non-joiner pool.

    `planned_sql` is the body of a NOT EXISTS correlated against `p.email` and
    overrides the default membership lookup; pass the matching binds in
    `planned_params`.
    """
    head = await _load_webinar(db, webinar_id)
    if head is None:
        return []

    numbers = await window_webinar_numbers(db, webinar_id, window)
    if not numbers:
        return []

    params: dict[str, Any] = {
        "nj_nums": numbers,
        "nj_uid": str(head["user_id"]),
        "nj_won_stage": DEAL_WON_STAGE_ID,
        "nj_dq_stage": DISQUALIFIED_STAGE_ID,
    }
    if planned_sql is None:
        planned_sql = _DEFAULT_PLANNED_SQL
        params["nj_wid"] = webinar_id
    params.update(planned_params or {})

    rows = (await db.execute(sa_text(f"""
        WITH win AS (
            SELECT broadcast_id, number FROM webinars
            WHERE user_id = CAST(:nj_uid AS uuid)
              AND number = ANY(CAST(:nj_nums AS int[]))
              AND broadcast_id IS NOT NULL
              AND {_AIRED}
        ),
        -- Collapse to one row per (contact, webinar number) so A/B variants of
        -- the same number can't tie for "latest": joining either variant means
        -- they joined that webinar.
        per_number AS (
            SELECT LOWER(s.email) AS email,
                   w.number,
                   bool_or({JOINED_SQL}) AS joined
            FROM webinargeek_subscribers s
            JOIN win w ON w.broadcast_id = s.broadcast_id
            WHERE s.email IS NOT NULL
            GROUP BY 1, 2
        ),
        -- Every registration restarts the counter, so only the most recent one
        -- in the window decides.
        latest AS (
            SELECT DISTINCT ON (email) email, joined
            FROM per_number
            ORDER BY email, number DESC
        ),
        -- One opt-out anywhere in the window is permanent, so this spans the
        -- whole window rather than just the latest registration.
        unsubscribed AS (
            SELECT DISTINCT LOWER(s.email) AS email
            FROM webinargeek_subscribers s
            JOIN win w ON w.broadcast_id = s.broadcast_id
            WHERE s.email IS NOT NULL AND s.unsubscribed_at IS NOT NULL
        ),
        pool AS (
            SELECT l.email FROM latest l
            WHERE NOT l.joined
              AND NOT EXISTS (SELECT 1 FROM unsubscribed u WHERE u.email = l.email)
        )
        SELECT p.email FROM pool p
        -- The blocklist table is the canonical permanent-suppression list and
        -- is keyed by email alone: it aggregates GHL unsubscribes ('ghl_dnd'),
        -- WebinarGeek unsubscribes ('wg_unsub'), manual UI additions and CSV
        -- imports. Checked directly rather than only via contacts.is_blocklisted
        -- because ~9% of the pool has no contacts row at all (they came from
        -- WebinarGeek, not from one of our uploads), and those slipped through.
        WHERE NOT EXISTS (
            SELECT 1 FROM blocklist b WHERE LOWER(b.email) = p.email
        )
          AND NOT EXISTS (
            SELECT 1 FROM contacts c
            WHERE LOWER(c.email) = p.email
              AND c.user_id = CAST(:nj_uid AS uuid)
              AND c.is_blocklisted
        )
        -- Converted contacts leave the cold funnel for good. Booking a call,
        -- closing, or being disqualified are all one-way doors: unlike
        -- attendance, re-registering does NOT bring them back.
          AND NOT EXISTS (
            SELECT 1 FROM ghl_contact g
            WHERE LOWER(g.email) = p.email
              AND (
                LOWER(COALESCE(g.is_booked_call, '')) IN ('true', 'yes', '1')
                OR EXISTS (
                    SELECT 1 FROM webinar_booking_attribution b
                    WHERE b.ghl_contact_id = g.ghl_contact_id
                )
                OR EXISTS (
                    SELECT 1 FROM ghl_opportunity o
                    WHERE o.ghl_contact_id = g.ghl_contact_id
                      AND o.pipeline_stage_id IN (:nj_won_stage, :nj_dq_stage)
                )
              )
        )
          AND NOT EXISTS ({planned_sql})
    """).bindparams(**params))).all()
    return [r[0] for r in rows if r[0]]
