from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from config import settings

bearer_scheme = HTTPBearer()

# Dedicated key for the public read-only contact-counts endpoint, sent by
# external apps in the "X-API-Key" header. auto_error=False so we can return
# our own messages for missing/invalid/unconfigured cases.
stats_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_auth(credentials: HTTPAuthorizationCredentials = Security(bearer_scheme)):
    if credentials.credentials != settings.API_BEARER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )
    return credentials.credentials


def require_stats_key(api_key: str | None = Security(stats_key_scheme)):
    if not settings.STATS_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stats API key not configured",
        )
    if api_key != settings.STATS_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return api_key
