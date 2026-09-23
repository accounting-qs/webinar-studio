"""
Skarpe MCP client.

Skarpe (calendar-invitation campaigns) exposes its API as MCP-over-HTTP:
JSON-RPC 2.0 tools/call POSTed to the workspace endpoint, answered as a
single SSE-framed event (`data: {...}`) or plain JSON. The server is
stateless — no initialize handshake or session id is needed for tools/call
(verified against staging).

Base URL is per-credential (staging vs production backend host), so every
function takes (base_url, api_key) explicitly — connector_credentials.base_url
carries it. One API key = one Skarpe workspace; no tool takes an account id.

Deliberately NOT implemented:
  - launch_campaign: sends real, unrecallable calendar invitations. Out of
    scope for the draft-creation feature; if it is ever added it needs its
    own human-confirmation flow.
  - list_campaigns: errors unconditionally on the staging backend as of
    2026-09-23. Campaign state comes from our skarpe_campaigns rows plus
    get_campaign by id.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
# add_campaign_contacts ingests up to CONTACT_CHUNK rows per call server-side.
CONTACT_PUSH_TIMEOUT = 120.0
# Verified fine on staging; kept modest so one failed call never re-sends much.
CONTACT_CHUNK = 500

MAX_RETRIES = 3


class SkarpeError(Exception):
    pass


class SkarpeAuthError(SkarpeError):
    """The API key was rejected (HTTP 401/403)."""


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # The server frames responses as SSE; it requires the stream type
        # to be acceptable even for single-shot calls.
        "Accept": "application/json, text/event-stream",
    }


def _parse_rpc_response(resp: httpx.Response, req_id: int) -> dict[str, Any]:
    """Extract the JSON-RPC envelope from an SSE-framed or plain-JSON body."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        candidates: list[str] = []
        # SSE: events separated by blank lines; each event's payload is the
        # concatenation of its `data:` lines. `event:`/comment lines and
        # keep-alives are skipped.
        current: list[str] = []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                current.append(line[5:].lstrip())
            elif not line.strip() and current:
                candidates.append("\n".join(current))
                current = []
        if current:
            candidates.append("\n".join(current))
        for payload in candidates:
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("jsonrpc") == "2.0" and obj.get("id") == req_id:
                return obj
        raise SkarpeError(f"No JSON-RPC response found in SSE body: {resp.text[:200]}")
    try:
        obj = resp.json()
    except ValueError:
        raise SkarpeError(f"Unparseable response: {resp.text[:200]}")
    if not isinstance(obj, dict):
        raise SkarpeError(f"Unexpected response shape: {resp.text[:200]}")
    return obj


