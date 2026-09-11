# MCP Server

A Model Context Protocol endpoint mounted on the Webinar Studio API itself, so
an external agent (Grok, Claude, any MCP client) can read the same numbers the
app's own pages show and build reports across them.

```
POST https://competeiq-api.onrender.com/mcp
```

One route on the app that already serves the API — no second service, one
deploy, no new infra.

---

## Setup

Order matters, or the first connection attempt looks broken:

1. **Deploy** (includes migration `080_mcp_connectors`).
2. **Generate a token** in Webinar Studio → Connectors → **MCP Server**. The
   plaintext is shown **once**; only its sha256 is stored. Lose it and you
   rotate for a new one.
3. **Paste it into the client.**
4. **Ask the agent to run a read-only tool first** (`list_webinars`). If tools
   come back, you're connected.

### Grok / xAI

Nothing is built on their side — it's a custom remote MCP connector:

```json
{
  "type": "mcp",
  "server_url": "https://competeiq-api.onrender.com/mcp",
  "server_label": "webinarstudio",
  "authorization": "<token>"
}
```

Add `"allowed_tools": ["list_webinars", "get_overview_series", …]` to restrict a
given bot without a redeploy.

xAI's servers make the calls, so the endpoint must be reachable from the public
internet — it cannot sit behind a VPN or an IP allowlist.

### Headers

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <token>` |
| `Content-Type` | `application/json` |
| `Accept` | anything — `application/json`, `text/event-stream`, both, `*/*`, or absent |

`Mcp-Session-Id` is accepted and ignored. The server never issues one (see
[Stateless](#stateless)).

---

## Tools

Every tool wraps exactly one route with one fixed method. There is no generic
`http_request` tool, nothing that deletes, and nothing that can edit the prompts
driving the app's own copy or report generation.

### Reference

| Tool | What it's for |
| --- | --- |
| `describe_metrics` | What every metric means, what the audience scopes are, how to derive rates. Static and free — read it first. |

### Webinar statistics

| Tool | Notes |
| --- | --- |
| `list_webinars` | Identity list. The entry point — nearly everything else takes ids from here. |
| `get_webinar_metrics` | One webinar. `include`: `rows`, `copies`, `allMetrics`. |
| `get_overview_series` | Per-webinar series for trend reports. Defaults to the `overall` scope; `include`: `allScopes`, `allMetrics`, `allWebinars`. |
| `get_statistics_freshness` | When the snapshots were last rebuilt. Check before quoting snapshot-backed totals. |

### Funnels and cuts

| Tool | Notes |
| --- | --- |
| `get_segment_funnel` | By segment (bucket). |
| `get_segment_by_employee` | One segment × company size. |
| `get_funnel_by_list_source` | By lead vendor, with vintages. `include`: `vintages`, `perWebinar`, `webinars`. |
| `get_funnel_by_employee_count` | By company-size bucket. |
| `get_email_provider_breakdown` | Deliverability by mailbox provider. `webinars` required, max 12. **Slowest tool here** — it scans membership rather than reading a snapshot. |
| `get_list_distribution` | Source lists and email domains behind a scope. |
| `get_metric_contacts` | The individual contacts behind one metric, with GHL deep links. |
| `list_booking_calendars` | Calendar → source mapping behind booking attribution. |

### Reports

| Tool | Notes |
| --- | --- |
| `get_webinar_report` | Stored report + AI insights. Read-only: never triggers generation. `include`: `payload`. |
| `get_webinar_report_status` | Poll this after `generate_webinar_report`. |
| `generate_webinar_report` | **confirm + writes.** ~$0.14, 2–4 min, async. |
| `recompute_statistics` | **confirm + writes.** Heavy background rebuild, async. |
| `get_weekly_report_settings` | Schedule, recipients, last send result. |
| `send_report_email` | **confirm + writes.** Sends real email to real people. |

### Planning, contacts, send quality, ops

| Tool | Notes |
| --- | --- |
| `list_segments` | Segment inventory. `include`: `copies`. |
| `list_planning_webinars` | Planning-side webinars with assignments. |
| `get_webinar_lists` | Assigned lists for one webinar. `include`: `copy`. |
| `list_contact_uploads` | CSV import history. `include`: `bucketSummary`. |
| `search_contacts` | Contacts directory. Needs a driver — see below. |
| `get_contact` | Everything known about one contact. `include`: `raw`. |
| `get_calendar_account_health` | Response per sending account. `include`: `perWebinar`, `senders`. |
| `get_calendar_send_day_stats` | Response by weekday sent. `include`: `byAccount`. |
| `list_calendar_uploads` | Calendar invite upload history. |
| `get_crm_sync_status` | GHL sync state — booking/show/won numbers come from it. |

`search_contacts` must be led by a driver (`search` with a 3+ char term,
`webinar_id`, `bucket_id`, `response`, `engagement`, or `blocklisted: true`).
Filter-only requests are rejected with 422 rather than served as a scan of a
5.6M-row table — that is the app's own guard, passed through.

---

## Response contract

Every tool returns exactly one of:

```json
{"ok": true,  "data": {…}}
{"ok": false, "error": {"code": "…", "message": "…"}}
```

Failures also set MCP `isError: true`.

Several of this app's routes answer HTTP 200 with an error in the body
(`{"available": false, "reason": …}`). The MCP layer normalises those to
`ok:false` in the adapter, so an agent can never read a 200 as success. The
routes themselves are untouched — the UI already handles their shape.

Error codes: `INVALID_ARGUMENT`, `NOT_FOUND`, `UNAVAILABLE`, `FORBIDDEN`,
`CONFIRMATION_REQUIRED`, `TIMEOUT`, `RATE_LIMITED`, `UPSTREAM_ERROR`,
`UNKNOWN_TOOL`, `INTERNAL_ERROR`.

### Context budget

Compact by default, heavy sections opt-in through `include`. Long lists are
paged (`limit` / `offset`, with a `_page` block saying what was held back).
A single result is capped at ~90 KB and truncated with an explicit `_truncated`
marker — never silently.

The raw routes are much larger than what reaches the agent: account health is
613 KB on the wire and 11 KB through the tool; send-day stats are 2.8 MB and
26 KB.

---

## Guardrails

xAI does **not** support MCP's `require_approval` — there is no
human-in-the-loop prompt on the client side. So anything that spends money,
queues heavy work or sends email is gated twice:

1. The connector must have **writes allowed** (off by default — a fresh
   connector is read-only).
2. The call must pass `confirm: true`. Without it the tool returns
   `CONFIRMATION_REQUIRED` **having done no work**.

Nothing here deletes anything.

---

## Security

- Bearer token per connector. Only `sha256(token)` is stored; the plaintext is
  shown once at generation and no endpoint can return it.
- Comparison is `hmac.compare_digest` over constant-length hex digests.
- **Fails closed**: no enabled connector row → `503` for everyone. A missing env
  var or an empty table can never mean an open endpoint.
- `401` with `WWW-Authenticate: Bearer`, identical whether the header was
  missing or wrong. `resource_metadata` is deliberately not advertised — it
  sends OAuth-capable clients into a discovery dance that dead-ends, since no
  OAuth metadata is served.
- Ids are pattern-checked at the tool boundary before any string concatenation,
  so a path fragment smuggled into an id cannot reach a database filter.
- Unknown arguments are rejected by name, with the allowed ones listed.
- One audit line per call: tool name, argument **keys only** (values carry PII),
  duration, outcome, and which connector acted. The token is never logged.

Managing tokens in the settings UI rather than an env var is deliberate: one row
per agent means you can revoke one bot without knocking out the others, and
`last_used_at` is the only honest signal that a connector is actually live.

---

## Transport

The JSON-RPC is hand-rolled (~250 lines) rather than taking
`@modelcontextprotocol/sdk`. A client only ever sends five methods —
`initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`.

**Content negotiation.** If `Accept` includes `text/event-stream` and not
`application/json`, the reply is a single SSE frame (with
`X-Accel-Buffering: no`, or a buffering proxy holds it until timeout);
otherwise JSON. So `application/json`, `text/event-stream`, both, `*/*` and
absent all work. SDK transports that hard-406 anything but both media types
break real clients.

**Stateless.** No `Mcp-Session-Id` is ever issued. Render overlaps instances
during a deploy, and a session pinned to a replaced instance 404s
mid-conversation with no recovery path for the agent. A session id sent by a
client is ignored, not rejected. `GET` and `DELETE` on `/mcp` answer `405`.

**Loopback.** Tools call the app's own HTTP API at `http://127.0.0.1:$PORT/…`,
never internal functions, so MCP behaviour can never drift from what the UI
gets: same routes, same validation, same caches. `127.0.0.1` and not
`localhost`, which can resolve to `::1` and give `ECONNREFUSED`.

