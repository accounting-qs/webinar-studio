"""GoHighLevel API v2 client — auth, pagination, rate limiting.

Scoped to a single location. Used for syncing contacts and opportunities
into the local DB for the Statistics dashboard.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

import httpx
from sqlalchemy import select

from config import settings
from db.models import ConnectorCredential
from db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

GHL_PROVIDER = "ghl"

# Pagination knobs are intentionally hardcoded — they're tuned for GHL's
# v2 API limits (contacts/search caps at 500 per page) and we never want
# them changed at runtime.
#
# Rate limit budget: GHL sub-account v2 burst is 100 req / 10s = 10 req/s.
# Each page response itself takes 200-500ms, so the natural cadence is
# already 2-5 req/s. The inter-page sleep adds margin against bursts when
# the server is fast. 50ms keeps us comfortably under the limit (~7 req/s
# worst case) without leaving free throughput on the table.
GHL_PAGE_SIZE = 500
GHL_PAGE_DELAY_S = 0.05

# Retry config for transient HTTP errors (5xx, 429, network timeouts).
# A single mid-stream blip used to fail an entire 200k-row deep sync;
# this turns those into a brief pause and a continued stream.
_RETRY_MAX_ATTEMPTS = 4
_RETRY_BASE_DELAY_S = 1.0  # 1s, 2s, 4s, 8s


async def get_ghl_credentials() -> tuple[str, str, str | None]:
    """Resolve GHL credentials with DB-first, env-fallback semantics.

    Returns (api_key, location_id, pipeline_id). All three are configurable
    via the Connectors tab; pipeline_id is optional and falls back to env
    if the DB row doesn't have it set.

    Raises RuntimeError if neither source has the required (api_key,
    location_id) pair.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ConnectorCredential).where(ConnectorCredential.provider == GHL_PROVIDER)
        )
        cred = result.scalar_one_or_none()
        if cred and cred.api_key and cred.location_id:
            pipeline = cred.pipeline_id or settings.GHL_PIPELINE_ID
            return cred.api_key, cred.location_id, pipeline

    if settings.GHL_API_KEY and settings.GHL_LOCATION_ID:
        return settings.GHL_API_KEY, settings.GHL_LOCATION_ID, settings.GHL_PIPELINE_ID

    raise RuntimeError(
        "GHL not configured. Add API key + location ID in the Connectors tab "
        "(or set GHL_API_KEY and GHL_LOCATION_ID env vars)."
    )


async def get_ghl_location_id() -> str | None:
    """Return the configured GHL location id, or None if unconfigured.
    Used by non-sync code (e.g. statistics URL builders) that just needs
    to render links and shouldn't crash if GHL isn't connected.
    """
    try:
        _, location_id, _ = await get_ghl_credentials()
        return location_id
    except RuntimeError:
        return None


