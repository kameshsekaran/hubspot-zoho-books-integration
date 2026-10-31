"""
Base Zoho Books HTTP client.

All outbound requests to the Zoho Books API flow through ``ZohoClient``, which
handles:

* Automatic injection of Authorization and X-com-zoho-books-organizationid
  headers.
* Token-bucket rate limiting (max ``settings.zoho_rate_limit_per_min``
  requests per minute).
* Daily API call counter tracked in the DB (resets at midnight via scheduler).
* Exponential back-off retry for 429 and 5xx responses (via ``tenacity``).
* Full request / response logging to ``IntegrationLog``.
* Structured exception hierarchy for callers to handle.
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.models.log import IntegrationLog
from app.zoho.auth import get_valid_token, refresh_access_token

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ZohoAPIError(Exception):
    """Base class for all Zoho API errors raised by this client."""

    def __init__(self, message: str, status_code: int = 0, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ZohoRateLimitError(ZohoAPIError):
    """Raised when Zoho returns HTTP 429 (per-minute limit exceeded)."""


class ZohoDailyLimitError(ZohoAPIError):
    """Raised when the daily API call quota is exhausted."""


class ZohoTemporaryError(ZohoAPIError):
    """Raised for transient 5xx errors that may succeed on retry."""


class ZohoPermanentError(ZohoAPIError):
    """Raised for 4xx errors (except 429) that will not succeed on retry."""


# ---------------------------------------------------------------------------
# Simple in-process token-bucket rate limiter (thread-safe).
# ---------------------------------------------------------------------------


class _TokenBucket:
    """
    Token-bucket rate limiter.

    Tokens refill at a fixed rate (``rate_per_second``).  Each ``acquire``
    call blocks until a token is available.
    """

    def __init__(self, rate_per_minute: int) -> None:
        self._rate: float = rate_per_minute / 60.0  # tokens per second
        self._capacity: float = float(rate_per_minute)
        self._tokens: float = float(rate_per_minute)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_for = (1.0 - self._tokens) / self._rate
            time.sleep(wait_for)

    def _refill(self) -> None:
        """Add tokens proportional to elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


# Module-level singleton bucket; shared across all ZohoClient instances.
_rate_limiter = _TokenBucket(rate_per_minute=settings.zoho_rate_limit_per_min)

# In-memory daily call counter (scheduler resets this at midnight).
_daily_call_count: int = 0
_daily_count_lock = threading.Lock()


def reset_daily_counter() -> None:
    """Reset the in-memory daily API call counter.  Called by the scheduler."""
    global _daily_call_count
    with _daily_count_lock:
        _daily_call_count = 0
    logger.info("Zoho daily API call counter reset to 0.")


def get_daily_call_count() -> int:
    """Return the current value of the daily API call counter."""
    with _daily_count_lock:
        return _daily_call_count


# ---------------------------------------------------------------------------
# ZohoClient
# ---------------------------------------------------------------------------


