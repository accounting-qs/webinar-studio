# Contact Counts API

A single read-only endpoint that returns live contact counts from Webinar Studio.

## Endpoint

```
GET https://competeiq-api.onrender.com/public/contact-counts
```

## Authentication

Send your API key in the `X-API-Key` request header. The key is provided to you
separately — keep it secret.

```
X-API-Key: <your-api-key>
```

## Response

`200 OK`, `application/json`:

```json
{
  "total_contacts": 12345,
  "available_contacts": 6789,
  "disqualified_contacts": 321
}
```

| Field                   | Meaning                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `total_contacts`        | Every contact in the system, regardless of status or bucket (includes Disqualified).             |
| `available_contacts`    | Contacts available for outreach, **excluding** the Disqualified bucket. Matches the Planning page "available" number. |
| `disqualified_contacts` | Contacts in the Disqualified bucket.                                                             |

All values are non-negative integers.

## Example

```bash
curl -s "https://competeiq-api.onrender.com/public/contact-counts" \
  -H "X-API-Key: <your-api-key>"
```

## Errors

| Status | Body                                              | Cause                                            |
| ------ | ------------------------------------------------- | ------------------------------------------------ |
| `401`  | `{"detail": "Invalid or missing API key"}`        | The `X-API-Key` header is missing or incorrect.  |
| `503`  | `{"detail": "Stats API key not configured"}`      | The server has no API key configured (contact us). |