async def verify_credentials(api_key: str, location_id: str) -> tuple[bool, str | None]:
    """Verify a candidate (api_key, location_id) pair against GHL.

    Returns (ok, error). On 200, ok=True. On 401/403 or 404 we treat the
    credentials as bad and return ok=False with a helpful message. Network
    errors propagate as ok=False with the error string — the caller should
    surface that to the user without persisting the credentials.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Accept": "application/json",
    }
    url = f"{settings.GHL_API_BASE_URL}/locations/{location_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Network error contacting GHL: {exc}"

    if r.status_code == 200:
        return True, None
    if r.status_code in (401, 403):
        return False, "API key rejected by GHL (unauthorized)"
    if r.status_code == 404:
        return False, "Location ID not found for this API key"
    return False, f"GHL responded with HTTP {r.status_code}"


# Opportunity custom field IDs
OPP_FIELD_WEBINAR_SOURCE_NUMBER = "gp70TwLRM9Tnsfr7FR9Y"
OPP_FIELD_LEAD_QUALITY = "M8RuTSXsLhZMvdMWAlLr"
OPP_FIELD_PROJECTED_DEAL_SIZE = "Oo9ktilF7QwTNBzksT3k"

# Call appointment fields (from reference project — used for shows / no-shows)
OPP_FIELD_CALL1_APPT_STATUS = "V82ErbW24izA5aQUzRUv"
OPP_FIELD_CALL1_APPT_DATE = "bFDWu3koncdxn26h6nAm"

# Contact custom field IDs — primary signals
CONTACT_FIELD_CALENDAR_INVITE_RESPONSE_HISTORY = "ghPIByTtKxRmHveNu4b1"
CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_HISTORY = "6YyME5pcbkr2zpxMHDPK"
CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_NON_JOINERS = "6TYlHOaOXS2DWHH5kR8D"
CONTACT_FIELD_BOOKED_CALL_WEBINAR_SERIES = "rsgthoV5ScH49VPFZlyq"
CONTACT_FIELD_IS_BOOKED_CALL = "wWkP8RfjazF5HdAzR9hA"
CONTACT_FIELD_WEBINAR_REGISTRATION_IN_FORM_DATE = "PUuRqljS3gWyBEmwBxwL"
CONTACT_FIELD_COLD_CALENDAR_UNSUBSCRIBE_DATE = "OLQt9nEWyG7tpYIdNs4F"

# Contact custom field IDs — fallback / auxiliary (all discovered from live location)
CONTACT_FIELD_INVITE_RESPONSE_PREFIX = "nFS2za5WnLXWmp55sWi1"
CONTACT_FIELD_INVITE_RESPONSE_PREFIX_NON_JOINERS = "SAiRDcopO9DhvokO2E8W"
CONTACT_FIELD_WEBINAR_REGISTRATION_NUMBER = "kJQewaxZUjDp83WCS0Fj"
CONTACT_FIELD_ZOOM_WEBINAR_SERIES_LATEST = "XH7sGVl71ZqO9xhEn4gg"
CONTACT_FIELD_ZOOM_WEBINAR_SERIES_REG_COUNT = "IzPgZbcLiXTSmCz8LFGM"
CONTACT_FIELD_ZOOM_WEBINAR_SERIES_ATTENDED_COUNT = "5hubKUb30XFSKHLBZzje"
CONTACT_FIELD_ZOOM_TIME_IN_SESSION_MINUTES = "hiADWfGo1jeaMIhzM97o"
CONTACT_FIELD_ZOOM_VIEWING_TIME_IN_MINUTES = "dfAAxcYN08ZQ09wTtQ8N"
CONTACT_FIELD_ZOOM_ATTENDED = "ycQkLxVLljWmk7qAZRRR"
CONTACT_FIELD_BOOK_CAMPAIGN_SOURCE = "TBPA9KJhtSV9bWxLKrXm"
CONTACT_FIELD_BOOK_CAMPAIGN_MEDIUM = "iNpb0QADMehVmkdePLVF"
CONTACT_FIELD_BOOK_CAMPAIGN_NAME = "j5P9np8IegTDp5HsJgc9"
CONTACT_FIELD_BOOK_CAMPAIGN_CONTENT = "33Xi2sTreZQFa6P3yeDY"
CONTACT_FIELD_BOOK_CAMPAIGN_TERM = "zgNYtkGETYNsAgh9zG18"
CONTACT_FIELD_BOOK_CAMPAIGN_ID = "oJ2WFYgSyMGwOl9Sqxt3"
CONTACT_FIELD_REGISTRATION_CAMPAIGN_SOURCE = "J2DrQ8FJ1i0yIDnZr7BD"
CONTACT_FIELD_REGISTRATION_CAMPAIGN_MEDIUM = "T5F3y5f5rjOqGELX84CB"
CONTACT_FIELD_REGISTRATION_CAMPAIGN_NAME = "M9B6aeMQ5243R3GJZNit"

# Tag we need to preserve as a boolean on the contact
SMS_CLICK_TAG = "webinar reminder sms clicked"


class GHLClient:
    """Async GHL API v2 client."""

    def __init__(
        self,
        api_key: str,
        location_id: str,
        pipeline_id: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Version": "2021-07-28",
            "Accept": "application/json",
        }
        self._base_url = base_url or settings.GHL_API_BASE_URL
        self._location_id = location_id
        self._pipeline_id = pipeline_id
        self._page_delay_s = GHL_PAGE_DELAY_S
        self._page_size = GHL_PAGE_SIZE

    @classmethod
    async def create(cls) -> "GHLClient":
        """Construct a client by resolving credentials from DB (Connectors
        tab) with env-var fallback. Raises RuntimeError if neither source
        provides a valid (api_key, location_id) pair — which the sync
        lifecycle catches and finalizes the run as failed.
        """
        api_key, location_id, pipeline_id = await get_ghl_credentials()
        return cls(api_key=api_key, location_id=location_id, pipeline_id=pipeline_id)

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict:
        """HTTP request with retry on 429 / 5xx / network errors.

        Retries up to _RETRY_MAX_ATTEMPTS times with exponential backoff.
        For 429, honours the Retry-After header if present. 4xx (other
        than 429) raises immediately — those are bugs, not transient.
        """
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            try:
                if method == "GET":
                    response = await client.get(url, headers=self._headers, params=params)
                else:
                    response = await client.post(url, headers=self._headers, json=json)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                if attempt == _RETRY_MAX_ATTEMPTS - 1:
                    raise
                delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                logger.warning("GHL %s %s network error (attempt %d/%d): %s — retrying in %.1fs",
                               method, path, attempt + 1, _RETRY_MAX_ATTEMPTS, exc, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code < 400:
                return response.json()
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == _RETRY_MAX_ATTEMPTS - 1:
                    response.raise_for_status()
                # Honour Retry-After if present, otherwise exponential backoff
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else _RETRY_BASE_DELAY_S * (2 ** attempt)
                except ValueError:
                    delay = _RETRY_BASE_DELAY_S * (2 ** attempt)
                logger.warning("GHL %s %s HTTP %d (attempt %d/%d) — retrying in %.1fs",
                               method, path, response.status_code, attempt + 1, _RETRY_MAX_ATTEMPTS, delay)
                await asyncio.sleep(delay)
                continue
            # 4xx (not 429) — don't retry. Include the response body in the
            # raised error so the sync's error_details JSONB captures what
            # GHL actually said (e.g. "filters[1].field is required"), not
            # just the bare status code.
            body_preview = response.text[:500] if response.text else ""
            raise RuntimeError(
                f"GHL {method} {path} HTTP {response.status_code}: {body_preview}"
            )
        # Unreachable: loop either returns or raises
        raise RuntimeError(f"GHL request exhausted retries: {last_exc}")

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        return await self._request_with_retry(client, "GET", path, params=params)

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict) -> dict:
        return await self._request_with_retry(client, "POST", path, json=body)

    # ------------------------------------------------------------------
    # Opportunities
    # ------------------------------------------------------------------

    async def stream_opportunities(
        self, updated_after: datetime | None = None
    ) -> AsyncGenerator[dict, None]:
        """Yield raw GHL opportunity dicts one at a time, handling pagination.

        updated_after: if set, only fetch opportunities updated after this ts.
        """
        if not self._pipeline_id:
            raise RuntimeError("GHL_PIPELINE_ID not configured")

        # /opportunities/search caps at limit=100 (returns 400 otherwise);
        # /contacts/search accepts up to 500.
        opp_page_size = min(self._page_size, 100)
        params: dict = {
            "location_id": self._location_id,
            "pipeline_id": self._pipeline_id,
            "limit": opp_page_size,
        }
        if updated_after:
            params["startAfter"] = int(updated_after.timestamp() * 1000)

        cursor_id: str | None = None
        cursor_after: int | None = None
        page = 0

        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                page += 1
                page_params = dict(params)
                if cursor_id and cursor_after is not None:
                    page_params["startAfterId"] = cursor_id
                    page_params["startAfter"] = cursor_after

                data = await self._get(client, "/opportunities/search", page_params)
                opps = data.get("opportunities", [])
                if not opps:
                    break

                for opp in opps:
                    yield opp

                meta = data.get("meta", {})
                total = meta.get("total", 0)
                fetched = (page - 1) * opp_page_size + len(opps)
                logger.info("GHL: fetched %d / %d opportunities", fetched, total)

                cursor_id = meta.get("startAfterId")
                cursor_after = meta.get("startAfter")

                if not cursor_id or fetched >= total:
                    break

                await asyncio.sleep(self._page_delay_s)

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    # GHL filter presets ---------------------------------------------------

    @staticmethod
    def narrow_webinar_filter() -> list[dict]:
        """OR of narrow webinar custom fields — excludes the huge
        calendar_webinar_series_history field (4.2M rows).

        Captures contacts who: responded Yes/Maybe, are non-joiners, have
        booked a call, self-registered, unsubscribed, or have any
        registration/zoom signal.
        """
        return [{"group": "OR", "filters": [
            {"field": f"customFields.{CONTACT_FIELD_CALENDAR_INVITE_RESPONSE_HISTORY}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_NON_JOINERS}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_BOOKED_CALL_WEBINAR_SERIES}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_IS_BOOKED_CALL}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_WEBINAR_REGISTRATION_IN_FORM_DATE}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_COLD_CALENDAR_UNSUBSCRIBE_DATE}", "operator": "exists"},
            # Fallback signals — narrower variants of the above
            {"field": f"customFields.{CONTACT_FIELD_INVITE_RESPONSE_PREFIX}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_INVITE_RESPONSE_PREFIX_NON_JOINERS}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_WEBINAR_REGISTRATION_NUMBER}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_ZOOM_ATTENDED}", "operator": "exists"},
            {"field": f"customFields.{CONTACT_FIELD_ZOOM_WEBINAR_SERIES_LATEST}", "operator": "exists"},
        ]}]

    @staticmethod
    def webinar_number_filter(webinar_number: int, deep: bool = False) -> list[dict]:
        """OR across fields that reference a webinar number for the given N.

        Fast mode (default, deep=False): narrow fields only — invite response,
        non-joiners, booked_call_webinar_series. ~1500 contacts for W136.

        Deep mode (deep=True): also includes calendar_webinar_series_history
        (contains eN), which expands to ~200k contacts for a typical webinar.
        We don't actually need those rows — the count is captured via
        count_contacts_with_filter() and stored in ghl_webinar_stats.
        """
        tok = f"e{webinar_number}"
        filters = [
            {"field": f"customFields.{CONTACT_FIELD_CALENDAR_INVITE_RESPONSE_HISTORY}", "operator": "contains", "value": tok},
            {"field": f"customFields.{CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_NON_JOINERS}", "operator": "contains", "value": tok},
            {"field": f"customFields.{CONTACT_FIELD_BOOKED_CALL_WEBINAR_SERIES}", "operator": "eq", "value": webinar_number},
        ]
        if deep:
            filters.insert(0, {
                "field": f"customFields.{CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_HISTORY}",
                "operator": "contains", "value": tok,
            })
        return [{"group": "OR", "filters": filters}]

    @staticmethod
    def gcal_invited_count_filter(webinar_number: int) -> list[dict]:
        """Filter that matches contacts whose calendar_webinar_series_history
        contains eN — i.e., everyone invited to webinar N via GCal.

        Used with count_contacts_with_filter() to get gcal_invited_count
        without syncing the ~200k matching rows.
        """
        return [{
            "field": f"customFields.{CONTACT_FIELD_CALENDAR_WEBINAR_SERIES_HISTORY}",
            "operator": "contains",
            "value": f"e{webinar_number}",
        }]

    async def count_contacts_with_filter(self, filters: list[dict]) -> int:
        """Return the total count matching a filter in a single request.

        Uses pageLimit=1 so GHL only returns one contact but populates `total`.
        Perfect for getting gcal_invited_count without syncing the rows.
        """
        body: dict = {
            "locationId": self._location_id,
            "pageLimit": 1,
            "filters": filters,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            data = await self._post(client, "/contacts/search", body)
        return int(data.get("total") or 0)

    async def stream_contacts(
        self,
        updated_after: datetime | None = None,
        filters: list[dict] | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Yield raw GHL contact dicts. Uses POST /contacts/search with cursor paging.

        updated_after: if set, narrows results to contacts touched after this ts.
        filters: extra filter clauses ANDed with the dateUpdated filter.
                 Use narrow_webinar_filter() or webinar_number_filter(n).
        """
        body: dict = {
            "locationId": self._location_id,
            "pageLimit": self._page_size,
        }

        # GHL /contacts/search rejects the request with 422 if the top-level
        # `filters` array mixes shapes (e.g. a group object alongside a flat
        # filter object). Incremental sync hits this because the narrow
        # webinar filter is a single OR-group and we used to append a flat
        # `dateUpdated gt` filter next to it. Wrap them together in an AND
        # group so the top level is uniformly group-shaped.
        #
        # `dateUpdated` only accepts the Smart-List operator vocabulary
        # (range/not_range/last/next/eq/...); `gt` was rejected with 422
        # after a GHL API change. `range` with an open-ended `{gte}` value
        # is the documented way to express "updated after X".
        date_filter: dict | None = None
        if updated_after:
            date_filter = {
                "field": "dateUpdated",
                "operator": "range",
                "value": {"gte": int(updated_after.timestamp() * 1000)},
            }

        if filters and date_filter:
            body["filters"] = [{"group": "AND", "filters": [*filters, date_filter]}]
        elif filters:
            body["filters"] = list(filters)
        elif date_filter:
            body["filters"] = [date_filter]

        search_after: list | None = None
        page = 0

        async with httpx.AsyncClient(timeout=60.0) as client:
            while True:
                page += 1
                req = dict(body)
                if search_after is not None:
                    req["searchAfter"] = search_after

                data = await self._post(client, "/contacts/search", req)
                contacts = data.get("contacts", [])
                if not contacts:
                    break

                for c in contacts:
                    yield c

                total = data.get("total", 0)
                logger.info(
                    "GHL: fetched %d contacts (page %d, total %d)",
                    len(contacts), page, total,
                )

                # Cursor lives on the last contact as `searchAfter`
                last = contacts[-1]
                search_after = last.get("searchAfter")
                if not search_after:
                    break

                await asyncio.sleep(self._page_delay_s)


