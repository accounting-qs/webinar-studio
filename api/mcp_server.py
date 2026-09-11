"""
Remote MCP server for Webinar Studio — mounted at POST /mcp on this same app.

WHAT THIS IS
    A Model Context Protocol endpoint that lets an external agent (Grok,
    Claude, any MCP client) read everything the Statistics / Planning /
    Contacts pages read, and build reports across those metrics — over HTTPS
    with a bearer token generated in Connectors → MCP.

WHY IT LOOKS LIKE THIS
    * Mounted inside this app, not a second service. One deploy, no new infra.
    * Every tool calls this app's OWN HTTP API over loopback (127.0.0.1:$PORT),
      never an internal function. So an MCP answer can never drift from what
      the UI gets: same routes, same validation, same caches. 127.0.0.1 and not
      "localhost" — the latter can resolve to ::1 and give ECONNREFUSED.
    * The JSON-RPC is hand-rolled. A client only ever sends five methods, and
      the official SDK's Node/ASGI transports hard-fail clients that don't send
      both `application/json` and `text/event-stream` in Accept. We
      content-negotiate instead, so every Accept combination works.
    * Stateless: no Mcp-Session-Id is ever issued. Hosts overlap instances on
      deploy, and a session pinned to a dead instance 404s mid-conversation
      with no recovery path for the agent.
    * Fails closed: no enabled connector row => 503 for everyone. A missing
      token can never mean an open endpoint.

GUARDRAILS
    xAI does not support MCP's `require_approval`, so there is no
    human-in-the-loop prompt on the client side. Anything that spends money or
    queues heavy work therefore (a) requires the connector to have
    allow_writes ON, and (b) takes a required `confirm: true` argument and
    returns doing no work without it. Nothing here deletes anything.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import require_auth
from config import settings
from db.models import McpConnector
from db.session import AsyncSessionLocal, get_db

logger = logging.getLogger("mcp")


def _configure_audit_logger() -> None:
    """Give the audit trail its own handler.

    Uvicorn configures only the `uvicorn*` loggers and leaves root at WARNING
    with no handler, so an INFO line from an application module is silently
    dropped — which is how the rest of this app's logger.info calls behave. One
    audit line per MCP call is not optional, so this logger carries its own
    stderr handler and stops propagating (no duplicates if root is ever
    configured). Scoped to this logger; app-wide logging is left alone.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [mcp] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_configure_audit_logger()

SERVER_NAME = "webinar-studio"
SERVER_VERSION = "1.0.0"

# Protocol versions we can speak. We echo the client's if we know it, else we
# answer with our default and let the client decide.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# Everything a tool returns lands in the agent's context window. Cap it.
MAX_RESULT_BYTES = 90_000

# Loopback calls go to routes that may do real work (a segment funnel over
# every webinar on a cold cache is minutes, not seconds).
LOOPBACK_TIMEOUT_S = 120.0

# One write per call to stamp last_used_at is wasteful on a chatty agent.
LAST_USED_THROTTLE_S = 60


# ═══════════════════════════════════════════════════════════════════════════
# TOKENS
# ═══════════════════════════════════════════════════════════════════════════

TOKEN_PREFIX = "wsm_"


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _display_prefix(token: str) -> str:
    return token[:10]


def _bearer_from(request: Request) -> str | None:
    raw = request.headers.get("authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


@dataclass
class AuthedConnector:
    id: str
    name: str
    allow_writes: bool


async def _authenticate(request: Request) -> AuthedConnector:
    """Resolve the bearer token to a connector row, or raise.

    503 when nothing is configured, 401 otherwise — with the SAME response
    whether the header was missing or wrong, so the endpoint never tells an
    attacker which it was. `WWW-Authenticate: Bearer` without
    `resource_metadata`: advertising that sends OAuth-capable clients into a
    discovery dance that dead-ends, since we serve no OAuth metadata.
    """
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(McpConnector).where(McpConnector.enabled.is_(True))
        )).scalars().all()

        if not rows:
            raise _http_error(503, "MCP server is not configured. Generate a "
                                   "connector token in Webinar Studio → Connectors → MCP.")

        presented = _bearer_from(request)
        # Hash even a missing token so the compare below runs in the same shape
        # every time; digest_hex is constant length, so compare_digest is
        # constant time over it.
        digest = _hash_token(presented or "")

        matched: McpConnector | None = None
        for row in rows:
            if hmac.compare_digest(digest, row.token_hash or ""):
                matched = row
        if matched is None or presented is None:
            raise _http_error(401, "Invalid or missing bearer token",
                              headers={"WWW-Authenticate": "Bearer"})

        await _stamp_used(db, matched)
        return AuthedConnector(id=matched.id, name=matched.name,
                               allow_writes=bool(matched.allow_writes))


async def _stamp_used(db: AsyncSession, row: McpConnector) -> None:
    """last_used_at is the only honest signal that a connector is live, but it
    does not need to be exact — throttle so a chatty agent isn't one UPDATE per
    tool call. Never fatal: a DB hiccup here must not 500 the MCP call."""
    now = datetime.now(timezone.utc)
    last = row.last_used_at
    if last is not None and (now - last) < timedelta(seconds=LAST_USED_THROTTLE_S):
        return
    try:
        await db.execute(
            update(McpConnector)
            .where(McpConnector.id == row.id)
            .values(last_used_at=now, call_count=McpConnector.call_count + 1)
        )
        await db.commit()
    except Exception:
        logger.warning("mcp last_used_at stamp failed for connector=%s", row.name, exc_info=True)


def _http_error(status: int, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail=message, headers=headers)


# ═══════════════════════════════════════════════════════════════════════════
# LOOPBACK ADAPTER — tools call this app's own HTTP API, never its internals
# ═══════════════════════════════════════════════════════════════════════════

def _loopback_base() -> str:
    override = os.environ.get("MCP_LOOPBACK_BASE_URL")
    if override:
        return override.rstrip("/")
    # Render/uvicorn bind $PORT. 127.0.0.1, never "localhost".
    return f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


class ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_STATUS_CODES = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    409: "CONFLICT", 422: "INVALID_ARGUMENT", 429: "RATE_LIMITED",
    500: "UPSTREAM_ERROR", 502: "UPSTREAM_ERROR", 503: "UNAVAILABLE", 504: "TIMEOUT",
}