**Preflight.** `OPTIONS /mcp` is answered by a middleware registered after
`CORSMiddleware` (so it runs before it) — otherwise the app-wide CORS handler
would judge the MCP preflight against its own two-origin allowlist. Allowed
headers: `Authorization`, `Content-Type`, `Mcp-Session-Id`,
`MCP-Protocol-Version`.

**Timeouts.** A loopback call is given 120 s. `get_webinar_metrics` on a cold
statistics cache can exceed that and return `TIMEOUT`; a retry once the cache is
warm succeeds in about a second.

---

## Files

| Path | Role |
| --- | --- |
| `api/mcp_server.py` | The entire MCP layer: transport, auth, tool registry, loopback adapter, token admin routes. |
| `api/main.py` | Three lines of wiring. |
| `db/models/mcp.py` | `McpConnector`. |
| `migrations/versions/080_mcp_connectors.py` | The `mcp_connectors` table. |
| `frontend/src/components/connectors/McpConnectorPage.tsx` | Settings screen. |

## Deploy checklist

- [ ] `alembic upgrade head` — creates `mcp_connectors` (new table, touches nothing existing).
- [ ] Generate a token in Connectors → MCP Server.
- [ ] Verify: `curl -s -X POST https://…/mcp -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'`
- [ ] Leave **writes** off unless that agent genuinely needs report generation, recompute, or the report email.