_CUSTOM_FIELD_VALUE_KEYS = (
    # /contacts/search and /opportunities/{id}
    "fieldValue", "value",
    # /opportunities/search uses typed-by-shape keys; first non-null wins.
    # fieldValueDate is Unix milliseconds — _parse_dt accepts ints.
    "fieldValueString", "fieldValueDate", "fieldValueArray", "fieldValueNumber",
)


def parse_custom_fields(raw: list[dict] | None) -> dict[str, object]:
    """Convert GHL `customFields` array to {fieldId: value} dict.

    GHL uses different value-key shapes per endpoint:
      - /contacts/search:        {"id": ..., "value": X}
      - /opportunities/{id}:     {"id": ..., "fieldValue": X}
      - /opportunities/search:   {"id": ..., "fieldValueString": ..., "type": ...}
                                 or fieldValueDate (epoch-ms int) / fieldValueArray
                                 / fieldValueNumber depending on the field's type.
    We accept all of them; first non-null wins so the dropped-shape variants
    don't blow away a populated value.
    """
    out: dict[str, object] = {}
    if not raw:
        return out
    for item in raw:
        fid = item.get("id")
        if not fid:
            continue
        val: object = None
        for k in _CUSTOM_FIELD_VALUE_KEYS:
            v = item.get(k)
            if v is not None:
                val = v
                break
        out[fid] = val
    return out


def parse_webinar_source_number(value: object) -> int | None:
    """Parse the Webinar Source Number v2 text value into an int.

    GHL stores it as TEXT even though the values are numeric. Handles strings
    like "136", "136.0", " 136 ". Returns None for unparseable values.
    """
    if value is None:
        return None
    try:
        s = str(value).strip()
        if not s:
            return None
        return int(float(s))
    except (ValueError, TypeError):
        return None


# Projected Deal Size dropdown → numeric value (single number, not a range)
PROJECTED_DEAL_SIZE_VALUES: dict[str, int] = {
    "7,700": 7700,
    "15,000": 15000,
    "20,000": 20000,
    "25,000": 25000,
    # Also handle comma-less variants defensively
    "7700": 7700,
    "15000": 15000,
    "20000": 20000,
    "25000": 25000,
}


def parse_projected_deal_size(option: object) -> int | None:
    """Convert the Projected Deal Size dropdown option string to its numeric value."""
    if option is None:
        return None
    key = str(option).strip()
    return PROJECTED_DEAL_SIZE_VALUES.get(key)