async def _call_app(method: str, path: str, *, params: dict | None = None,
                    body: dict | None = None) -> Any:
    """One loopback request to this app's own API. Raises ToolError on failure.

    NOTE the 200-with-an-error normalisation below: several of this app's
    routes answer 200 with `{"available": false, "reason": ...}` or
    `{"ok": false, "error": ...}`. An agent must never read a 200 as success,
    so those are turned into ToolError HERE, in the adapter — not by changing
    the routes, which would break the UI that already handles them.
    """
    url = _loopback_base() + path
    headers = {"Authorization": f"Bearer {settings.API_BEARER_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=LOOPBACK_TIMEOUT_S) as client:
            resp = await client.request(method, url, params=params, json=body, headers=headers)
    except httpx.TimeoutException:
        raise ToolError("TIMEOUT", f"{method} {path} did not finish within {int(LOOPBACK_TIMEOUT_S)}s. "
                                   "Narrow the request (fewer webinars, lower limit) and retry.")
    except httpx.HTTPError as exc:
        raise ToolError("UPSTREAM_ERROR", f"{method} {path} failed: {exc}")

    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text[:500]
        code = _STATUS_CODES.get(resp.status_code, "UPSTREAM_ERROR")
        raise ToolError(code, f"{method} {path} → {resp.status_code}: {detail}")

    if not resp.content:
        return {}
    try:
        data = resp.json()
    except Exception:
        raise ToolError("UPSTREAM_ERROR", f"{method} {path} returned a non-JSON body")

    if isinstance(data, dict):
        if data.get("available") is False:
            raise ToolError("UNAVAILABLE", str(data.get("reason") or "Not available for this scope"))
        if data.get("ok") is False:
            raise ToolError("UPSTREAM_ERROR", str(data.get("error") or data.get("message") or "Request failed"))
    return data


# ═══════════════════════════════════════════════════════════════════════════
# CONTEXT BUDGET
# ═══════════════════════════════════════════════════════════════════════════

def _project(obj: dict, keys: Iterable[str]) -> dict:
    keep = set(keys)
    return {k: v for k, v in obj.items() if k in keep}


def _drop(obj: dict, keys: Iterable[str]) -> dict:
    drop = set(keys)
    return {k: v for k, v in obj.items() if k not in drop}


# The metrics a report actually reads. The full StatisticsMetrics model carries
# ~100 fields per scope per webinar; returning all of them for every webinar is
# most of a context window spent on fields nobody asked for. Opt in with
# include: ["allMetrics"].
HEADLINE_METRICS = (
    "invited", "actuallyUsed", "unsubscribes", "totalRegs", "totalAttended",
    "total10MinPlus", "total30MinPlus", "uniqueBookers", "totalBookings",
    "totalCallsDatePassed", "confirmed", "shows", "noShows", "canceled",
    "won", "qualified", "disqualified",
    "unsubPercent", "invitedToRegPercent", "totalRegsPer1kInv",
    "regToAttendPercent", "invitedToAttendPercent", "totalAttendedPer1kInv",
    "attend10MinPercent", "attend30MinPercent", "total10MinPlusPer1kInv",
    "total30MinPlusPer1kInv", "bookingsPerAttended", "bookingsPerPast10Min",
    "totalBookingsPer1kInv", "showPercent", "closeRatePercent", "qualPercent",
)


def _thin_metrics(metrics: Any) -> Any:
    if isinstance(metrics, dict):
        return _project(metrics, HEADLINE_METRICS)
    return metrics


def _paginate(data: Any, field: str, limit: int, offset: int) -> Any:
    """Page one list inside a response, and say so. The routes these tools wrap
    return every row (109 segments, 288 calendars, 117 uploads) because the UI
    renders a table; an agent only needs a window, and `_page` tells it exactly
    what it is looking at so it can ask for the rest."""
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        return data
    items = data[field]
    total = len(items)
    window = items[offset:offset + limit]
    out = dict(data)
    out[field] = window
    if total > len(window):
        out["_page"] = {
            "field": field, "total": total, "offset": offset, "returned": len(window),
            "next_offset": (offset + len(window)) if (offset + len(window)) < total else None,
        }
    return out


def _cap(data: Any) -> Any:
    """Hold a single result under MAX_RESULT_BYTES, truncating with an explicit
    marker rather than silently. Lists are trimmed longest-first, because that
    is where the bulk always is (per-webinar rows, contact items, bands)."""
    encoded = json.dumps(data, default=str)
    if len(encoded) <= MAX_RESULT_BYTES:
        return data
    if not isinstance(data, dict):
        return {"_truncated": True,
                "_note": f"Result exceeded {MAX_RESULT_BYTES} bytes and was dropped. "
                         "Narrow the request and retry."}

    out = dict(data)
    truncated: dict[str, str] = {}
    lists = sorted(
        [(k, v) for k, v in out.items() if isinstance(v, list) and v],
        key=lambda kv: len(json.dumps(kv[1], default=str)),
        reverse=True,
    )
    for key, items in lists:
        total = len(items)
        kept = total
        while kept > 0 and len(json.dumps(out, default=str)) > MAX_RESULT_BYTES:
            kept = kept // 2
            out[key] = items[:kept]
        if kept < total:
            truncated[key] = f"{kept} of {total} items returned"
        if len(json.dumps(out, default=str)) <= MAX_RESULT_BYTES:
            break

    if truncated:
        out["_truncated"] = truncated
        out["_note"] = ("Result was capped for context budget. Re-request a narrower scope "
                        "(fewer `webinars`, a lower `limit`, or fewer `include` sections) "
                        "to see the rest.")
    if len(json.dumps(out, default=str)) > MAX_RESULT_BYTES:
        return {"_truncated": True,
                "_note": f"Result exceeded {MAX_RESULT_BYTES} bytes even after trimming. "
                         "Narrow the request (fewer `webinars`, a lower `limit`) and retry."}
    return out


# ═══════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY
# One tool = one route = one fixed method. There is deliberately no generic
# http_request tool, and nothing that can edit the prompts driving the
# app's own copy/report generation.
# ═══════════════════════════════════════════════════════════════════════════

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SOURCE_ENUM = ("auto", "ghl", "workbook")

# Arguments that reach a path segment or a database filter as a raw string.
# Validated against _ID_RE at the tool boundary, BEFORE any concatenation —
# a path fragment smuggled into an id would otherwise reach the query.
_ID_ARGS = {"webinar_id", "bucket_id", "contact_id", "assignment", "bucket", "calendar_id"}


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    method: str = "GET"
    path: str = ""
    required: tuple[str, ...] = ()
    path_args: tuple[str, ...] = ()
    query_args: tuple[str, ...] = ()
    body_args: tuple[str, ...] = ()
    defaults: dict = field(default_factory=dict)   # applied when the arg is absent
    fixed_query: dict = field(default_factory=dict)  # always applied, overrides args
    includes: tuple[str, ...] = ()
    writes: bool = False
    # Routes that answer with one long list and take no limit of their own get
    # paged here, in the adapter. The route is unchanged; only what reaches the
    # agent's context is bounded, and `_page` always says how much was held back.
    list_field: str | None = None
    page_size: int = 25
    shape: Callable[[Any, set[str], dict], Any] | None = None
    static: Callable[[dict], Any] | None = None

    def schema(self) -> dict:
        props = dict(self.properties)
        if self.includes:
            props["include"] = {
                "type": "array",
                "items": {"type": "string", "enum": list(self.includes)},
                "description": ("Heavy sections to add to the compact default. "
                                f"One or more of: {', '.join(self.includes)}."),
            }
        if self.list_field:
            props["limit"] = {
                "type": "integer", "minimum": 1, "maximum": 500,
                "description": f"How many {self.list_field} to return (default {self.page_size}).",
            }
            props["offset"] = {
                "type": "integer", "minimum": 0,
                "description": f"Skip this many {self.list_field} — page through with it.",
            }
        if self.writes:
            props["confirm"] = {
                "type": "boolean",
                "description": "Must be true. Without it this tool does no work and returns "
                               "CONFIRMATION_REQUIRED.",
            }
        required = list(self.required) + (["confirm"] if self.writes else [])
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }


# ── shared argument fragments ──────────────────────────────────────────────

_P_SOURCE = {"source": {"type": "string", "enum": list(SOURCE_ENUM),
                        "description": "Statistics data source. 'auto' (default) is what the UI uses."}}
_P_WEBINARS = {"webinars": {"type": "string",
                            "description": "Comma-separated Webinar UUIDs to include "
                                           "(from list_webinars). Omit for every passed webinar."}}


# ── per-tool shaping ───────────────────────────────────────────────────────

