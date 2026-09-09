"""Outreach sub-router: release contacts back to the bucket pool after a webinar.

Operators upload a CSV of emails that could not be contacted in time. We revert
those contacts (status `assigned` or `used` → `available`) so they can be
re-assigned to a future webinar. `WebinarListAssignment.volume` is left
untouched so the original "planned" number is preserved for plan-vs-actual
comparison on the statistics page.

Each released contact is recorded in `contact_release_log` for a future undo /
auth-aware audit trail.

A CSV release runs as a background job (`_run_release_job`) that commits one
chunk at a time, because a release does far more work than its row count
suggests: every released contact rewrites 12 contact indexes, and the touched
buckets' fresh baselines get re-derived from scratch. Doing that inside the
request meant one slow release hit the 120s statement cap and rolled the WHOLE
upload back — the operator saw a stuck progress bar and "released 0". See
`_RELEASE_JOBS` below.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from api.routers.outreach._helpers import (
    LLOYD_USER_ID, reconcile_bucket_remaining, release_contact_slots,
)
from api.routers.outreach.webinars import _is_retryable_db_error
from db.models import (
    Contact, ContactReleaseLog, Webinar, WebinarContactMembership,
)
from db.session import AsyncSessionLocal, get_db

logger = logging.getLogger(__name__)

router = APIRouter()


class ReleaseRequest(BaseModel):
    emails: list[str]
    # Optional batch id to group multiple chunked requests into one audit
    # entry. The frontend uploads in 1k-row chunks for progress reporting;
    # all chunks for the same upload share a release_batch_id so the audit
    # log + future "undo" action treat them atomically. The first chunk
    # omits this and the server generates one; subsequent chunks pass it back.
    release_batch_id: str | None = None


class ReleaseByIdRequest(BaseModel):
    contact_ids: list[str]
    release_batch_id: str | None = None
    # Scope guard: the assignment(s) the operator is currently looking at.
    # The server will refuse to release any contact_id whose current
    # assignment_id is not in this set — protects against a future UI bug
    # accidentally submitting ids outside the visible page.
    assignment_ids: list[str] | None = None


def _normalize_email(raw: str) -> str | None:
    if not raw:
        return None
    e = raw.strip().lower()
    return e or None


# asyncpg caps bind parameters at 32,767 per query. Our largest IN-clauses use
# one parameter per email (plus a few constants), so cap at 5,000 to stay well
# under the limit and match the chunking pattern used by the import pipeline.
_DB_CHUNK_SIZE = 5000


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# A CSV release ALWAYS runs as a background job — there is no "small enough to
# do inline" size. The dominant cost is re-deriving each touched bucket's fresh
# baseline, which scales with BUCKET size, not with how many emails were
# uploaded: releasing 200 emails across webinar 154's 18 buckets measured 64s,
# essentially all of it the recount. Inline, that put every release one cold
# cache away from the 120s statement cap, and because the request is a single
# transaction, blowing it rolled back the entire upload — the operator saw a
# frozen progress bar and "released 0".
#
# Emails per committed transaction inside the job. Each chunk is independently
# committed, so a failure only costs the chunk in flight.
#
# Was 2000, sized on the assumption that fixed per-chunk work dominated. That is
# no longer true: the bucket recount already ran once at the end, and the two
# remaining fixed costs (the assignments SELECT, and the eligible-cache
# invalidation that kicked a full rollup rebuild) have both been hoisted out. What
# is left scales linearly with the chunk, so a smaller chunk costs nothing and
# buys three things: the progress bar moves 4x more often (a 3.4k-email upload now
# steps 7 times instead of twice, instead of sitting at 0% for minutes), row locks
# are held ~4x more briefly, and a retryable failure redoes 500 rows instead of
# 2000. Timeout headroom too — the merged UPDATE is ~5.5s at 500 against the 120s
# cap. Don't go below 500: the fixed session/commit overhead starts to show.
_RELEASE_JOB_CHUNK = 500

# By-id selections at or under this size are released inline in the request, as
# they always were; anything larger returns a job and runs in the background.
# Mirrors _MARK_SYNC_LIMIT in webinars.py — the interactive path stays snappy and
# unchanged for the ordinary case, and only a bulk selection pays for polling.
_RELEASE_SYNC_LIMIT = 500

# job_id → progress dict. In-memory on purpose: progress is ephemeral, the
# membership deletes + contact_release_log rows are the durable state. Pruned
# lazily on job creation. Mirrors _MARK_JOBS in webinars.py.
_RELEASE_JOBS: dict[str, dict] = {}
_active_release_tasks: dict[str, asyncio.Task] = {}


async def _release_emails_chunk(
    db: AsyncSession,
    webinar_id: str,
    emails: list[str],
    release_batch_id: str,
    now: datetime,
    *,
    reconcile_buckets: bool = True,
) -> dict:
    """Release one chunk of (already normalized, deduped) emails from this
    webinar. Flushes but does NOT commit — the caller owns the transaction.

    Idempotent: an email with no membership in this webinar (already released,
    or never scheduled here) is reported, not re-released, so re-running the
    same CSV after a partial failure finishes the job instead of double-counting.

    `reconcile_buckets=False` skips the bucket recount and just reports the
    touched buckets in `touched_bucket_ids`. The recount is a full re-derivation
    of each bucket's fresh baseline, so its cost depends on bucket size, not on
    this chunk — a multi-chunk job runs it once at the end over the union
    instead of paying it per chunk.
    """
    # Resolve every email in ONE pass: drive from the uploaded list, join the
    # contact, then LEFT JOIN this webinar's membership. Rows with a membership
    # are releasable; rows without one are contacts that exist but were already
    # released (or never scheduled here); emails with no row at all are unknown.
    # Replaces two separate scans (matched, then a second pass to classify the
    # misses) that between them read the same contact pages twice.
    #
    # m.user_id belongs in the ON clause, NOT the WHERE — in WHERE it turns the
    # LEFT JOIN back into an inner join and every already-released email would be
    # misreported as not_found.
    #
    # lower(c.email) is deliberate: ix_contacts_lower_email is defined on
    # lower(email), and while migration 069 added a lowercase CHECK it is still
    # NOT VALID, so a legacy mixed-case row would silently fail to match a plain
    # equality and never get released.
    resolve_sql = sa_text(
        "SELECT c.id AS contact_id, lower(c.email) AS email, "
        "       c.assignment_id AS legacy_assignment_id, "
        "       m.status AS m_status, m.assignment_id AS m_assignment_id, "
        "       m.bucket_id AS m_bucket_id, m.used_at AS m_used_at "
        "FROM unnest(CAST(:emails AS text[])) AS k(email) "
        "JOIN contacts c "
        "  ON c.user_id = CAST(:uid AS uuid) AND lower(c.email) = k.email "
        "LEFT JOIN webinar_contact_memberships m "
        "  ON m.contact_id = c.id "
        " AND m.webinar_id = CAST(:wid AS uuid) "
        " AND m.user_id    = CAST(:uid AS uuid)"
    )
    # by_email: releasable (has a membership here). known: every email that
    # resolved to a contact at all, membership or not — drives the
    # already_available vs not_found split exactly as the old second pass did.
    by_email: dict[str, dict] = {}
    known_emails: set[str] = set()
    for chunk in _chunked(emails, _DB_CHUNK_SIZE):
        c_result = await db.execute(
            resolve_sql,
            {"emails": chunk, "uid": LLOYD_USER_ID, "wid": webinar_id},
        )
        for row in c_result.all():
            known_emails.add(row.email)
            if row.m_status is None:
                continue
            by_email[row.email] = {
                "id": row.contact_id,
                "status": row.m_status,
                "assignment_id": row.m_assignment_id,
                "bucket_id": row.m_bucket_id,
                "used_at": row.m_used_at,
                "legacy_assignment_id": row.legacy_assignment_id,
            }

    not_found: list[str] = []
    already_available: list[str] = []
    by_status_count = {"assigned": 0, "used": 0}
    touched_bucket_ids: set[str] = set()
    log_rows: list[dict] = []
    contact_ids_to_release: list[str] = []
    legacy_reset_ids: list[str] = []
    # assignment_id → how many 'assigned' memberships this chunk releases from it.
    per_assignment: dict[str, int] = {}

    for email in emails:
        target = by_email.get(email)
        if target is None:
            # No membership in this webinar — already released, or unknown email.
            (already_available if email in known_emails else not_found).append(email)
            continue

        log_rows.append({
            "user_id": LLOYD_USER_ID,
            "webinar_id": webinar_id,
            "release_batch_id": release_batch_id,
            "released_at": now,
            "released_by": None,
            "contact_id": target["id"],
            "email": email,
            "prior_status": target["status"],
            "prior_assignment_id": target["assignment_id"],
            "prior_bucket_id": target["bucket_id"],
            "prior_used_at": target["used_at"],
        })
        contact_ids_to_release.append(target["id"])
        by_status_count[target["status"]] += 1
        if target["bucket_id"]:
            touched_bucket_ids.add(target["bucket_id"])
        # Only reset the legacy slot when it actually represents THIS webinar's
        # membership; a reused contact's slot points at another webinar and must
        # be left intact.
        if target["legacy_assignment_id"] and target["legacy_assignment_id"] == target["assignment_id"]:
            legacy_reset_ids.append(target["id"])

        # `assignment.remaining` tracks "claimed but not yet marked used"
        # (mark_contacts_used decrements it). Releasing an `assigned` contact
        # removes one from that pool. Releasing a `used` contact doesn't
        # touch it — it was already decremented at mark-used time.
        if target["status"] == "assigned" and target["assignment_id"]:
            aid = target["assignment_id"]
            per_assignment[aid] = per_assignment.get(aid, 0) + 1

    # Bulk INSERT audit-log rows. asyncpg's param cap is 32,767; each row has
    # 11 columns so ~2,900 rows per insert is the hard limit — we use 2,000.
    LOG_CHUNK = 2000
    for chunk in _chunked(log_rows, LOG_CHUNK):
        await db.execute(insert(ContactReleaseLog), chunk)

    # Remove this webinar's membership rows for the released contacts (release =
    # "never scheduled for this webinar" → drops out of its metrics and stops
    # counting toward times_invited), then recompute the affected caches.
    for chunk in _chunked(contact_ids_to_release, _DB_CHUNK_SIZE):
        await db.execute(
            delete(WebinarContactMembership).where(
                WebinarContactMembership.webinar_id == webinar_id,
                WebinarContactMembership.contact_id.in_(chunk),
            )
        )

    # Decrement the assignments' `remaining` counters in one statement. A
    # relative decrement rather than the absolute write an ORM instance would
    # produce, so a mark-used job committing concurrently can't be clobbered by a
    # value this transaction read before it started. The user_id/webinar_id
    # predicates reproduce the old assignments_by_id.get() miss-is-a-no-op.
    if per_assignment:
        await db.execute(
            sa_text(
                "UPDATE webinar_list_assignments a "
                "   SET remaining = GREATEST(0, a.remaining - v.n), "
                "       updated_at = now() "
                "  FROM unnest(CAST(:aids AS uuid[]), CAST(:ns AS int[])) AS v(aid, n) "
                " WHERE a.id = v.aid "
                "   AND a.user_id = CAST(:uid AS uuid) "
                "   AND a.webinar_id = CAST(:wid AS uuid)"
            ),
            {
                "aids": list(per_assignment.keys()),
                "ns": list(per_assignment.values()),
                "uid": LLOYD_USER_ID,
                "wid": webinar_id,
            },
        )

    # One statement for the legacy slot reset AND the cache re-derivation — must
    # run after the DELETE above so it sees the memberships that survive. This
    # used to be three separate UPDATEs over the same rows, i.e. three non-HOT
    # rewrites of every contact index.
    await release_contact_slots(db, contact_ids_to_release, legacy_reset_ids)

    # Reconcile bucket.remaining_contacts from the live fresh baseline (never
    # invited, not in-flight) — keeps the field self-healing if it ever drifts.
    bucket_updates: dict[str, int] = {}
    if touched_bucket_ids and reconcile_buckets:
        await db.flush()  # so the cache updates are visible to the count query
        bucket_updates = await reconcile_bucket_remaining(db, touched_bucket_ids)

    await db.flush()

    # NOTE: no invalidate_eligible_cache() here. It does not just clear a dict —
    # it marks the fresh rollup stale and immediately starts a rebuild that walks
    # the whole claimable pool, and the scheduler's dirty-flag loop makes a call
    # arriving mid-rebuild queue up another one. Firing it per chunk therefore
    # kept the rollup rebuilding continuously for the entire release, competing
    # with the release itself for a 5+10 connection pool, and every rebuild but
    # the last was stale on arrival anyway. The job invalidates once when it is
    # done; the synchronous caller does its own.
    return {
        "released": len(contact_ids_to_release),
        "not_found": not_found,
        "already_available": already_available,
        "by_status": by_status_count,
        "bucket_updates": bucket_updates,
        "touched_bucket_ids": sorted(touched_bucket_ids),
    }


def _release_job_public(job: dict) -> dict:
    return {k: v for k, v in job.items() if not k.startswith("_")}


async def _run_release_job(
    job_id: str, webinar_id: str, emails: list[str], release_batch_id: str
) -> None:
    """Background worker: one committed transaction per chunk, so progress
    survives any single failure and a dead task never holds locks.

    Every chunk is idempotent (see `_release_emails_chunk`), so a task killed
    mid-upload — a deploy, a timeout that outlives the retries — just leaves the
    tail unreleased; re-running the same CSV finishes it.
    """
    job = _RELEASE_JOBS[job_id]
    touched: set[str] = set()
    try:
        for i in range(0, len(emails), _RELEASE_JOB_CHUNK):
            chunk = emails[i : i + _RELEASE_JOB_CHUNK]
            for attempt in range(3):
                try:
                    async with AsyncSessionLocal() as db:
                        res = await _release_emails_chunk(
                            db, webinar_id, chunk, release_batch_id,
                            datetime.now(timezone.utc),
                            reconcile_buckets=False,
                        )
                        await db.commit()
                    job["released"] += res["released"]
                    job["not_found"].extend(res["not_found"])
                    job["already_available"].extend(res["already_available"])
                    job["by_status"]["assigned"] += res["by_status"]["assigned"]
                    job["by_status"]["used"] += res["by_status"]["used"]
                    touched.update(res["touched_bucket_ids"])
                    break
                except Exception as exc:
                    if attempt < 2 and _is_retryable_db_error(exc):
                        await asyncio.sleep(1 + 2 * attempt)
                        continue
                    raise
            job["done"] = min(i + len(chunk), job["total"])
        job["status"] = "done"
    except Exception as exc:
        logger.exception(
            "Release job %s failed at %s/%s", job_id, job["done"], job["total"]
        )
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        # Recount every touched bucket ONCE, after the last chunk — including
        # when the job failed part-way, so a partial release still leaves the
        # counters true rather than stale. Its own transaction: the releases are
        # already committed and must not be undone by a reconcile failure.
        for attempt in range(3):
            if not touched:
                break
            try:
                async with AsyncSessionLocal() as db:
                    job["bucket_updates"] = await reconcile_bucket_remaining(db, touched)
                    await db.commit()
                break
            except Exception as exc:
                if attempt < 2 and _is_retryable_db_error(exc):
                    await asyncio.sleep(1 + 2 * attempt)
                    continue
                logger.exception(
                    "Release job %s: bucket reconcile failed for %d bucket(s); "
                    "remaining_contacts may read low until the next release",
                    job_id, len(touched),
                )
                break
        # Unconditional, and OUTSIDE the loop above: the release moved contacts
        # back into the claimable pool whether or not any bucket was recounted.
        # `touched` is empty for custom_list memberships (their bucket_id is
        # NULL) and the loop also exits early when every reconcile attempt fails
        # — in both cases the eligible counts and the fresh rollup are still
        # stale, so this has to run regardless. It used to sit inside the loop,
        # where the per-chunk call masked the gap.
        from api.routers.outreach.buckets import invalidate_eligible_cache
        invalidate_eligible_cache()
        job["_ts"] = datetime.now(timezone.utc).timestamp()
        _active_release_tasks.pop(job_id, None)


def _spawn_release_job(
    webinar_id: str, emails: list[str], release_batch_id: str
) -> dict:
    now_ts = datetime.now(timezone.utc).timestamp()
    for jid in [
        jid for jid, j in _RELEASE_JOBS.items()
        if j["status"] != "running" and j["_ts"] < now_ts - 3600
    ]:
        _RELEASE_JOBS.pop(jid, None)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id, "status": "running", "total": len(emails), "done": 0,
        "release_batch_id": release_batch_id, "released": 0,
        "not_found": [], "already_available": [],
        "by_status": {"assigned": 0, "used": 0}, "bucket_updates": {},
        "error": None, "_ts": now_ts,
    }
    _RELEASE_JOBS[job_id] = job
    _active_release_tasks[job_id] = asyncio.create_task(
        _run_release_job(job_id, webinar_id, emails, release_batch_id)
    )
    return job


def _spawn_release_ids_job(
    contact_ids: list[str],
    scope_assignment_ids: set[str] | None,
    release_batch_id: str,
) -> dict:
    """Same registry and same GET /release-jobs/{id} as the CSV path; the extra
    `out_of_scope` list is the only shape difference."""
    now_ts = datetime.now(timezone.utc).timestamp()
    for jid in [
        jid for jid, j in _RELEASE_JOBS.items()
        if j["status"] != "running" and j["_ts"] < now_ts - 3600
    ]:
        _RELEASE_JOBS.pop(jid, None)
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id, "status": "running", "total": len(contact_ids), "done": 0,
        "release_batch_id": release_batch_id, "released": 0,
        "not_found": [], "already_available": [], "out_of_scope": [],
        "by_status": {"assigned": 0, "used": 0}, "bucket_updates": {},
        "error": None, "_ts": now_ts,
    }
    _RELEASE_JOBS[job_id] = job
    _active_release_tasks[job_id] = asyncio.create_task(
        _run_release_ids_job(job_id, contact_ids, scope_assignment_ids, release_batch_id)
    )
    return job


@router.get("/release-jobs/{job_id}")
async def get_release_job(job_id: str, _: str = Depends(require_auth)):
    job = _RELEASE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Release job not found")
    return _release_job_public(job)


@router.post("/webinars/{webinar_id}/releases", status_code=201)
async def release_contacts(
    webinar_id: str,
    body: ReleaseRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Release contacts in this webinar back to `available`.

    For each email in `body.emails` that maps to a contact assigned to one of
    this webinar's WebinarListAssignments and currently in status `assigned`
    or `used`: revert the contact (clear assignment_id, used_at, assigned_date;
    set status to `available`) and snapshot the prior state into
    `contact_release_log` under one shared `release_batch_id`.

    Bucket `remaining_contacts` is restored from the live `available` count for
    each touched bucket. Assignment `volume` is intentionally untouched so the
    planned-send number is preserved for statistics comparison.

    Always returns immediately with a `job` the client polls at
    `GET /outreach/release-jobs/{job_id}`; `released` is 0 on this response and
    accumulates on the job.
    """
    w_result = await db.execute(
        select(Webinar).where(
            Webinar.id == webinar_id,
            Webinar.user_id == LLOYD_USER_ID,
        )
    )
    webinar = w_result.scalar_one_or_none()
    if not webinar:
        raise HTTPException(404, "Webinar not found")

    # Normalize + dedupe input emails, drop empties
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in body.emails:
        e = _normalize_email(raw)
        if e and e not in seen:
            seen.add(e)
            normalized.append(e)

    if not normalized:
        raise HTTPException(400, "No valid emails provided")

    release_batch_id = body.release_batch_id or str(uuid.uuid4())

    # Hand the whole list to the background job and answer now. `released` is 0
    # here by construction and accumulates on the job; the client polls
    # GET /outreach/release-jobs/{job_id} for progress and the final totals.
    job = _spawn_release_job(webinar_id, normalized, release_batch_id)
    return {
        "release_batch_id": release_batch_id,
        "released": 0,
        "not_found": [],
        "already_available": [],
        "by_status": {"assigned": 0, "used": 0},
        "bucket_updates": {},
        "job": _release_job_public(job),
    }


