"""
WebinarGeek API v2 client.

Base URL: https://app.webinargeek.com/api/v2
Auth:     Api-Token: <key> header

Key endpoints:
  GET /webinars                                 → paginated, items under "webinars"
                                                  each item: episodes[].broadcasts[]
  GET /broadcasts/{id}                          → single broadcast record
  GET /subscriptions?broadcast_id={id}          → paginated, items under "subscriptions"
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://app.webinargeek.com/api/v2"
PAGE_SIZE = 100


class WebinarGeekError(Exception):
    pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Api-Token": api_key, "Accept": "application/json"}


async def _paged(
    client: httpx.AsyncClient,
    path: str,
    api_key: str,
    params: Optional[dict[str, Any]] = None,
    items_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1
    base_params = dict(params or {})
    while True:
        resp = await client.get(
            f"{BASE_URL}{path}",
            headers=_headers(api_key),
            params={**base_params, "page": page, "per_page": PAGE_SIZE},
            timeout=30,
        )
        if resp.status_code == 401:
            raise WebinarGeekError("Invalid API key")
        if resp.status_code != 200:
            raise WebinarGeekError(f"{path} returned {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            if items_key and items_key in data:
                items = data[items_key]
            else:
                list_keys = [k for k, v in data.items() if isinstance(v, list)]
                items = data[list_keys[0]] if list_keys else []
        else:
            items = []

        if not items:
            break
        results.extend(items)
        if len(items) < PAGE_SIZE:
            break
        page += 1
        if page > 500:
            logger.warning("WG pagination exceeded 500 pages for %s", path)
            break
    return results


async def verify_api_key(api_key: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{BASE_URL}/webinars",
            headers=_headers(api_key),
            params={"per_page": 1, "page": 1},
            timeout=15,
        )
        if resp.status_code == 401:
            return False
        if resp.status_code >= 400:
            raise WebinarGeekError(f"verify returned {resp.status_code}: {resp.text[:200]}")
        return True


async def list_webinars(api_key: str) -> list[dict[str, Any]]:
    """Fetch all webinars with embedded episodes + broadcasts."""
    async with httpx.AsyncClient() as client:
        return await _paged(client, "/webinars", api_key, items_key="webinars")


async def list_broadcasts(api_key: str) -> list[dict[str, Any]]:
    """
    Fetch all broadcasts directly from /broadcasts (flat, paginated).

    Requests nested_resources=episode,webinar so each broadcast embeds its
    parent `webinar` (id, title, internal_title) and `episode` — covering
    every broadcast including ended ones (unlike the windowed /webinars
    nesting). Read the webinar via webinar_meta_from_broadcast();
    build_broadcast_meta(list_webinars(...)) stays a fallback for any
    broadcast missing the embed.
    """
    async with httpx.AsyncClient() as client:
        return await _paged(
            client,
            "/broadcasts",
            api_key,
            params={"nested_resources": "episode,webinar"},
            items_key="broadcasts",
        )


async def list_subscriptions(api_key: str, broadcast_id: str | int) -> list[dict[str, Any]]:
    """All subscribers for one broadcast."""
    async with httpx.AsyncClient() as client:
        return await _paged(
            client,
            "/subscriptions",
            api_key,
            params={"broadcast_id": broadcast_id},
            items_key="subscriptions",
        )


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def unix_to_dt(val: Any) -> Optional[datetime]:
    if val is None or val == "":
        return None
    try:
        return datetime.fromtimestamp(int(val), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def build_broadcast_meta(webinars: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Walk webinars[].episodes[].broadcasts[] and return a
    {broadcast_id (str) → {webinar_id, webinar_title, internal_title}} map.

    Used to enrich the flat /broadcasts response with webinar metadata.
    """
    out: dict[str, dict[str, Any]] = {}
    for w in webinars:
        meta = {
            "webinar_id": w.get("id"),
            "webinar_title": w.get("title") or "",
            "internal_title": w.get("internal_title") or "",
        }
        for ep in w.get("episodes", []) or []:
            for b in ep.get("broadcasts", []) or []:
                bid = b.get("id")
                if bid is not None:
                    out[str(bid)] = meta
    return out


def webinar_meta_from_broadcast(b: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Extract {webinar_id, webinar_title, internal_title} from a broadcast's
    embedded `webinar` resource (present when list_broadcasts requests
    nested_resources=episode,webinar). Returns None when not embedded so
    callers can fall back to build_broadcast_meta().
    """
    w = b.get("webinar")
    if not isinstance(w, dict):
        return None
    return {
        "webinar_id": w.get("id"),
        "webinar_title": w.get("title") or "",
        "internal_title": w.get("internal_title") or "",
    }