class ZohoClient:
    """
    Thread-safe, rate-limited HTTP client for the Zoho Books API.

    Args:
        db:        Open SQLAlchemy session used for token retrieval and logging.
        deal_id:   Optional HubSpot deal ID for log attribution.
        direction: Sync direction label for log attribution.
    """

    def __init__(
        self,
        db: Session,
        deal_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> None:
        self._db = db
        self._deal_id = deal_id
        self._direction = direction
        self._base_url = settings.zoho_books_url
        self._org_id = settings.zoho_org_id

    # ── Public request methods ──────────────────────────────────────────────

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        """
        Perform a GET request to the Zoho Books API.

        Args:
            path:   URL path relative to the base URL, e.g. ``/salesorders``.
            params: Optional query-string parameters.

        Returns:
            Parsed JSON response body as a dict.
        """
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        """
        Perform a POST request to the Zoho Books API.

        Args:
            path:   URL path relative to the base URL.
            body:   JSON-serialisable request body dict.
            params: Optional query-string parameters.

        Returns:
            Parsed JSON response body as a dict.
        """
        return self._request("POST", path, body=body, params=params)

    def put(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        """
        Perform a PUT request to the Zoho Books API.

        Args:
            path:   URL path relative to the base URL.
            body:   JSON-serialisable request body dict.
            params: Optional query-string parameters.

        Returns:
            Parsed JSON response body as a dict.
        """
        return self._request("PUT", path, body=body, params=params)

    def delete(self, path: str, params: Optional[dict] = None) -> dict:
        """
        Perform a DELETE request to the Zoho Books API.

        Args:
            path:   URL path relative to the base URL.
            params: Optional query-string parameters.

        Returns:
            Parsed JSON response body as a dict.
        """
        return self._request("DELETE", path, params=params)

    # ── Core request implementation ─────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """
        Internal request dispatcher with rate limiting, retry, and logging.

        Args:
            method: HTTP method string.
            path:   URL path relative to base URL.
            body:   Optional JSON body for POST/PUT.
            params: Optional query-string params.

        Returns:
            Parsed response JSON as a dict.

        Raises:
            ZohoDailyLimitError: When daily quota is exhausted.
            ZohoRateLimitError:  On 429 responses.
            ZohoTemporaryError:  On transient 5xx errors.
            ZohoPermanentError:  On permanent 4xx errors.
        """
        global _daily_call_count

        # Daily quota guard.
        with _daily_count_lock:
            if _daily_call_count >= settings.zoho_daily_limit:
                raise ZohoDailyLimitError(
                    f"Zoho daily API limit of {settings.zoho_daily_limit} reached."
                )

        # Throttle to stay within per-minute rate limit.
        _rate_limiter.acquire()

        url = f"{self._base_url}{path}"
        start_ts = time.monotonic()
        response: Optional[httpx.Response] = None
        error_msg: Optional[str] = None
        response_body: Optional[dict] = None

        try:
            response = self._execute_with_retry(method, url, body, params)
            duration_ms = int((time.monotonic() - start_ts) * 1000)

            with _daily_count_lock:
                _daily_call_count += 1

            try:
                response_body = response.json()
            except Exception:
                response_body = {"raw": response.text}

            self._raise_for_zoho_status(response, response_body)

            self._log(
                method=method,
                endpoint=url,
                request_body=body,
                response_body=response_body,
                status_code=response.status_code,
                duration_ms=duration_ms,
                success=True,
            )
            return response_body

        except (ZohoRateLimitError, ZohoTemporaryError, ZohoPermanentError, ZohoDailyLimitError):
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            if response is not None:
                try:
                    response_body = response_body or response.json()
                except Exception:
                    response_body = {"raw": response.text}
            error_msg = str(sys_exc_info := __import__("sys").exc_info()[1])
            self._log(
                method=method,
                endpoint=url,
                request_body=body,
                response_body=response_body,
                status_code=response.status_code if response else None,
                duration_ms=duration_ms,
                success=False,
                error_message=error_msg,
            )
            raise

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            error_msg = str(exc)
            self._log(
                method=method,
                endpoint=url,
                request_body=body,
                response_body=response_body,
                status_code=response.status_code if response else None,
                duration_ms=duration_ms,
                success=False,
                error_message=error_msg,
            )
            raise

    @retry(
        retry=retry_if_exception_type((ZohoRateLimitError, ZohoTemporaryError)),
        wait=wait_exponential(multiplier=2, min=2, max=64),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _execute_with_retry(
        self,
        method: str,
        url: str,
        body: Optional[dict],
        params: Optional[dict],
    ) -> httpx.Response:
        """
        Execute the HTTP request, refreshing the token on 401.

        Decorated with ``tenacity`` to retry on rate-limit and server errors.

        Args:
            method: HTTP method.
            url:    Full request URL.
            body:   Request body (for POST/PUT).
            params: Query-string parameters.

        Returns:
            ``httpx.Response`` object.
        """
        access_token = get_valid_token(self._db)
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "X-com-zoho-books-organizationid": self._org_id,
            "Content-Type": "application/json",
        }
        merged_params = {"organization_id": self._org_id}
        if params:
            merged_params.update(params)

        with httpx.Client(timeout=30) as client:
            resp = client.request(
                method=method,
                url=url,
                headers=headers,
                params=merged_params,
                json=body,
            )

        # Handle 401 by force-refreshing the token once before retrying.
        if resp.status_code == 401:
            logger.warning("Zoho returned 401 – force-refreshing token.")
            refresh_access_token(self._db)
            access_token = get_valid_token(self._db)
            headers["Authorization"] = f"Zoho-oauthtoken {access_token}"
            with httpx.Client(timeout=30) as client:
                resp = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=merged_params,
                    json=body,
                )

        if resp.status_code == 429:
            raise ZohoRateLimitError("Zoho rate limit exceeded", status_code=429)
        if resp.status_code >= 500:
            raise ZohoTemporaryError(
                f"Zoho server error {resp.status_code}", status_code=resp.status_code
            )

        return resp

    def _raise_for_zoho_status(
        self, response: httpx.Response, body: Optional[dict]
    ) -> None:
        """
        Raise an appropriate exception for non-2xx Zoho API responses.

        Zoho sometimes returns 200 with a non-zero ``code`` field in the body.
        We handle both HTTP-level and application-level errors here.

        Args:
            response: The raw httpx response.
            body:     Parsed JSON body (may be None).
        """
        if response.status_code == 429:
            raise ZohoRateLimitError(
                "Zoho rate limit exceeded", status_code=429, body=body
            )
        if response.status_code >= 500:
            raise ZohoTemporaryError(
                f"Zoho server error {response.status_code}",
                status_code=response.status_code,
                body=body,
            )
        if response.status_code >= 400:
            raise ZohoPermanentError(
                f"Zoho client error {response.status_code}: {body}",
                status_code=response.status_code,
                body=body,
            )
        # Check application-level error codes embedded in 200 responses.
        if body and isinstance(body, dict) and body.get("code", 0) != 0:
            msg = body.get("message", "Unknown Zoho API error")
            zoho_code = body["code"]
            # 429-equivalent application codes
            if zoho_code in (4000, 4001):
                raise ZohoRateLimitError(msg, status_code=200, body=body)
            raise ZohoPermanentError(
                f"Zoho API error {zoho_code}: {msg}",
                status_code=200,
                body=body,
            )

    def _log(
        self,
        method: str,
        endpoint: str,
        request_body: Optional[dict],
        response_body: Optional[dict],
        status_code: Optional[int],
        duration_ms: int,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Persist an IntegrationLog row for the completed request.

        Args:
            method:        HTTP method string.
            endpoint:      Full request URL.
            request_body:  Request JSON body (None for GETs).
            response_body: Parsed response body.
            status_code:   HTTP status code.
            duration_ms:   Round-trip duration in milliseconds.
            success:       Whether the request succeeded.
            error_message: Short error description (if not success).
        """
        try:
            log_entry = IntegrationLog(
                deal_id=self._deal_id,
                direction=self._direction,
                system="zoho_books",
                method=method,
                endpoint=endpoint,
                request_json=json.dumps(request_body) if request_body else None,
                response_json=json.dumps(response_body) if response_body else None,
                status_code=status_code,
                duration_ms=duration_ms,
                success=success,
                error_message=error_message,
                created_at=datetime.utcnow(),
            )
            self._db.add(log_entry)
            self._db.commit()
        except Exception as log_exc:  # pragma: no cover
            logger.error("Failed to write integration log: %s", log_exc)
