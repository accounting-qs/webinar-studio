"""Zoom API client (Server-to-Server OAuth).

Base URL: https://api.zoom.us/v2
Auth:     POST https://zoom.us/oauth/token
          Basic base64(client_id:client_secret)
          grant_type=account_credentials&account_id=<id>
          -> bearer token, expires_in 3600, no refresh token

Deliberately NOT a copy of webinargeek_client.py. Three things differ and each
one is load-bearing:

1. Tokens. WebinarGeek takes a static Api-Token header; Zoom needs a minted
   bearer that expires hourly. Cached in memory (see _token_cache).
2. Pagination. WebinarGeek pages with page/per_page; Zoom uses an opaque
   next_page_token cursor, so _paged() from that module cannot express it.
3. Rate limits. WebinarGeek has none worth handling; Zoom enforces per-second
   and daily caps and the /report/* endpoints sit in the "Heavy" tier. 429s
   carry Retry-After (daily) or X-RateLimit-Reset (per-second).

Endpoints used:
  GET /users                                   hosts on the account
  GET /users/{id}/webinars?type=scheduled|past webinars per host
  GET /webinars/{id}                           detail + occurrences[]
  GET /webinars/{id}/registrants               who signed up
  GET /past_webinars/{id}/instances            occurrence -> instance UUID
  GET /past_webinars/{id}/absentees            registered, did not attend
  GET /report/webinars/{uuid}/participants     attendance + watch duration

Concurrency note: the token cache assumes multiple simultaneously-valid S2S
tokens, which is current Zoom behaviour (minting a new one does not invalidate
the old). That holds for a single-process service. If this ever runs under more
than one uvicorn worker and random 401s appear, that assumption broke — the fix
is moving the token into connector_credentials behind SELECT ... FOR UPDATE.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"

# Zoom's documented maximum for the list endpoints.
PAGE_SIZE = 300

# Re-mint this long before expiry so a long sync never crosses the boundary
# mid-request.
TOKEN_SKEW_SECONDS = 300

MAX_RETRIES = 3
RETRY_AFTER_CAP_SECONDS = 60

# Guard against a pathological cursor loop.
MAX_PAGES = 500


class ZoomError(Exception):
    """Any non-retryable failure talking to Zoom."""


class ZoomAuthError(ZoomError):
    """Credentials rejected — bad client id/secret/account, or app not activated."""


class ZoomScopeError(ZoomError):
    """Authenticated, but the app is missing a scope for this call.

    Zoom reports this as HTTP 400 with code 4711 (not 403), and helpfully names
    the scopes it wanted. Those are parsed out and carried on `missing_scopes`
    so the UI can tell the user exactly what to add rather than showing a raw
    error — a missing scope is by far the most common setup mistake.
    """

    def __init__(self, path: str, detail: str = "", missing_scopes: Optional[list[str]] = None) -> None:
        self.path = path
        self.detail = detail
        self.missing_scopes = missing_scopes or []
        if self.missing_scopes:
            wanted = " or ".join(self.missing_scopes)
            msg = f"Zoom denied {path}: the app is missing the scope {wanted}."
        else:
            msg = f"Zoom denied {path}: the app is missing a scope for this call. {detail}".strip()
        super().__init__(msg)


# Zoom: {"code":4711,"message":"Invalid access token, does not contain scopes:[a, b]."}
_MISSING_SCOPES_RE = re.compile(r"does not contain scopes\s*:\s*\[([^\]]*)\]", re.I)


def parse_missing_scopes(body: str) -> list[str]:
    """Pull the scope names out of Zoom's 4711 body.

    Parsing beats hardcoding an endpoint->scope map: Zoom states exactly what it
    wanted, so this stays correct even for calls added later.
    """
    m = _MISSING_SCOPES_RE.search(body or "")
    if not m:
        return []
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Token minting / caching
# ---------------------------------------------------------------------------
# {f"{account_id}:{client_id}": (access_token, expires_at_epoch)}
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock()


def _cache_key(account_id: str, client_id: str) -> str:
    return f"{account_id}:{client_id}"


def invalidate_token(account_id: str, client_id: str) -> None:
    _token_cache.pop(_cache_key(account_id, client_id), None)


async def _mint_token(account_id: str, client_id: str, client_secret: str) -> tuple[str, float]:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "account_credentials", "account_id": account_id},
            timeout=20,
        )
    if resp.status_code in (400, 401):
        raise ZoomAuthError(
            "Zoom rejected the credentials. Check Account ID / Client ID / Client Secret, "
            "and that the Server-to-Server OAuth app is Activated."
        )
    if resp.status_code != 200:
        raise ZoomError(f"Token request returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ZoomError("Token response contained no access_token")
    expires_at = time.time() + float(data.get("expires_in") or 3600)
    return token, expires_at


async def get_access_token(
    account_id: str, client_id: str, client_secret: str, *, force: bool = False
) -> str:
    key = _cache_key(account_id, client_id)
    if not force:
        cached = _token_cache.get(key)
        if cached and cached[1] - TOKEN_SKEW_SECONDS > time.time():
            return cached[0]

    async with _token_lock:
        # Re-check inside the lock: a burst of concurrent callers should mint once.
        if not force:
            cached = _token_cache.get(key)
            if cached and cached[1] - TOKEN_SKEW_SECONDS > time.time():
                return cached[0]
        token, expires_at = await _mint_token(account_id, client_id, client_secret)
        _token_cache[key] = (token, expires_at)
        return token


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------
def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429, per Zoom's documented headers."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after:
        try:
            # Documented as seconds for the per-second cap and as an ISO-8601
            # datetime when a daily cap is hit.
            return min(float(retry_after), RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            try:
                when = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
                delta = (when - datetime.now(timezone.utc)).total_seconds()
                return max(0.0, min(delta, RETRY_AFTER_CAP_SECONDS))
            except ValueError:
                pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, min(float(reset) - time.time(), RETRY_AFTER_CAP_SECONDS))
        except ValueError:
            pass
    return min(2.0 ** attempt, RETRY_AFTER_CAP_SECONDS)


class ZoomSession:
    """Holds one account's credentials and issues authenticated GETs."""

    def __init__(self, account_id: str, client_id: str, client_secret: str) -> None:
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret

    async def _token(self, force: bool = False) -> str:
        return await get_access_token(
            self.account_id, self.client_id, self.client_secret, force=force
        )

    async def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        *,
        allow_404: bool = False,
    ) -> Optional[dict[str, Any]]:
        """GET one page. Returns None on a tolerated 404."""
        refreshed = False
        for attempt in range(MAX_RETRIES):
            token = await self._token(force=refreshed)
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{BASE_URL}{path}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    params=params,
                    timeout=60,
                )

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 401 and not refreshed:
                # Clock skew or a revoked token: re-mint once, then retry.
                invalidate_token(self.account_id, self.client_id)
                refreshed = True
                continue

            if resp.status_code == 401:
                raise ZoomAuthError("Zoom rejected the access token (401) after a refresh.")

            if resp.status_code in (400, 403):
                # Zoom returns missing-scope as 400/code 4711, not 403. Treating
                # it as a generic failure reported a setup problem as a network
                # problem, so check for it on both.
                body = resp.text or ""
                missing = parse_missing_scopes(body)
                if missing or resp.status_code == 403 or '"code":4711' in body.replace(" ", ""):
                    raise ZoomScopeError(path, body[:300], missing)
                raise ZoomError(f"{path} returned {resp.status_code}: {body[:200]}")

            if resp.status_code == 404:
                if allow_404:
                    return None
                raise ZoomError(f"{path} returned 404: {resp.text[:200]}")

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    raise ZoomError(f"{path} returned {resp.status_code} after {MAX_RETRIES} attempts")
                delay = _retry_delay(resp, attempt)
                logger.warning(
                    "Zoom %s -> %s, retrying in %.1fs (attempt %d/%d)",
                    path, resp.status_code, delay, attempt + 1, MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            raise ZoomError(f"{path} returned {resp.status_code}: {resp.text[:200]}")

        raise ZoomError(f"{path} exhausted retries")

    async def paged(
        self,
        path: str,
        items_key: str,
        params: Optional[dict[str, Any]] = None,
        *,
        allow_404: bool = False,
    ) -> list[dict[str, Any]]:
        """Walk a next_page_token cursor to exhaustion."""
        out: list[dict[str, Any]] = []
        base = dict(params or {})
        token: Optional[str] = None
        for page in range(MAX_PAGES):
            page_params = {**base, "page_size": PAGE_SIZE}
            if token:
                # Zoom requires the other params stay identical across a cursor walk.
                page_params["next_page_token"] = token
            data = await self.get(path, page_params, allow_404=allow_404)
            if data is None:
                return out
            out.extend(data.get(items_key) or [])
            token = (data.get("next_page_token") or "").strip() or None
            if not token:
                return out
            if page == MAX_PAGES - 1:
                logger.warning("Zoom pagination hit MAX_PAGES for %s", path)
        return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def encode_uuid(uuid: str) -> str:
    """Path-encode a webinar instance UUID.

    Zoom requires DOUBLE encoding when the UUID starts with '/' or contains
    '//'. Getting this wrong does not error — it resolves to a different (or
    missing) instance, so a subset of webinars silently returns 404 or the
    wrong participants.
    """
    once = quote(uuid, safe="")
    if uuid.startswith("/") or "//" in uuid:
        return quote(once, safe="")
    return once


async def check_connection(
    account_id: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    """Mint a token, then probe the calls the sync actually makes.

    Reports credentials and scopes SEPARATELY, because they fail for different
    reasons and have different fixes:

    - Minting a token exercises all three values at once (account id, client id,
      client secret). If it succeeds, all three are correct — there is no way for
      one to be wrong and the mint to still work. That is what lets the UI say
      which half of the setup is done.
    - A scope problem happens only after a good token, so it can never be
      confused with a bad credential.

    Probes `/users` rather than `/users/me`: listing hosts is what
    refresh_webinars genuinely needs, so verifying it proves something useful,
    and it avoids requiring `user:read:user:admin` for a health check alone.
    """
    out: dict[str, Any] = {
        "credentials_ok": False,
        "credential_error": None,
        "account_email": None,
        "checks": [],
        "missing_scopes": [],
        "ok": False,
    }

    session = ZoomSession(account_id, client_id, client_secret)
    try:
        # Force a fresh mint so a cached token cannot mask bad credentials.
        await session._token(force=True)
        out["credentials_ok"] = True
    except ZoomError as e:
        out["credential_error"] = str(e)
        return out

    async def probe(name: str, endpoint: str, params: Optional[dict[str, Any]] = None):
        entry = {"name": name, "endpoint": endpoint, "ok": False,
                 "missing_scopes": [], "error": None}
        try:
            data = await session.get(endpoint, params)
            entry["ok"] = True
            out["checks"].append(entry)
            return data
        except ZoomScopeError as e:
            entry["missing_scopes"] = e.missing_scopes
            entry["error"] = str(e)
        except ZoomError as e:
            entry["error"] = str(e)
        out["checks"].append(entry)
        return None

    users = await probe("List hosts on the account", "/users", {"page_size": 1})
    if users:
        rows = users.get("users") or []
        if rows:
            out["account_email"] = rows[0].get("email")
            uid = rows[0].get("id")
            if uid:
                await probe(
                    "List that host's webinars",
                    f"/users/{uid}/webinars",
                    {"type": "scheduled", "page_size": 1},
                )

    seen: list[str] = []
    for c in out["checks"]:
        for s in c["missing_scopes"]:
            if s not in seen:
                seen.append(s)
    out["missing_scopes"] = seen
    out["ok"] = out["credentials_ok"] and all(c["ok"] for c in out["checks"]) and bool(out["checks"])
    return out


async def list_users(session: ZoomSession) -> list[dict[str, Any]]:
    """Active hosts on the account.

    Server-to-Server OAuth has no "me" in the user sense — /users/me is the app
    owner, but webinars may be hosted by anyone on the account, so the webinar
    list has to be assembled per host.
    """
    return await session.paged("/users", "users", {"status": "active"})


async def list_webinars(session: ZoomSession, user_id: str, kind: str = "scheduled") -> list[dict[str, Any]]:
    return await session.paged(
        f"/users/{user_id}/webinars", "webinars", {"type": kind}, allow_404=True
    )


async def get_webinar(session: ZoomSession, webinar_id: str) -> Optional[dict[str, Any]]:
    return await session.get(f"/webinars/{webinar_id}", allow_404=True)


async def list_registrants(
    session: ZoomSession,
    webinar_id: str,
    occurrence_id: Optional[str] = None,
    statuses: tuple[str, ...] = ("approved", "pending"),
) -> list[dict[str, Any]]:
    """Registrants across the given statuses.

    'denied' is deliberately excluded — those people cannot attend, so counting
    them as registrations would understate every conversion rate.
    """
    out: list[dict[str, Any]] = []
    for status in statuses:
        params: dict[str, Any] = {"status": status}
        if occurrence_id:
            params["occurrence_id"] = occurrence_id
        rows = await session.paged(
            f"/webinars/{webinar_id}/registrants", "registrants", params, allow_404=True
        )
        for r in rows:
            r["_status"] = status
        out.extend(rows)
    return out


async def list_past_instances(session: ZoomSession, webinar_id: str) -> list[dict[str, Any]]:
    data = await session.get(f"/past_webinars/{webinar_id}/instances", allow_404=True)
    return (data or {}).get("webinars") or []


async def list_absentees(
    session: ZoomSession, webinar_id_or_uuid: str
) -> list[dict[str, Any]]:
    return await session.paged(
        f"/past_webinars/{encode_uuid(webinar_id_or_uuid)}/absentees",
        "registrants",
        allow_404=True,
    )


async def list_participants(session: ZoomSession, instance_uuid: str) -> Optional[list[dict[str, Any]]]:
    """Participant report for one past instance.

    Returns None (not []) when the report does not exist yet — the caller must
    tell "nobody attended" apart from "Zoom has not generated this yet", or a
    premature sync would overwrite real attendance with zeros.
    """
    path = f"/report/webinars/{encode_uuid(instance_uuid)}/participants"
    out: list[dict[str, Any]] = []
    token: Optional[str] = None
    first = True
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"page_size": PAGE_SIZE, "include_fields": "registrant_id"}
        if token:
            params["next_page_token"] = token
        data = await session.get(path, params, allow_404=True)
        if data is None:
            return None if first else out
        first = False
        out.extend(data.get("participants") or [])
        token = (data.get("next_page_token") or "").strip() or None
        if not token:
            return out
    return out


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def parse_dt(val: Any) -> Optional[datetime]:
    """Zoom sends ISO-8601 ('2026-09-22T14:00:00Z'); WebinarGeek sent epochs."""
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def merge_watch_seconds(segments: list[tuple[datetime, datetime]]) -> int:
    """Total distinct seconds covered by a set of (join, leave) intervals.

    Neither naive approach is correct:
      - SUM(duration) double-counts someone joined on two devices at once.
      - MAX(leave) - MIN(join) credits the time they were away between joins.

    Sorting and coalescing overlaps gives the honest figure, which matters
    because `minutes_viewing >= 10` and `>= 30` are hardcoded thresholds in
    several statistics queries.
    """
    clean = [(s, e) for s, e in segments if s and e and e > s]
    if not clean:
        return 0
    clean.sort(key=lambda p: p[0])
    total = timedelta()
    cur_start, cur_end = clean[0]
    for start, end in clean[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return int(total.total_seconds())


def participant_segment(p: dict[str, Any]) -> tuple[Optional[datetime], Optional[datetime], int]:
    """(join, leave, reported_duration_seconds) for one participant row.

    Zoom documents `duration` in seconds on this endpoint, but has returned
    minutes on sibling dashboard endpoints. Callers should cross-check against
    the join/leave delta rather than trusting it blindly — see
    duration_units_suspect().
    """
    join = parse_dt(p.get("join_time"))
    leave = parse_dt(p.get("leave_time"))
    try:
        reported = int(p.get("duration") or 0)
    except (TypeError, ValueError):
        reported = 0
    return join, leave, reported


def duration_units_suspect(reported_seconds: int, measured_seconds: int) -> bool:
    """True when Zoom's `duration` disagrees with join/leave by more than 2x.

    A silent seconds-vs-minutes mix-up would be a 60x error in minutes_viewing
    and would corrupt every 10/30-minute metric, so the sync logs loudly rather
    than quietly writing a wrong number.
    """
    if reported_seconds <= 0 or measured_seconds <= 0:
        return False
    ratio = max(reported_seconds, measured_seconds) / min(reported_seconds, measured_seconds)
    return ratio > 2.0