@router.get("/webinars/{webinar_id}/releases")
async def list_releases(
    webinar_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """List release batches for this webinar (newest first)."""
    w_result = await db.execute(
        select(Webinar.id).where(
            Webinar.id == webinar_id,
            Webinar.user_id == LLOYD_USER_ID,
        )
    )
    if not w_result.scalar_one_or_none():
        raise HTTPException(404, "Webinar not found")

    from sqlalchemy import func as sa_func
    r = await db.execute(
        select(
            ContactReleaseLog.release_batch_id,
            sa_func.min(ContactReleaseLog.released_at).label("released_at"),
            sa_func.count().label("count"),
            sa_func.count().filter(ContactReleaseLog.prior_status == "used").label("used_count"),
            sa_func.count().filter(ContactReleaseLog.prior_status == "assigned").label("assigned_count"),
        )
        .where(
            ContactReleaseLog.webinar_id == webinar_id,
            ContactReleaseLog.user_id == LLOYD_USER_ID,
        )
        .group_by(ContactReleaseLog.release_batch_id)
        .order_by(sa_func.min(ContactReleaseLog.released_at).desc())
    )
    batches = [
        {
            "release_batch_id": row.release_batch_id,
            "released_at": row.released_at.isoformat() if row.released_at else None,
            "count": int(row.count or 0),
            "used_count": int(row.used_count or 0),
            "assigned_count": int(row.assigned_count or 0),
        }
        for row in r.all()
    ]
    return {"batches": batches}


async def _release_ids_chunk(
    db: AsyncSession,
    contact_ids: list[str],
    scope_assignment_ids: set[str] | None,
    release_batch_id: str,
    now: datetime,
    *,
    reconcile_buckets: bool = True,
) -> dict:
    """Release one chunk of (already deduped) contact ids. Flushes but does NOT
    commit — the caller owns the transaction.

    The by-id twin of `_release_emails_chunk`: same revert + audit-log pipeline,
    but keyed on contact id and spanning webinars, since the operator's selection
    can cross them. Idempotent for the same reason — a contact with no membership
    in scope is reported, not re-released.
    """
    m = WebinarContactMembership
    mem_by_contact: dict[str, dict] = {}
    had_any_membership: set[str] = set()
    # Load the membership rows for these contacts (optionally restricted to the
    # assignment(s) the operator is viewing). The membership carries webinar_id
    # and the authoritative status — so a reused contact is released from the
    # viewed webinar, not whatever its legacy slot happens to point at.
    # `had_any_membership` tracks contacts that DO hold memberships but none in
    # scope, so the response can report them as out_of_scope rather than
    # not_found. Without a scope, a multi-webinar contact's NEWEST membership is
    # released (created_at DESC) — deterministic, and matches the operator
    # intuition of undoing the most recent scheduling.
    #
    # Kept as IN (...) deliberately. Rewriting it as = ANY(uuid[]) was considered
    # and measured: cold, an IN-list and an array/unnest form of the same lookup
    # read the same ~4k pages and run in the same ~2-3s, because resolution here
    # is bound by random page reads, not by plan shape. Not worth the casting
    # fragility of hand-building a uuid[] bind against an ORM column.
    for chunk in _chunked(contact_ids, _DB_CHUNK_SIZE):
        if scope_assignment_ids is not None:
            any_result = await db.execute(
                select(m.contact_id).where(
                    m.user_id == LLOYD_USER_ID,
                    m.contact_id.in_(chunk),
                )
            )
            had_any_membership.update(any_result.scalars().all())
        conds = [m.user_id == LLOYD_USER_ID, m.contact_id.in_(chunk)]
        if scope_assignment_ids is not None:
            conds.append(m.assignment_id.in_(scope_assignment_ids))
        c_result = await db.execute(
            select(
                m.contact_id,
                Contact.email.label("email"),
                m.status, m.webinar_id, m.assignment_id, m.bucket_id, m.used_at,
                Contact.assignment_id.label("legacy_assignment_id"),
            )
            .join(Contact, Contact.id == m.contact_id)
            .where(*conds)
            .order_by(m.contact_id, m.created_at.desc())
        )
        for row in c_result.all():
            # First row per contact wins = newest membership (ORDER BY above);
            # an explicit scope narrows it to the viewed page's lists.
            mem_by_contact.setdefault(row.contact_id, {
                "id": row.contact_id,
                "email": row.email.lower() if row.email else None,
                "status": row.status,
                "webinar_id": row.webinar_id,
                "assignment_id": row.assignment_id,
                "bucket_id": row.bucket_id,
                "used_at": row.used_at,
                "legacy_assignment_id": row.legacy_assignment_id,
            })

    not_found: list[str] = []
    out_of_scope: list[str] = []
    by_status_count = {"assigned": 0, "used": 0}
    touched_bucket_ids: set[str] = set()
    log_rows: list[dict] = []
    contact_ids_to_release: list[str] = []
    legacy_reset_ids: list[str] = []
    per_assignment: dict[str, int] = {}

    for cid in contact_ids:
        row = mem_by_contact.get(cid)
        if row is None:
            # Memberships exist but none in the viewed scope → out_of_scope
            # (feeds the frontend's scope-violation warning); otherwise the
            # contact has nothing to release.
            (out_of_scope if cid in had_any_membership else not_found).append(cid)
            continue

        log_rows.append({
            "user_id": LLOYD_USER_ID,
            "webinar_id": row["webinar_id"],
            "release_batch_id": release_batch_id,
            "released_at": now,
            "released_by": None,
            "contact_id": row["id"],
            "email": row["email"],
            "prior_status": row["status"],
            "prior_assignment_id": row["assignment_id"],
            "prior_bucket_id": row["bucket_id"],
            "prior_used_at": row["used_at"],
        })
        contact_ids_to_release.append(row["id"])
        by_status_count[row["status"]] += 1
        if row["bucket_id"]:
            touched_bucket_ids.add(row["bucket_id"])
        if row["legacy_assignment_id"] and row["legacy_assignment_id"] == row["assignment_id"]:
            legacy_reset_ids.append(row["id"])
        if row["status"] == "assigned" and row["assignment_id"]:
            aid = row["assignment_id"]
            per_assignment[aid] = per_assignment.get(aid, 0) + 1

    LOG_CHUNK = 2000
    for chunk in _chunked(log_rows, LOG_CHUNK):
        await db.execute(insert(ContactReleaseLog), chunk)

    # Remove the membership rows for exactly the (contact, webinar) pairs being
    # released (webinar_id came from each contact's viewed assignment), so a
    # reused contact loses only the membership for THIS webinar.
    release_by_webinar: dict[str, list[str]] = {}
    for lr in log_rows:
        release_by_webinar.setdefault(lr["webinar_id"], []).append(lr["contact_id"])
    for wid, cids in release_by_webinar.items():
        for chunk in _chunked(cids, _DB_CHUNK_SIZE):
            await db.execute(
                delete(WebinarContactMembership).where(
                    WebinarContactMembership.webinar_id == wid,
                    WebinarContactMembership.contact_id.in_(chunk),
                )
            )

    # See _release_emails_chunk: relative decrement, one statement. No webinar
    # predicate here — a by-id selection can span webinars, and the assignment id
    # already identifies the row uniquely.
    if per_assignment:
        await db.execute(
            sa_text(
                "UPDATE webinar_list_assignments a "
                "   SET remaining = GREATEST(0, a.remaining - v.n), "
                "       updated_at = now() "
                "  FROM unnest(CAST(:aids AS uuid[]), CAST(:ns AS int[])) AS v(aid, n) "
                " WHERE a.id = v.aid AND a.user_id = CAST(:uid AS uuid)"
            ),
            {
                "aids": list(per_assignment.keys()),
                "ns": list(per_assignment.values()),
                "uid": LLOYD_USER_ID,
            },
        )

    # Legacy slot reset + cache re-derivation in one pass, after the DELETE.
    await release_contact_slots(db, contact_ids_to_release, legacy_reset_ids)

    bucket_updates: dict[str, int] = {}
    if touched_bucket_ids and reconcile_buckets:
        await db.flush()
        bucket_updates = await reconcile_bucket_remaining(db, touched_bucket_ids)

    await db.flush()
    return {
        "released": len(contact_ids_to_release),
        "not_found": not_found,
        "out_of_scope": out_of_scope,
        "by_status": by_status_count,
        "bucket_updates": bucket_updates,
        "touched_bucket_ids": sorted(touched_bucket_ids),
    }


async def _run_release_ids_job(
    job_id: str,
    contact_ids: list[str],
    scope_assignment_ids: set[str] | None,
    release_batch_id: str,
) -> None:
    """Background worker for large by-id selections. Mirrors _run_release_job."""
    job = _RELEASE_JOBS[job_id]
    touched: set[str] = set()
    try:
        for i in range(0, len(contact_ids), _RELEASE_JOB_CHUNK):
            chunk = contact_ids[i : i + _RELEASE_JOB_CHUNK]
            for attempt in range(3):
                try:
                    async with AsyncSessionLocal() as db:
                        res = await _release_ids_chunk(
                            db, chunk, scope_assignment_ids, release_batch_id,
                            datetime.now(timezone.utc),
                            reconcile_buckets=False,
                        )
                        await db.commit()
                    job["released"] += res["released"]
                    job["not_found"].extend(res["not_found"])
                    job["out_of_scope"].extend(res["out_of_scope"])
                    job["by_status"]["assigned"] += res["by_status"]["assigned"]
                    job["by_status"]["used"] += res["by_status"]["used"]
                    touched.update(res["touched_bucket_ids"])
                    break
                except Exception as exc:
                    if attempt < 2 and _is_retryable_db_error(exc):
                        await asyncio.sleep(1 + 2 * attempt)
                        continue
                    raise
            job["done"] = min(i + len(chunk), job["total"])
        job["status"] = "done"
    except Exception as exc:
        logger.exception(
            "Release-by-id job %s failed at %s/%s", job_id, job["done"], job["total"]
        )
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        for attempt in range(3):
            if not touched:
                break
            try:
                async with AsyncSessionLocal() as db:
                    job["bucket_updates"] = await reconcile_bucket_remaining(db, touched)
                    await db.commit()
                break
            except Exception as exc:
                if attempt < 2 and _is_retryable_db_error(exc):
                    await asyncio.sleep(1 + 2 * attempt)
                    continue
                logger.exception(
                    "Release-by-id job %s: bucket reconcile failed for %d bucket(s)",
                    job_id, len(touched),
                )
                break
        from api.routers.outreach.buckets import invalidate_eligible_cache
        invalidate_eligible_cache()
        job["_ts"] = datetime.now(timezone.utc).timestamp()
        _active_release_tasks.pop(job_id, None)


@router.post("/contacts/releases", status_code=201)
async def release_contacts_by_id(
    body: ReleaseByIdRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Release a set of contacts (by id) back to `available`.

    Used by the per-assignment / per-group contacts pages where the operator
    selects rows directly. Same revert + audit-log + bucket-reconcile pipeline
    as the email-based endpoint above. Contacts can span multiple webinars and
    assignments — each contact is logged against its current webinar.

    Selections up to _RELEASE_SYNC_LIMIT are released inline, as before. Larger
    ones return immediately with a job and are worked through in the background,
    committing one chunk at a time — the same protection the CSV path already
    had. Without it a big selection ran the whole pipeline, bucket recount
    included, inside one request transaction: blowing the 120s statement cap
    rolled the entire release back and the operator saw "released 0".
    """
    # Dedup, preserve order
    seen: set[str] = set()
    contact_ids = [c for c in body.contact_ids if c and not (c in seen or seen.add(c))]
    if not contact_ids:
        raise HTTPException(400, "No contact_ids provided")

    scope_assignment_ids: set[str] | None = (
        set(body.assignment_ids) if body.assignment_ids else None
    )
    release_batch_id = body.release_batch_id or str(uuid.uuid4())

    if len(contact_ids) > _RELEASE_SYNC_LIMIT:
        job = _spawn_release_ids_job(
            contact_ids, scope_assignment_ids, release_batch_id
        )
        return {
            "release_batch_id": release_batch_id,
            "released": 0,
            "not_found": [],
            "already_available": [],
            "out_of_scope": [],
            "by_status": {"assigned": 0, "used": 0},
            "bucket_updates": {},
            "job": _release_job_public(job),
        }

    res = await _release_ids_chunk(
        db, contact_ids, scope_assignment_ids, release_batch_id,
        datetime.now(timezone.utc),
    )

    # Remaining counts changed — drop the eligible-counts micro-cache.
    from api.routers.outreach.buckets import invalidate_eligible_cache
    invalidate_eligible_cache()
    return {
        "release_batch_id": release_batch_id,
        "released": res["released"],
        "not_found": res["not_found"],
        # Always empty on this path — a contact id that resolves to no membership
        # is either not_found or out_of_scope. Kept so the response shape matches
        # the email endpoint's.
        "already_available": [],
        "out_of_scope": res["out_of_scope"],
        "by_status": res["by_status"],
        "bucket_updates": res["bucket_updates"],
        "job": None,
    }