def _shape_overview(data: Any, include: set[str], args: dict) -> Any:
    """Three scopes x ~100 metric fields x every webinar is most of a context
    window. Default to the `overall` scope and the headline fields; the other
    scopes and the full field set are each one `include` away."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "allWebinars" not in include:
        out.pop("allWebinars", None)
    keep_scopes = None if "allScopes" in include else {"overall"}
    thin = "allMetrics" not in include
    if keep_scopes or thin:
        shaped = []
        for w in (out.get("webinars") or []):
            scopes = w.get("scopes") or {}
            if keep_scopes:
                scopes = {k: v for k, v in scopes.items() if k in keep_scopes}
            if thin:
                scopes = {k: _thin_metrics(v) for k, v in scopes.items()}
            shaped.append({**w, "scopes": scopes})
        out["webinars"] = shaped
        if keep_scopes:
            out["_scopeNote"] = ("Only the `overall` scope is returned. Add "
                                 "include: [\"allScopes\"] for the assigned-lists and "
                                 "new-joiner scopes.")
    return out


def _shape_webinar(data: Any, include: set[str], args: dict) -> Any:
    """`summary` is the webinar's metric block; `rows` are its per-list rows,
    each carrying its own full metric set AND the copy that list was sent — the
    rows are several times the size of the summary, so they are opt-in."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    thin = "allMetrics" not in include
    if thin and isinstance(out.get("summary"), dict):
        out["summary"] = _thin_metrics(out["summary"])

    rows = out.get("rows")
    if isinstance(rows, list):
        if "rows" not in include:
            out["rowCount"] = len(rows)
            out.pop("rows", None)
        else:
            shaped = []
            for r in rows:
                if not isinstance(r, dict):
                    shaped.append(r)
                    continue
                r = dict(r)
                if thin and isinstance(r.get("metrics"), dict):
                    r["metrics"] = _thin_metrics(r["metrics"])
                if "copies" not in include:
                    r.pop("titleCopy", None)
                    r.pop("descCopy", None)
                shaped.append(r)
            out["rows"] = shaped
    return out


def _shape_by_source(data: Any, include: set[str], args: dict) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "webinars" not in include:
        out.pop("webinars", None)
    if "perWebinar" not in include:
        out.pop("perWebinar", None)
    if "vintages" not in include:
        out["bySource"] = [_drop(r, ("vintages",)) if isinstance(r, dict) else r
                           for r in (out.get("bySource") or [])]
    return out


def _shape_by_employee(data: Any, include: set[str], args: dict) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "webinars" not in include:
        out.pop("webinars", None)
    if "perWebinar" not in include:
        out.pop("perWebinar", None)
    return out


def _shape_segments(data: Any, include: set[str], args: dict) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "webinars" not in include:
        out.pop("webinars", None)
    return out


def _shape_providers(data: Any, include: set[str], args: dict) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "perWebinar" not in include:
        out.pop("webinars", None)
    return out


def _sum_cells(cells: Iterable[dict]) -> dict:
    out = {"total_sent": 0, "yes": 0, "maybe": 0}
    for c in cells:
        if not isinstance(c, dict):
            continue
        for k in out:
            out[k] += int(c.get(k) or 0)
    return out


def _shape_account_health(data: Any, include: set[str], args: dict) -> Any:
    """The route answers with a 395-account x 24-webinar matrix plus the
    sending-account→sender mapping the UI needs: ~600KB, far past any context
    budget. Default to what "account health" actually asks — per-account totals
    over the webinars in scope, biggest sender first. The raw matrix is opt-in,
    and `webinars` narrows it."""
    if not isinstance(data, dict):
        return data
    wanted = {x for x in (args.get("webinars") or "").split(",") if x}
    webinars = [w for w in (data.get("webinars") or [])
                if not wanted or w.get("id") in wanted]
    scope_ids = {w.get("id") for w in webinars}

    accounts = []
    for acc in (data.get("accounts") or []):
        per = {k: v for k, v in (acc.get("per_webinar") or {}).items() if k in scope_ids}
        if not per:
            continue
        row = {"calendar_account": acc.get("calendar_account"), **_sum_cells(per.values())}
        row["respondedPct"] = (round(100 * (row["yes"] + row["maybe"]) / row["total_sent"], 3)
                               if row["total_sent"] else None)
        if "perWebinar" in include:
            row["per_webinar"] = per
        accounts.append(row)
    accounts.sort(key=lambda r: r["total_sent"], reverse=True)

    totals = {k: v for k, v in (data.get("totals") or {}).items() if k in scope_ids}
    out = {
        "webinars": webinars,
        "accounts": accounts,
        "perWebinarTotals": totals,
        "grandTotal": _sum_cells(totals.values()),
        "_note": ("Per-account counts are summed over the webinars in scope. Pass `webinars` to "
                  "narrow, include: [\"perWebinar\"] for the per-webinar cells."),
    }
    if "senders" in include:
        out["senders"] = data.get("senders")
        out["sender_names"] = data.get("sender_names")
    return out


