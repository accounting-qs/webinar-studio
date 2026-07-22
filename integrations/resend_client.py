"""
Resend email client (https://resend.com).

Minimal httpx wrapper — used by the weekly report sender. The API key is
stored in connector_credentials (provider='resend'), not env config.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.resend.com"
TIMEOUT = 30.0


class ResendError(Exception):
    """Raised on any non-2xx Resend API response.

    Carries the HTTP status + Resend's error message so callers can surface
    actionable failures (invalid key, unverified from-domain, ...).
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Resend API error {status_code}: {message}")


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return data.get("message") or data.get("error") or resp.text
    except Exception:
        return resp.text


async def send_email(api_key: str, *, from_addr: str, to: list[str], subject: str, html: str) -> str:
    """Send an HTML email. Returns the Resend message id."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"from": from_addr, "to": to, "subject": subject, "html": html},
        )
    if resp.status_code >= 300:
        raise ResendError(resp.status_code, _error_message(resp))
    return resp.json().get("id", "")


async def verify_key(api_key: str) -> bool:
    """Cheap key check — list domains. Returns False on 401/403; raises
    ResendError on other failures so transient issues aren't mistaken for a
    bad key."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/domains",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code in (401, 403):
        return False
    if resp.status_code >= 300:
        raise ResendError(resp.status_code, _error_message(resp))
    return True
