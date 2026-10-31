"""
Zoho OAuth2 token manager.

Responsibilities:
- Persist access/refresh tokens in the database (``ZohoToken`` table).
- Automatically refresh the access token when it is less than 5 minutes from
  expiry (``get_valid_token``).
- Provide a helper to build the authorization URL for the initial OAuth consent
  flow (``get_authorization_url``).

Token storage in a DB (rather than env vars) ensures that worker restarts do
not require a new OAuth code grant.  The refresh token itself is seeded from
``settings.zoho_refresh_token`` the first time the table is empty.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base
from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

logger = logging.getLogger(__name__)

# How many seconds before actual expiry we proactively refresh.
_REFRESH_BUFFER_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# ORM model – kept here to keep all token-related code in one file.
# ---------------------------------------------------------------------------


class ZohoToken(Base):
    """
    Single-row table storing the current Zoho OAuth2 token pair.

    We always upsert the row with id=1 so there is at most one row.
    """

    __tablename__ = "zoho_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)

    # UTC datetime at which the current access_token expires.
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ZohoToken expires_at={self.expires_at}>"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_authorization_url(state: str = "") -> str:
    """
    Build the Zoho OAuth2 authorization URL for the initial consent flow.

    Open this URL in a browser; after the user grants access Zoho will redirect
    to ``settings.zoho_redirect_uri`` with a ``code`` query parameter that can
    be exchanged for tokens via ``exchange_code_for_tokens``.

    Args:
        state: Optional opaque string echoed back by Zoho in the callback.

    Returns:
        Full authorization URL string.
    """
    params = {
        "scope": "ZohoBooks.fullaccess.all",
        "client_id": settings.zoho_client_id,
        "response_type": "code",
        "redirect_uri": settings.zoho_redirect_uri,
        "access_type": "offline",
    }
    if state:
        params["state"] = state
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{settings.zoho_accounts_url}/oauth/v2/auth?{query}"


def exchange_code_for_tokens(code: str, db: Session) -> ZohoToken:
    """
    Exchange a one-time authorization code for an access + refresh token pair
    and persist the result.

    Args:
        code: The ``code`` parameter received in the OAuth callback.
        db:   Open database session.

    Returns:
        The newly stored ``ZohoToken`` row.

    Raises:
        httpx.HTTPStatusError: If the Zoho accounts endpoint returns a non-2xx.
        ValueError: If the response body does not contain the expected fields.
    """
    resp = httpx.post(
        f"{settings.zoho_accounts_url}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "redirect_uri": settings.zoho_redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _validate_token_response(data)
    return _persist_tokens(
        db=db,
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_in=int(data.get("expires_in", 3600)),
    )


def get_valid_token(db: Session) -> str:
    """
    Return a valid Zoho access token, refreshing if necessary.

    If the stored token expires within ``_REFRESH_BUFFER_SECONDS`` (5 min),
    a refresh is performed automatically before the token is returned.

    The function also seeds the DB from ``settings.zoho_refresh_token`` if no
    token row exists yet (first-run bootstrap).

    Args:
        db: Open database session.

    Returns:
        A valid access token string.

    Raises:
        RuntimeError: If no refresh token is available (neither in DB nor env).
        httpx.HTTPStatusError: If the token refresh HTTP call fails.
    """
    token = db.get(ZohoToken, 1)

    if token is None:
        # Bootstrap from the env var on first run.
        refresh_token = settings.zoho_refresh_token
        if not refresh_token:
            raise RuntimeError(
                "No Zoho token in database and ZOHO_REFRESH_TOKEN env var is empty. "
                "Complete the OAuth consent flow first."
            )
        logger.info("Bootstrapping Zoho token from environment variable.")
        token = _do_refresh(db, refresh_token)
        return token.access_token

    # Check expiry.
    if _needs_refresh(token):
        logger.info("Zoho access token expiring soon – refreshing now.")
        token = _do_refresh(db, token.refresh_token)

    return token.access_token


def refresh_access_token(db: Session) -> str:
    """
    Force-refresh the Zoho access token regardless of current expiry.

    Useful for recovering from 401 responses during API calls.

    Args:
        db: Open database session.

    Returns:
        Fresh access token string.
    """
    token = db.get(ZohoToken, 1)
    if token is None:
        raise RuntimeError("No Zoho token in database – cannot force-refresh.")
    refreshed = _do_refresh(db, token.refresh_token)
    return refreshed.access_token


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _needs_refresh(token: ZohoToken) -> bool:
    """Return True if the token should be refreshed proactively."""
    if token.expires_at is None:
        return True
    now_utc = datetime.now(tz=timezone.utc).replace(tzinfo=None)
    buffer = timedelta(seconds=_REFRESH_BUFFER_SECONDS)
    return now_utc >= (token.expires_at - buffer)


def _do_refresh(db: Session, refresh_token: str) -> ZohoToken:
    """
    Call Zoho's token endpoint with a refresh_token grant and persist the result.

    Args:
        db:            Open database session.
        refresh_token: Current refresh token string.

    Returns:
        Updated ``ZohoToken`` row.
    """
    resp = httpx.post(
        f"{settings.zoho_accounts_url}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": settings.zoho_client_id,
            "client_secret": settings.zoho_client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"Zoho token refresh error: {data['error']}")

    return _persist_tokens(
        db=db,
        access_token=data["access_token"],
        # Zoho only returns a new refresh_token on code exchange, not on refresh.
        refresh_token=data.get("refresh_token", refresh_token),
        expires_in=int(data.get("expires_in", 3600)),
    )


def _persist_tokens(
    db: Session,
    access_token: str,
    refresh_token: str,
    expires_in: int,
) -> ZohoToken:
    """
    Upsert the ZohoToken row (always id=1) with the supplied values.

    Args:
        db:            Open database session.
        access_token:  New access token.
        refresh_token: Refresh token (may be unchanged after a refresh grant).
        expires_in:    Seconds until the access token expires.

    Returns:
        The persisted ``ZohoToken`` instance.
    """
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
    token = db.get(ZohoToken, 1)
    if token is None:
        token = ZohoToken(id=1)
        db.add(token)

    token.access_token = access_token
    token.refresh_token = refresh_token
    token.expires_at = expires_at
    db.commit()
    db.refresh(token)
    logger.debug("Zoho token persisted; expires_at=%s", expires_at.isoformat())
    return token


def _validate_token_response(data: dict) -> None:
    """Raise ValueError if the token response is missing required fields."""
    for field in ("access_token", "refresh_token"):
        if field not in data:
            raise ValueError(
                f"Zoho token response missing field '{field}': {data}"
            )