async def _call_tool(
    base_url: str,
    api_key: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    client: Optional[httpx.AsyncClient] = None,
) -> Any:
    """One tools/call round trip. Returns the tool's JSON payload.

    Retries transport errors, 429 (honoring Retry-After) and 5xx with
    backoff. Tool-level failures (result.isError) are semantic, not
    transient — they raise without retry.
    """
    req_id = 1
    body = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }

    owns_client = client is None
    http = client or httpx.AsyncClient()
    try:
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await http.post(
                    base_url, headers=_headers(api_key), json=body, timeout=timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
                await asyncio.sleep(1 + 2 * attempt)
                continue

            if resp.status_code in (401, 403):
                raise SkarpeAuthError("Invalid Skarpe API key")
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("retry-after")
                try:
                    delay = float(retry_after) if retry_after else 1 + 2 * attempt
                except ValueError:
                    delay = 1 + 2 * attempt
                last_error = SkarpeError(
                    f"{tool} returned {resp.status_code}: {resp.text[:200]}"
                )
                await asyncio.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise SkarpeError(f"{tool} returned {resp.status_code}: {resp.text[:200]}")

            rpc = _parse_rpc_response(resp, req_id)
            if "error" in rpc:
                err = rpc["error"] or {}
                raise SkarpeError(f"{tool}: {err.get('message', 'JSON-RPC error')}")
            result = rpc.get("result") or {}
            if result.get("isError"):
                content = result.get("content") or []
                text = content[0].get("text", "") if content else ""
                raise SkarpeError(f"{tool} failed: {text or 'unknown tool error'}")
            structured = result.get("structuredContent")
            if structured is not None:
                # MCP wraps non-object tool outputs (lists) as {"result": ...}.
                if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
                    return structured["result"]
                return structured
            # Tools returning a list emit one content block per item, each
            # block's text being one JSON object.
            content = result.get("content") or []
            parsed = []
            for block in content:
                text = block.get("text")
                if text is None:
                    continue
                try:
                    parsed.append(json.loads(text))
                except ValueError:
                    parsed.append(text)
            if len(parsed) == 1:
                return parsed[0]
            return parsed
        raise SkarpeError(f"{tool} failed after {MAX_RETRIES} attempts: {last_error}")
    finally:
        if owns_client:
            await http.aclose()


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Public tools
# ---------------------------------------------------------------------------
async def whoami(base_url: str, api_key: str, *, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """{key_name, permissions, can_launch_campaigns, timezone}."""
    return await _call_tool(base_url, api_key, "whoami", {}, client=client)


async def verify_credentials(base_url: str, api_key: str) -> dict[str, Any]:
    """whoami as a save-time check. SkarpeAuthError on a bad key,
    SkarpeError when unreachable/broken."""
    return await whoami(base_url, api_key)


async def list_webinars(base_url: str, api_key: str, *, client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    """Workspace webinars, newest first, each with `platform` identity
    (zoom_webinar_id / webinargeek_broadcast_id) for matching ours."""
    result = await _call_tool(base_url, api_key, "list_webinars", {}, client=client)
    return result if isinstance(result, list) else [result]


async def list_sending_accounts(base_url: str, api_key: str, *, client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    result = await _call_tool(base_url, api_key, "list_sending_accounts", {}, client=client)
    return result if isinstance(result, list) else [result]


async def get_contact_policy(base_url: str, api_key: str, *, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """{policy_version, policy_hash, statements, links}. The statements must
    be shown verbatim to the user before any contact push."""
    return await _call_tool(base_url, api_key, "get_contact_policy", {}, client=client)


async def get_campaign(base_url: str, api_key: str, campaign_id: str, *, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    return await _call_tool(base_url, api_key, "get_campaign", {"campaign_id": campaign_id}, client=client)


async def create_campaign_draft(
    base_url: str,
    api_key: str,
    *,
    title: str,
    description: Optional[str] = None,
    webinar_number: Optional[int] = None,
    external_ref: Optional[str] = None,
    event_title: Optional[str] = None,
    event_description: Optional[str] = None,
    event_location: Optional[str] = None,
    event_start: Optional[str] = None,
    event_end: Optional[str] = None,
    event_timezone: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Create a draft (sends nothing). Reusing external_ref returns the
    existing draft instead of duplicating. event_timezone omitted → the
    workspace's own timezone applies."""
    args = _compact({
        "title": title,
        "description": description,
        "webinar_number": webinar_number,
        "external_ref": external_ref,
        "event_title": event_title,
        "event_description": event_description,
        "event_location": event_location,
        "event_start": event_start,
        "event_end": event_end,
        "event_timezone": event_timezone,
    })
    return await _call_tool(base_url, api_key, "create_campaign_draft", args, client=client)


async def update_campaign_draft(
    base_url: str,
    api_key: str,
    campaign_id: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    **fields: Any,
) -> dict[str, Any]:
    """Revise a draft's fields; only passed (non-None) fields change.
    Fails on Skarpe's side once the campaign has launched."""
    args = {"campaign_id": campaign_id, **_compact(fields)}
    return await _call_tool(base_url, api_key, "update_campaign_draft", args, client=client)


async def attach_sending_accounts(
    base_url: str,
    api_key: str,
    campaign_id: str,
    account_ids: list[str],
    daily_limit: Optional[int] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Attach mailboxes to a campaign. Does not send anything."""
    args = _compact({
        "campaign_id": campaign_id,
        "account_ids": account_ids,
        "daily_limit": daily_limit,
    })
    return await _call_tool(base_url, api_key, "attach_sending_accounts", args, client=client)


async def add_campaign_contacts(
    base_url: str,
    api_key: str,
    campaign_id: str,
    contacts: list[dict[str, Any]],
    *,
    policy_version: str,
    policy_hash: str,
    confirmed_by: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """Upload recipients into a campaign.

    `confirmed` is sent as true unconditionally, so the caller MUST have
    shown the live get_contact_policy statements to a human and received
    their explicit confirmation (with confirmed_by naming them) — the pair
    (policy_version, policy_hash) must come from that same fetch. Never call
    this on inferred consent. Contacts already on the campaign and suppressed
    addresses are skipped silently by Skarpe.
    """
    if not (policy_version and policy_hash and confirmed_by):
        raise SkarpeError("Contact push requires policy_version, policy_hash and confirmed_by")
    args = {
        "campaign_id": campaign_id,
        "contacts": contacts,
        "policy_version": policy_version,
        "policy_hash": policy_hash,
        "confirmed": True,
        "confirmed_by": confirmed_by,
    }
    return await _call_tool(
        base_url, api_key, "add_campaign_contacts", args,
        timeout=CONTACT_PUSH_TIMEOUT, client=client,
    )