def _shape_send_day(data: Any, include: set[str], args: dict) -> Any:
    """18k (webinar x account x weekday) cells — 2.8MB. The question this
    answers is "which weekday performs best", so default to the weekday
    totals and the per-webinar x weekday cut; the per-account cells are opt-in
    and need `webinars` to stay inside the budget."""
    if not isinstance(data, dict):
        return data
    days = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
    wanted = {x for x in (args.get("webinars") or "").split(",") if x}
    webinars = [w for w in (data.get("webinars") or [])
                if not wanted or w.get("id") in wanted]
    scope_ids = {w.get("id") for w in webinars}

    cells = [c for c in (data.get("cells") or [])
             if isinstance(c, dict) and c.get("webinar_id") in scope_ids]

    def _blank() -> dict:
        return {"sent": 0, "yes": 0, "maybe": 0}

    by_day: dict[int, dict] = {}
    by_webinar_day: dict[tuple, dict] = {}
    for c in cells:
        dow = c.get("dow")
        for bucket, key in ((by_day, dow), (by_webinar_day, (c.get("webinar_id"), dow))):
            row = bucket.setdefault(key, _blank())
            row["sent"] += int(c.get("sent") or 0)
            row["yes"] += int(c.get("yes") or 0)
            row["maybe"] += int(c.get("maybe") or 0)

    def _rate(row: dict) -> dict:
        sent = row["sent"]
        return {**row, "respondedPct": round(100 * (row["yes"] + row["maybe"]) / sent, 3) if sent else None}

    out = {
        "webinars": webinars,
        "byDay": [{"dow": d, "day": days[d], **_rate(by_day[d])} for d in sorted(by_day)],
        "byWebinarDay": [
            {"webinar_id": wid, "dow": d, "day": days[d], **_rate(row)}
            for (wid, d), row in sorted(by_webinar_day.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ],
        "skippedNoDate": len(data.get("skipped") or []),
        "_note": ("Counts are summed from the per-(webinar, account, weekday) cells. dow 0=Sunday. "
                  "Pass `webinars` to narrow; include: [\"byAccount\"] adds the per-account cells "
                  "(large — scope it with `webinars`)."),
    }
    if "byAccount" in include:
        out["byAccount"] = [{**c, "day": days[c["dow"]]} for c in cells if isinstance(c.get("dow"), int)]
    return out


def _shape_assignments(data: Any, include: set[str], args: dict) -> Any:
    """Each assignment embeds the full title and description copy records — the
    description alone is ~2KB. Keep the identity and the counts; the copy text
    is one `include` away."""
    if not isinstance(data, dict) or "copy" in include:
        return data
    rows = data.get("assignments")
    if not isinstance(rows, list):
        return data
    out = []
    for a in rows:
        if not isinstance(a, dict):
            out.append(a)
            continue
        a = dict(a)
        for key in ("title_copy", "desc_copy"):
            copy = a.get(key)
            if isinstance(copy, dict):
                a[key] = {"id": copy.get("id"), "variant_index": copy.get("variant_index"),
                          "_textOmitted": True}
        out.append(a)
    return {**data, "assignments": out}


def _shape_uploads(data: Any, include: set[str], args: dict) -> Any:
    """Every upload row carries a full per-bucket summary (~12KB each) that the
    upload screen renders as a breakdown. list_segments already serves that,
    per segment, so it is repeated 31 times here for nothing."""
    if not isinstance(data, dict) or "bucketSummary" in include:
        return data
    uploads = data.get("uploads")
    if not isinstance(uploads, list):
        return data
    return {**data, "uploads": [
        {**u, "bucket_summary": {"_omitted": True, "_buckets": len(u.get("bucket_summary") or [])}}
        if isinstance(u, dict) and u.get("bucket_summary") else u
        for u in uploads
    ]}


def _shape_report(data: Any, include: set[str], args: dict) -> Any:
    """The stored report payload is the whole frozen artifact — every funnel,
    every breakdown. The insights are the part an agent usually wants."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    payload = out.get("payload")
    if "payload" not in include and isinstance(payload, dict):
        out["payload"] = {"_omitted": True,
                          "_sections": sorted(payload.keys()),
                          "_note": "Pass include: [\"payload\"] for the full frozen report payload."}
    return out


def _shape_contact(data: Any, include: set[str], args: dict) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "raw" not in include:
        out.pop("raw_custom_fields", None)
        out.pop("raw", None)
    return out


# ── the metric dictionary (static reference, no HTTP call) ─────────────────

METRIC_GLOSSARY: dict[str, str] = {
    "invited": "Calendar invites sent for the webinar (the funnel denominator).",
    "actuallyUsed": "Contacts actually consumed from the assigned lists.",
    "unsubscribes": "Unsubscribes recorded against the send.",
    "totalRegs": "Registrations: Yes + Maybe + self-registrations.",
    "totalAttended": "Distinct attendees who joined the live room or replay.",
    "total10MinPlus": "Attendees who stayed 10 minutes or more.",
    "total30MinPlus": "Attendees who stayed 30 minutes or more.",
    "uniqueBookers": "Distinct contacts who booked a call, deduped by contact "
                     "via per-webinar booking attribution. This is the number displayed as 'Bookings'.",
    "totalBookings": "Booked calls (not deduped by contact).",
    "totalCallsDatePassed": "Booked calls whose appointment date has already passed. "
                            "This is the Show% denominator, so upcoming calls never depress the rate.",
    "confirmed": "Booked calls confirmed by the prospect.",
    "shows": "Booked calls the prospect attended.",
    "noShows": "Booked calls the prospect missed.",
    "won": "Closed-won opportunities.",
    "qualified": "Opportunities marked qualified.",
    "disqualified": "Opportunities marked disqualified.",
    "unsubPercent": "unsubscribes / invited.",
    "invitedToRegPercent": "totalRegs / invited.",
    "regToAttendPercent": "totalAttended / totalRegs.",
    "attend10MinPercent": "total10MinPlus / totalAttended.",
    "attend30MinPercent": "total30MinPlus / total10MinPlus — the 30-minute rate is measured "
                          "against the 10-minute cohort, not all attendees.",
    "totalRegsPer1kInv": "Registrations per 1,000 invites.",
    "totalAttendedPer1kInv": "Attendees per 1,000 invites.",
    "totalBookingsPer1kInv": "Bookings per 1,000 invites — the headline efficiency metric.",
    "bookingsPerAttended": "uniqueBookers / totalAttended.",
    "bookingsPerPast10Min": "uniqueBookers / total10MinPlus.",
    "showPercent": "shows / totalCallsDatePassed.",
    "closeRatePercent": "won / shows.",
    "qualPercent": "qualified / (qualified + disqualified).",
}

SCOPE_GLOSSARY = {
    "assigned": "Only contacts on lists assigned to the webinar in Planning.",
    "noListData": "Invited contacts with no list attribution.",
    "nonjoiners": "Contacts re-invited from the non-joiner pool (the last 6 webinars' non-joiners).",
    "newJoiners": "assigned + noListData — everyone new to this webinar.",
    "overall": "Every contact in the webinar, all scopes summed.",
}


def _static_metrics(args: dict) -> Any:
    return {
        "metrics": METRIC_GLOSSARY,
        "scopes": SCOPE_GLOSSARY,
        "notes": [
            "Rates are always derived from summed raw counts, never by averaging per-webinar rates.",
            "Segment / source / employee funnels return raw counts only (invites, regs, "
            "attendees10m, bookings, callsPassed, shows, won, …) — derive the percentages from those.",
            "Snapshot-backed tools (overview, segments, by-list-source, by-employee-count) read "
            "precomputed snapshots. Webinars returned in pendingWebinarIds have no snapshot yet and "
            "are excluded from the totals — check get_statistics_freshness before trusting a total.",
        ],
    }


TOOLS: list[Tool] = [
    # ── reference ──────────────────────────────────────────────────────────
    Tool(
        name="describe_metrics",
        description=(
            "Static reference: what every Webinar Studio metric means, what the audience scopes "
            "are, and the rules for deriving rates. Free and instant — read this FIRST before "
            "building any report so the numbers are named and combined correctly."
        ),
        properties={},
        static=_static_metrics,
    ),

    # ── webinar-level statistics ───────────────────────────────────────────
    Tool(
        name="list_webinars",
        description=(
            "Every statistics webinar, identity only (UUID, number, variant label, date, title). "
            "Cheap. This is the entry point: almost every other tool takes webinar UUIDs from here."
        ),
        properties={**_P_SOURCE},
        path="/statistics/webinars/list",
        query_args=("source",),
        list_field="webinars",
        page_size=50,
    ),
    Tool(
        name="get_webinar_metrics",
        description=(
            "Metrics for ONE webinar by UUID (or the synthetic stat-wNNN workbook id) — the "
            "single-webinar view behind the Statistics table. Returns the headline metrics; "
            "include: [\"rows\"] adds the per-assigned-list rows, [\"copies\"] the copy each was "
            "sent, [\"allMetrics\"] the full ~100 fields. NOTE: the first call after a deploy "
            "builds this app's statistics cache and can take minutes — if it returns TIMEOUT, "
            "retry once."
        ),
        properties={
            "webinar_id": {"type": "string", "description": "Webinar UUID from list_webinars."},
            **_P_SOURCE,
        },
        required=("webinar_id",),
        path="/statistics/webinars/{webinar_id}",
        path_args=("webinar_id",),
        query_args=("source",),
        includes=("rows", "copies", "allMetrics"),
        shape=_shape_webinar,
    ),
    Tool(
        name="get_overview_series",
        description=(
            "The metric series behind the Statistics Home charts: every selected webinar at three "
            "widening audience scopes (assigned lists / new joiners / overall). THE tool for "
            "trend reports and webinar-over-webinar comparisons. Reads precomputed snapshots, so "
            "it is instant — but webinars listed in pendingWebinarIds have no snapshot and are "
            "excluded; run recompute_statistics if that list is not empty. Returns the `overall` "
            "scope and the headline metrics by default — include: [\"allScopes\"] adds the "
            "assigned-lists and new-joiner scopes, include: [\"allMetrics\"] the full ~100 fields."
        ),
        properties={**_P_WEBINARS, **_P_SOURCE},
        path="/statistics/overview",
        query_args=("webinars", "source"),
        includes=("allScopes", "allMetrics", "allWebinars"),
        shape=_shape_overview,
    ),
    Tool(
        name="get_statistics_freshness",
        description=(
            "When the statistics snapshots were last rebuilt, how many exist, and whether a "
            "recompute is running right now. Check this before reporting on snapshot-backed "
            "numbers (overview, segments, by-list-source, by-employee-count). Cheap."
        ),
        properties={**_P_SOURCE},
        path="/statistics/recompute/status",
        query_args=("source",),
    ),

    # ── funnels / cuts ─────────────────────────────────────────────────────
    Tool(
        name="get_segment_funnel",
        description=(
            "Funnel by segment (outreach bucket) across the selected webinars: invites → regs → "
            "10-min attendees → bookings → calls passed → shows → won, plus lead-quality tiers. "
            "Raw counts — derive rates yourself. Use it to answer 'which segments convert'."
        ),
        properties={**_P_WEBINARS, **_P_SOURCE},
        path="/statistics/segments",
        query_args=("webinars", "source"),
        includes=("webinars",),
        shape=_shape_segments,
    ),
    Tool(
        name="get_segment_by_employee",
        description=(
            "One segment's funnel crossed with company size, across the selected webinars. "
            "Powers the 'which headcount range should this segment target' question. Snapshot-backed; "
            "webinars whose snapshot predates the cross come back in pendingWebinarIds."
        ),
        properties={
            "bucket_id": {"type": "string", "description": "Segment (bucket) UUID from list_segments."},
            **_P_WEBINARS, **_P_SOURCE,
        },
        required=("bucket_id",),
        path="/statistics/segments/{bucket_id}/by-employee",
        path_args=("bucket_id",),
        query_args=("webinars", "source"),
    ),
    Tool(
        name="get_funnel_by_list_source",
        description=(
            "Funnel by data source (AmpleLeads / FindyLeads / ZoomInfo / …) with a per-vintage "
            "drill-down. Answers 'which lead vendor and which list vintage actually produced "
            "bookings'. Per-webinar and per-vintage breakdowns are opt-in via `include` — they "
            "multiply the response size."
        ),
        properties={**_P_WEBINARS, **_P_SOURCE},
        path="/statistics/by-list-source",
        query_args=("webinars", "source"),
        includes=("vintages", "perWebinar", "webinars"),
        shape=_shape_by_source,
    ),
    Tool(
        name="get_funnel_by_employee_count",
        description=(
            "Funnel by company-size bucket across the selected webinars. Answers 'what headcount "
            "converts'. The per-webinar breakdown is opt-in via `include`."
        ),
        properties={**_P_WEBINARS, **_P_SOURCE},
        path="/statistics/by-employee-count",
        query_args=("webinars", "source"),
        includes=("perWebinar", "webinars"),
        shape=_shape_by_employee,
    ),
    Tool(
        name="get_email_provider_breakdown",
        description=(
            "Invite response (Yes / Maybe) by recipient mailbox provider, per webinar and in total — "
            "the deliverability cut. `webinars` is REQUIRED and capped at 12, because this scans "
            "membership per webinar instead of reading a snapshot: it is the slowest tool here. "
            "Domains the MX backfill has not reached are reported as 'Not resolved yet' so volumes "
            "still add up; `resolution` says how complete the cache is."
        ),
        properties={"webinars": {"type": "string",
                                 "description": "Comma-separated Webinar UUIDs. Required, max 12."}},
        required=("webinars",),
        path="/statistics/email-providers",
        query_args=("webinars",),
        includes=("perWebinar",),
        shape=_shape_providers,
    ),
    Tool(
        name="get_list_distribution",
        description=(
            "Which source lists and which email domains the contacts in a scope came from, with "
            "counts and percentage shares, plus how many sit on free/personal mailboxes. Scope is "
            "ONE of: assignment (a single assigned list), bucket + webinar_id, or webinar_id alone."
        ),
        properties={
            "assignment": {"type": "string", "description": "Assignment UUID (a single assigned list)."},
            "bucket": {"type": "string", "description": "Segment (bucket) UUID — pass with webinar_id."},
            "webinar_id": {"type": "string", "description": "Webinar UUID."},
        },
        path="/statistics/list-distribution",
        query_args=("assignment", "bucket", "webinar_id"),
    ),
    Tool(
        name="get_metric_contacts",
        description=(
            "The individual contacts (or opportunities) behind ONE metric on ONE webinar — the "
            "drill-down under a dashboard number, each with a GHL deep link. Use it to verify a "
            "number or to list the people in a cohort. Capped by `limit` (default 100, max 500); "
            "the response is large, so keep the limit low unless you need every row."
        ),
        properties={
            "metric": {"type": "string",
                       "description": "Metric key, e.g. totalRegs, total10MinPlus, uniqueBookers, "
                                      "shows, won. See describe_metrics."},
            "webinar_id": {"type": "string", "description": "Webinar UUID. Preferred over `webinar`: "
                                                            "it picks exactly one A/B variant."},
            "assignment": {"type": "string", "description": "Optional: narrow to one assigned list."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                      "description": "Max rows (default 100)."},
        },
        required=("metric", "webinar_id"),
        path="/statistics/contacts",
        query_args=("metric", "webinar_id", "assignment", "limit"),
        defaults={"limit": 100},
    ),
    Tool(
        name="list_booking_calendars",
        description=(
            "Every calendar calls are booked on, with its class (first / followup / exclude), its "
            "curated source label, and how many first calls it sourced. Needed to read booking "
            "numbers correctly — bookings are attributed through first-call calendars."
        ),
        properties={},
        path="/statistics/booking-calendars",
        list_field="calendars",
        page_size=25,
    ),

    # ── per-webinar report artifact ────────────────────────────────────────
    Tool(
        name="get_webinar_report",
        description=(
            "The stored per-webinar report: the frozen metric payload plus the AI-written insights. "
            "READ-ONLY — it will NOT trigger generation if none exists (that costs money); it "
            "returns status.generated_at = null instead, and generate_webinar_report is how you ask "
            "for one. The full payload is opt-in via include: [\"payload\"]."
        ),
        properties={"webinar_id": {"type": "string", "description": "Webinar UUID."}},
        required=("webinar_id",),
        path="/statistics/report/{webinar_id}",
        path_args=("webinar_id",),
        fixed_query={"generate_if_missing": "false"},
        includes=("payload",),
        shape=_shape_report,
    ),
    Tool(
        name="get_webinar_report_status",
        description=(
            "Generation progress for one webinar's report: running / queued / phase / last error, "
            "and typical_ms as an ETA. Poll this after generate_webinar_report, then re-read with "
            "get_webinar_report once finished_at is set."
        ),
        properties={"webinar_id": {"type": "string", "description": "Webinar UUID."}},
        required=("webinar_id",),
        path="/statistics/report/{webinar_id}/status",
        path_args=("webinar_id",),
    ),
    Tool(
        name="generate_webinar_report",
        description=(
            "Queue (re)generation of one webinar's report. COSTS MONEY — it runs the full metric "
            "pass and then an Opus model call (~$0.14 per report) — and takes 2–4 minutes. "
            "ASYNC: returns immediately; poll get_webinar_report_status, then read the result with "
            "get_webinar_report. Requires confirm: true and a connector with writes enabled. "
            "Check get_webinar_report first — a stored report may already answer the question."
        ),
        properties={"webinar_id": {"type": "string", "description": "Webinar UUID."}},
        required=("webinar_id",),
        method="POST",
        path="/statistics/report/{webinar_id}/generate",
        path_args=("webinar_id",),
        writes=True,
    ),
    Tool(
        name="recompute_statistics",
        description=(
            "Force a full rebuild of every webinar's statistics snapshot. HEAVY: a long background "
            "pass over the contacts table that competes with live traffic — only run it when "
            "get_statistics_freshness shows stale snapshots or a non-empty pendingWebinarIds. "
            "ASYNC: returns immediately; poll get_statistics_freshness. A second call while one is "
            "running is a no-op. Requires confirm: true and a connector with writes enabled."
        ),
        properties={**_P_SOURCE},
        method="POST",
        path="/statistics/recompute",
        query_args=("source",),
        writes=True,
    ),

    # ── planning / inventory ───────────────────────────────────────────────
    Tool(
        name="list_segments",
        description=(
            "Every outreach segment (bucket) with its stored total and remaining-contact counters — "
            "the Planning page's inventory view. Use the bucket UUIDs here for "
            "get_segment_by_employee and get_list_distribution. The generated copy per segment is "
            "opt-in via include: [\"copies\"]."
        ),
        properties={},
        path="/outreach/buckets",
        includes=("copies",),
        list_field="buckets",
        page_size=25,
    ),
    Tool(
        name="list_planning_webinars",
        description=(
            "Planning-side webinars with their list assignments — dates, numbers, variants, and "
            "which segments are assigned. Complements list_webinars, which is the statistics side."
        ),
        properties={},
        path="/outreach/webinars",
        list_field="webinars",
        page_size=25,
    ),
    Tool(
        name="get_webinar_lists",
        description=(
            "The assigned lists for one webinar: segment, sender account, contact counts and "
            "blocklist overlap. The title/description copy each list was sent is omitted by "
            "default (it is the bulk of the response) — include: [\"copy\"] restores it."
        ),
        properties={"webinar_id": {"type": "string", "description": "Webinar UUID."}},
        required=("webinar_id",),
        path="/outreach/webinars/{webinar_id}/lists",
        path_args=("webinar_id",),
        includes=("copy",),
        shape=_shape_assignments,
        list_field="assignments",
        page_size=40,
    ),
    Tool(
        name="list_contact_uploads",
        description=(
            "Contact CSV import history: file name, row counts, inserted vs skipped, and status. "
            "Use it to trace where a cohort came from. The per-segment breakdown each row carries "
            "is omitted by default (it repeats list_segments); include: [\"bucketSummary\"] "
            "restores it."
        ),
        properties={},
        path="/outreach/uploads",
        includes=("bucketSummary",),
        shape=_shape_uploads,
        list_field="uploads",
        page_size=25,
    ),

    # ── contacts ───────────────────────────────────────────────────────────
    Tool(
        name="search_contacts",
        description=(
            "The contacts directory: substring search across name/email/company/title, and/or "
            "filters by campaign (webinar), status, calendar response, engagement and firmographics. "
            "At 5.6M contacts a query MUST be led by a driver — pass at least one of: `search` (a "
            "term of 3+ chars), `webinar_id`, `bucket_id`, `response`, `engagement`, or "
            "blocklisted: true. Filter-only requests are rejected rather than served as a table scan."
        ),
        properties={
            "search": {"type": "string", "description": "Substring search; needs one term of 3+ chars."},
            "webinar_id": {"type": "string", "description": "Campaign (webinar) UUID the contact took part in."},
            "bucket_id": {"type": "string", "description": "Segment (bucket) UUID."},
            "status": {"type": "string", "enum": ["available", "assigned", "used"]},
            "response": {"type": "string", "enum": ["yes", "maybe", "no", "awaiting", "deleted", "spam"],
                         "description": "Calendar invite response — requires webinar_id to be meaningful."},
            "engagement": {"type": "string",
                           "enum": ["registered", "attended", "live", "replay", "booked", "won"]},
            "blocklisted": {"type": "boolean"},
            "country": {"type": "string"},
            "industry": {"type": "string"},
            "seniority": {"type": "string"},
            "employee_range": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                      "description": "Max rows (default 50, cap 200)."},
            "offset": {"type": "integer", "minimum": 0},
        },
        path="/outreach/contacts",
        query_args=("search", "webinar_id", "bucket_id", "status", "response", "engagement",
                    "blocklisted", "country", "industry", "seniority", "employee_range",
                    "limit", "offset"),
        defaults={"limit": 50},
    ),
    Tool(
        name="get_contact",
        description=(
            "Everything known about one contact: profile, per-webinar history (invite response, "
            "attendance, the copy they were sent), attributed bookings, release history, blocklist "
            "entry, and the matched CRM record. All index hits — safe at directory scale."
        ),
        properties={"contact_id": {"type": "string", "description": "Contact UUID."}},
        required=("contact_id",),
        path="/outreach/contacts/{contact_id}",
        path_args=("contact_id",),
        includes=("raw",),
        shape=_shape_contact,
    ),

    # ── send quality ───────────────────────────────────────────────────────
    Tool(
        name="get_calendar_account_health",
        description=(
            "Invite volume and Yes/Maybe response per SENDING ACCOUNT — the deliverability view "
            "over the calendar accounts, biggest sender first. Answers 'which mailboxes are "
            "burning' and 'which are still landing'. Totals are summed over the webinars in scope "
            "(all past webinars unless `webinars` narrows it); include: [\"perWebinar\"] adds the "
            "per-webinar cells, which is large — scope it first. Past webinars only."
        ),
        properties={**_P_WEBINARS},
        path="/calendar-uploads/account-health",
        includes=("perWebinar", "senders"),
        shape=_shape_account_health,
        list_field="accounts",
        page_size=40,
    ),
    Tool(
        name="get_calendar_send_day_stats",
        description=(
            "Invite response by the WEEKDAY the invite was sent. Answers 'which send day "
            "performs best'. Returns weekday totals plus a per-webinar x weekday cut, summed over "
            "the webinars in scope; include: [\"byAccount\"] adds the per-account cells (large — "
            "pass `webinars` when you use it). dow 0 = Sunday. Past webinars only."
        ),
        properties={**_P_WEBINARS},
        path="/calendar-uploads/day-of-week",
        includes=("byAccount",),
        shape=_shape_send_day,
    ),
    Tool(
        name="list_calendar_uploads",
        description=(
            "Calendar invite upload history: file, webinar, row counts, matched vs no-list-data, "
            "and import status. counts_pending means the match count never resolved — read it as "
            "unknown, not zero."
        ),
        properties={},
        path="/calendar-uploads",
        list_field="uploads",
        page_size=25,
    ),

    # ── ops ────────────────────────────────────────────────────────────────
    Tool(
        name="get_crm_sync_status",
        description=(
            "GoHighLevel sync status: last run, what it covered, and whether one is in flight. "
            "Booking, show and won numbers come from this sync — check it before reporting on them."
        ),
        properties={},
        path="/ghl-sync/status",
    ),
    Tool(
        name="get_weekly_report_settings",
        description=(
            "The scheduled weekly email report: whether it is on, when it sends, its timezone, "
            "recipients, from address, and the last send result."
        ),
        properties={},
        path="/reports/settings",
    ),
    Tool(
        name="send_report_email",
        description=(
            "Send the webinar report email NOW to the configured recipients. SENDS REAL EMAIL to "
            "real people and cannot be undone. Requires confirm: true and a connector with writes "
            "enabled. Omit webinar_id to report on the most recent passed webinar."
        ),
        properties={"webinar_id": {"type": "string",
                                   "description": "Optional Webinar UUID; defaults to the latest passed webinar."}},
        method="POST",
        path="/reports/send-test",
        body_args=("webinar_id",),
        writes=True,
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# ═══════════════════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def _validate(tool: Tool, args: dict) -> tuple[dict, set[str]]:
    """Strict validation at the tool boundary. Unknown arguments are rejected
    by name, with the allowed ones listed, so a model can correct itself in one
    turn instead of guessing."""
    if not isinstance(args, dict):
        raise ToolError("INVALID_ARGUMENT", "arguments must be an object")

    schema_props = tool.schema()["properties"]
    unknown = [k for k in args if k not in schema_props]
    if unknown:
        raise ToolError(
            "INVALID_ARGUMENT",
            f"Unknown argument(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(schema_props)) or '(none)'}.",
        )

    missing = [k for k in tool.required if args.get(k) in (None, "")]
    if missing:
        raise ToolError("INVALID_ARGUMENT", f"Missing required argument(s): {', '.join(missing)}.")

    include = set()
    raw_include = args.get("include")
    if raw_include is not None:
        if not isinstance(raw_include, list) or any(not isinstance(x, str) for x in raw_include):
            raise ToolError("INVALID_ARGUMENT", "include must be an array of strings")
        bad = [x for x in raw_include if x not in tool.includes]
        if bad:
            raise ToolError("INVALID_ARGUMENT",
                            f"Unknown include section(s): {', '.join(bad)}. "
                            f"Allowed: {', '.join(tool.includes) or '(none)'}.")
        include = set(raw_include)

    clean: dict = {}
    for key, value in args.items():
        if key in ("include", "confirm") or value is None:
            continue
        spec = schema_props.get(key, {})
        expected = spec.get("type")

        if expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError("INVALID_ARGUMENT", f"{key} must be an integer")
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if lo is not None and value < lo:
                raise ToolError("INVALID_ARGUMENT", f"{key} must be >= {lo}")
            if hi is not None and value > hi:
                raise ToolError("INVALID_ARGUMENT", f"{key} must be <= {hi}")
        elif expected == "boolean":
            if not isinstance(value, bool):
                raise ToolError("INVALID_ARGUMENT", f"{key} must be a boolean")
        elif expected == "string":
            if not isinstance(value, str):
                raise ToolError("INVALID_ARGUMENT", f"{key} must be a string")
            if len(value) > 500:
                raise ToolError("INVALID_ARGUMENT", f"{key} is too long (max 500 chars)")
            allowed = spec.get("enum")
            if allowed and value not in allowed:
                raise ToolError("INVALID_ARGUMENT",
                                f"{key} must be one of: {', '.join(allowed)}.")
            # Ids reach a path segment or a database filter — pattern-check
            # BEFORE any concatenation.
            if key in _ID_ARGS and not _ID_RE.match(value):
                raise ToolError("INVALID_ARGUMENT",
                                f"{key} is not a valid id (letters, digits, '-' and '_' only)")
            if key == "webinars":
                ids = [x.strip() for x in value.split(",") if x.strip()]
                if not ids:
                    raise ToolError("INVALID_ARGUMENT", "webinars must contain at least one id")
                if any(not _ID_RE.match(x) for x in ids):
                    raise ToolError("INVALID_ARGUMENT", "webinars must be comma-separated webinar ids")
                value = ",".join(ids)
        clean[key] = value

    for name in tool.path_args:
        if name not in clean:
            raise ToolError("INVALID_ARGUMENT", f"Missing required argument: {name}")

    return clean, include


async def _execute(tool: Tool, args: dict, connector: AuthedConnector) -> Any:
    clean, include = _validate(tool, args)

    if tool.writes:
        # Permission first, then the confirm gate: a read-only connector should
        # be told it cannot do this at all, not asked to confirm.
        if not connector.allow_writes:
            raise ToolError("FORBIDDEN",
                            f"This connector is read-only. Enable writes for '{connector.name}' in "
                            "Webinar Studio → Connectors → MCP to allow this tool.")
        if args.get("confirm") is not True:
            raise ToolError("CONFIRMATION_REQUIRED",
                            f"{tool.name} was not run. It spends money or queues heavy work, so it "
                            "requires confirm: true. Re-issue the call with confirm: true only if "
                            "the user has asked for it.")

    if tool.static is not None:
        return tool.static(clean)

    path = tool.path
    for name in tool.path_args:
        path = path.replace("{" + name + "}", clean[name])

    params = {k: v for k, v in tool.defaults.items() if k not in clean}
    params.update({k: clean[k] for k in tool.query_args if k in clean})
    params.update(tool.fixed_query)
    # list_segments maps `include: ["copies"]` onto the route's own ?include=copies
    if tool.name == "list_segments" and "copies" in include:
        params["include"] = "copies"

    body = {k: clean[k] for k in tool.body_args if k in clean} if tool.body_args else None
    if tool.method == "POST" and body is None:
        body = {}

    data = await _call_app(tool.method, path, params=params or None, body=body)
    if tool.shape is not None:
        data = tool.shape(data, include, clean)
    if tool.list_field:
        data = _paginate(data, tool.list_field, clean.get("limit", tool.page_size),
                         clean.get("offset", 0))
    return _cap(data)


# ═══════════════════════════════════════════════════════════════════════════
# JSON-RPC
# ═══════════════════════════════════════════════════════════════════════════

def _rpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_ok(data: Any) -> dict:
    payload = {"ok": True, "data": data}
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}], "isError": False}


def _tool_err(code: str, message: str) -> dict:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}], "isError": True}


async def _handle_rpc(message: dict, connector: AuthedConnector) -> dict | None:
    """Returns the JSON-RPC response, or None for a notification."""
    req_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _rpc_error(req_id, -32600, "Invalid Request: missing method")

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        requested = (params or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return _rpc_result(req_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Webinar Studio: webinar outreach, attendance and sales-conversion data. "
                "Start with describe_metrics (what the numbers mean) and list_webinars (the ids "
                "everything else takes). Snapshot-backed tools are instant; "
                "get_email_provider_breakdown and get_metric_contacts are the expensive ones. "
                "Tools that spend money or send email require confirm: true."
            ),
        })

    if method == "ping":
        return _rpc_result(req_id, {})

    if method == "tools/list":
        return _rpc_result(req_id, {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.schema()}
                for t in TOOLS
            ]
        })

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return _rpc_result(req_id, _tool_err(
                "UNKNOWN_TOOL",
                f"No tool named {name!r}. Call tools/list for the available tools."))

        started = time.monotonic()
        outcome = "ok"
        try:
            data = await _execute(tool, args if isinstance(args, dict) else {}, connector)
            result = _tool_ok(data)
        except ToolError as exc:
            outcome = exc.code
            result = _tool_err(exc.code, exc.message)
        except Exception as exc:  # never leak a traceback to the agent
            outcome = "INTERNAL_ERROR"
            logger.exception("mcp tool %s crashed", tool.name)
            result = _tool_err("INTERNAL_ERROR", f"{tool.name} failed unexpectedly: {exc}")
        finally:
            # Audit: argument KEYS only — values carry PII. Never the token.
            logger.info(
                "mcp call tool=%s arg_keys=%s ms=%d outcome=%s connector=%s",
                tool.name, sorted(args.keys()) if isinstance(args, dict) else [],
                int((time.monotonic() - started) * 1000), outcome, connector.name,
            )
        return _rpc_result(req_id, result)

    if is_notification:
        return None
    return _rpc_error(req_id, -32601, f"Method not found: {method}")


# ═══════════════════════════════════════════════════════════════════════════
# TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Mcp-Session-Id, MCP-Protocol-Version",
    "Access-Control-Expose-Headers": "MCP-Protocol-Version",
    "Access-Control-Max-Age": "86400",
}


def _wants_sse(request: Request) -> bool:
    """Content-negotiate instead of demanding both media types.

    An SDK transport that hard-406s anything but
    `application/json, text/event-stream` breaks real clients. Here:
    application/json, text/event-stream, both, */* and absent all work — we
    only reply with SSE when the client asked for SSE and did NOT ask for JSON.
    """
    accept = (request.headers.get("accept") or "").lower()
    return "text/event-stream" in accept and "application/json" not in accept


def _respond(request: Request, payload: dict | None, status: int = 200) -> Response:
    if payload is None:
        # A notification gets no body.
        return Response(status_code=202, headers=dict(CORS_HEADERS))
    if _wants_sse(request):
        body = f"event: message\ndata: {json.dumps(payload, default=str)}\n\n"
        return Response(
            content=body,
            media_type="text/event-stream",
            status_code=status,
            headers={
                **CORS_HEADERS,
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Without this a buffering proxy holds the frame until timeout.
                "X-Accel-Buffering": "no",
            },
        )
    return JSONResponse(payload, status_code=status, headers=dict(CORS_HEADERS))


router = APIRouter()


@router.options("/mcp", include_in_schema=False)
async def mcp_preflight() -> Response:
    return Response(status_code=204, headers=dict(CORS_HEADERS))


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_method_not_allowed() -> Response:
    # Stateless: there is no stream to resume and no session to delete.
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None,
         "error": {"code": -32000, "message": "This MCP endpoint is stateless. Use POST /mcp."}},
        status_code=405,
        headers={**CORS_HEADERS, "Allow": "POST, OPTIONS"},
    )


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    # Auth first — 503 when unconfigured, 401 otherwise, identical for a
    # missing and a wrong token.
    try:
        connector = await _authenticate(request)
    except HTTPException as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32001, "message": str(exc.detail)}},
            status_code=exc.status_code,
            headers={**CORS_HEADERS, **(exc.headers or {})},
        )

    # A client may send Mcp-Session-Id; we ignore it rather than reject it. We
    # never issue one — a session pinned to an instance that a deploy replaced
    # would 404 mid-conversation with no recovery path.

    try:
        raw = await request.body()
        message = json.loads(raw) if raw else None
    except Exception:
        return _respond(request, _rpc_error(None, -32700, "Parse error: body is not valid JSON"), 400)

    if isinstance(message, list):
        return _respond(request, _rpc_error(None, -32600,
                                            "Batch requests are not supported; send one request per call"), 400)
    if not isinstance(message, dict):
        return _respond(request, _rpc_error(None, -32600, "Invalid Request: expected a JSON object"), 400)

    response = await _handle_rpc(message, connector)
    return _respond(request, response)


# ═══════════════════════════════════════════════════════════════════════════
# PREFLIGHT MIDDLEWARE
# Registered AFTER CORSMiddleware in main.py so it runs BEFORE it (Starlette
# wraps middleware in reverse). Without this, the app's blanket CORS handler
# answers the MCP preflight against its own two-origin allowlist and rejects
# every other client.
# ═══════════════════════════════════════════════════════════════════════════

async def mcp_preflight_middleware(request: Request, call_next):
    if request.method == "OPTIONS" and request.url.path.rstrip("/") == "/mcp":
        return Response(status_code=204, headers=dict(CORS_HEADERS))
    return await call_next(request)


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN — token management for the settings screen (app bearer auth, not MCP)
# ═══════════════════════════════════════════════════════════════════════════

admin_router = APIRouter(dependencies=[Depends(require_auth)])


class McpConnectorOut(BaseModel):
    id: str
    name: str
    token_prefix: str
    enabled: bool
    allow_writes: bool
    last_used_at: str | None = None
    call_count: int = 0
    created_at: str | None = None


class McpConnectorListResponse(BaseModel):
    configured: bool
    endpoint_path: str
    tool_count: int
    connectors: list[McpConnectorOut]


class McpConnectorCreate(BaseModel):
    name: str
    allow_writes: bool = False


class McpConnectorUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    allow_writes: bool | None = None


class McpConnectorCreated(BaseModel):
    connector: McpConnectorOut
    # Returned exactly once, at generation. Nothing else can ever read it back.
    token: str


def _connector_out(row: McpConnector) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "token_prefix": row.token_prefix,
        "enabled": row.enabled,
        "allow_writes": row.allow_writes,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "call_count": row.call_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@admin_router.get("", response_model=McpConnectorListResponse)
async def list_mcp_connectors(db: AsyncSession = Depends(get_db)):
    """Every MCP connector token. Never returns a token — only its prefix."""
    rows = (await db.execute(
        select(McpConnector).order_by(McpConnector.created_at.desc())
    )).scalars().all()
    return {
        "configured": any(r.enabled for r in rows),
        "endpoint_path": "/mcp",
        "tool_count": len(TOOLS),
        "connectors": [_connector_out(r) for r in rows],
    }


@admin_router.post("", response_model=McpConnectorCreated, status_code=201)
async def create_mcp_connector(payload: McpConnectorCreate, db: AsyncSession = Depends(get_db)):
    """Generate a token. The plaintext is in this response and nowhere else —
    only its sha256 is stored."""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(422, "name is required")
    if len(name) > 100:
        raise HTTPException(422, "name must be 100 characters or fewer")

    token = _new_token()
    row = McpConnector(
        name=name,
        token_hash=_hash_token(token),
        token_prefix=_display_prefix(token),
        enabled=True,
        allow_writes=bool(payload.allow_writes),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info("mcp connector created name=%s allow_writes=%s", name, row.allow_writes)
    return {"connector": _connector_out(row), "token": token}


@admin_router.patch("/{connector_id}", response_model=McpConnectorOut)
async def update_mcp_connector(
    connector_id: str, payload: McpConnectorUpdate, db: AsyncSession = Depends(get_db)
):
    row = (await db.execute(
        select(McpConnector).where(McpConnector.id == connector_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "name cannot be empty")
        row.name = name[:100]
    if payload.enabled is not None:
        row.enabled = payload.enabled
    if payload.allow_writes is not None:
        row.allow_writes = payload.allow_writes
    await db.commit()
    await db.refresh(row)
    logger.info("mcp connector updated name=%s enabled=%s allow_writes=%s",
                row.name, row.enabled, row.allow_writes)
    return _connector_out(row)


@admin_router.delete("/{connector_id}", status_code=204)
async def delete_mcp_connector(connector_id: str, db: AsyncSession = Depends(get_db)):
    """Revoke permanently. The token stops working on the next call."""
    row = (await db.execute(
        select(McpConnector).where(McpConnector.id == connector_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    await db.delete(row)
    await db.commit()
    logger.info("mcp connector revoked name=%s", row.name)
    return Response(status_code=204)


@admin_router.post("/{connector_id}/rotate", response_model=McpConnectorCreated)
async def rotate_mcp_connector(connector_id: str, db: AsyncSession = Depends(get_db)):
    """Issue a new token for an existing connector; the old one stops working
    immediately. The new plaintext is shown once."""
    row = (await db.execute(
        select(McpConnector).where(McpConnector.id == connector_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Connector not found")
    token = _new_token()
    row.token_hash = _hash_token(token)
    row.token_prefix = _display_prefix(token)
    await db.commit()
    await db.refresh(row)
    logger.info("mcp connector rotated name=%s", row.name)
    return {"connector": _connector_out(row), "token": token}
