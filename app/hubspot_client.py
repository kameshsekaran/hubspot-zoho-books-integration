"""
HubSpot CRM API client.

Wraps the HubSpot Private Apps REST API (v3).  Handles:

* Bearer-token authentication via ``settings.hubspot_access_token``.
* Exponential back-off retry on 429 and 5xx responses.
* Full request / response logging to ``IntegrationLog``.
* Fetching deals with company + contact associations in one call.
* Patching deal properties (custom ``zoho_*`` fields + standard fields).
* Looking up deal owners by ID.
* Creating HubSpot deal custom property groups and individual properties
  (used by the setup script).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.models.log import IntegrationLog
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_HUBSPOT_BASE = "https://api.hubapi.com"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class HubSpotAPIError(Exception):
    """Base class for HubSpot API errors."""

    def __init__(self, message: str, status_code: int = 0, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class HubSpotRateLimitError(HubSpotAPIError):
    """Raised on HTTP 429."""


class HubSpotTemporaryError(HubSpotAPIError):
    """Raised on 5xx errors."""


class HubSpotPermanentError(HubSpotAPIError):
    """Raised on permanent 4xx errors (not 429)."""


# ---------------------------------------------------------------------------
# HubSpotClient
# ---------------------------------------------------------------------------


class HubSpotClient:
    """
    HTTP client for the HubSpot Private Apps API.

    Args:
        db:        Open SQLAlchemy session for audit logging.
        deal_id:   HubSpot deal ID for log attribution (optional).
        direction: Sync direction label for log attribution (optional).
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
        self._token = settings.hubspot_access_token

    # ── Public methods ──────────────────────────────────────────────────────

    def get_deal(self, deal_id: str) -> dict:
        """
        Fetch a deal with its associated company and contact IDs.

        Args:
            deal_id: HubSpot deal object ID.

        Returns:
            Raw HubSpot deal response dict.
        """
        path = f"/crm/v3/objects/deals/{deal_id}"
        params = {
            "associations": "companies,contacts",
            "properties": (
                "dealname,amount,closedate,dealstage,deal_currency_code,"
                "hubspot_owner_id,hs_object_id"
            ),
        }
        return self._request("GET", path, params=params)

    def get_company(self, company_id: str) -> dict:
        """
        Fetch a company record.

        Args:
            company_id: HubSpot company object ID.

        Returns:
            Raw HubSpot company response dict.
        """
        path = f"/crm/v3/objects/companies/{company_id}"
        params = {"properties": "name,phone,address,city,country"}
        return self._request("GET", path, params=params)

    def get_contact(self, contact_id: str) -> dict:
        """
        Fetch a contact record.

        Args:
            contact_id: HubSpot contact object ID.

        Returns:
            Raw HubSpot contact response dict.
        """
        path = f"/crm/v3/objects/contacts/{contact_id}"
        params = {
            "properties": "firstname,lastname,email,phone,mobilephone"
        }
        return self._request("GET", path, params=params)

    def get_owner(self, owner_id: str) -> dict:
        """
        Fetch an owner (user) record.

        Args:
            owner_id: HubSpot owner ID.

        Returns:
            Raw HubSpot owner response dict.
        """
        path = f"/crm/v3/owners/{owner_id}"
        return self._request("GET", path)

    def patch_deal(self, deal_id: str, properties: dict[str, Any]) -> dict:
        """
        Update deal properties using a PATCH request.

        Args:
            deal_id:    HubSpot deal object ID.
            properties: Dict of property name → value to update.

        Returns:
            Raw HubSpot response dict.
        """
        path = f"/crm/v3/objects/deals/{deal_id}"
        body = {"properties": properties}
        return self._request("PATCH", path, body=body)

    def create_company(self, properties: dict[str, Any]) -> dict:
        """Create a new company in HubSpot."""
        return self._request("POST", "/crm/v3/objects/companies", body={"properties": properties})

    def create_contact(self, properties: dict[str, Any]) -> dict:
        """Create a new contact in HubSpot."""
        return self._request("POST", "/crm/v3/objects/contacts", body={"properties": properties})

    def create_deal(self, properties: dict[str, Any]) -> dict:
        """Create a new deal in HubSpot."""
        return self._request("POST", "/crm/v3/objects/deals", body={"properties": properties})

    def associate_deal_with_company(self, deal_id: str, company_id: str) -> dict:
        """Associate a deal with a company."""
        path = f"/crm/v3/objects/deals/{deal_id}/associations/companies/{company_id}/deal_to_company"
        return self._request("PUT", path)

    def associate_deal_with_contact(self, deal_id: str, contact_id: str) -> dict:
        """Associate a deal with a contact."""
        path = f"/crm/v3/objects/deals/{deal_id}/associations/contacts/{contact_id}/deal_to_contact"
        return self._request("PUT", path)

    # ── Property management ─────────────────────────────────────────────────

    def create_property_group(self, name: str, label: str) -> dict:
        """
        Create a custom deal property group.

        Args:
            name:  Internal group name (e.g. ``zoho_integration``).
            label: Display label shown in the HubSpot UI.

        Returns:
            Response dict (may be an error if group already exists).
        """
        body = {"name": name, "label": label, "displayOrder": -1}
        return self._request(
            "POST", "/crm/v3/properties/deals/groups", body=body
        )

    def create_property(self, prop: dict) -> dict:
        """
        Create a single custom deal property.

        Args:
            prop: Full property definition dict per HubSpot API spec.

        Returns:
            Response dict.
        """
        return self._request("POST", "/crm/v3/properties/deals", body=prop)

    def get_property(self, name: str) -> Optional[dict]:
        """
        Fetch a property definition by name.  Returns None if not found.

        Args:
            name: Property internal name.

        Returns:
            Property dict or None.
        """
        try:
            return self._request("GET", f"/crm/v3/properties/deals/{name}")
        except HubSpotPermanentError as exc:
            if exc.status_code == 404:
                return None
            raise

    def create_webhook_subscription(
        self,
        app_id: str,
        event_type: str,
        property_name: Optional[str] = None,
        target_url: Optional[str] = None,
    ) -> dict:
        """
        Create a webhook subscription for this private app.

        Args:
            app_id:        HubSpot private app ID.
            event_type:    e.g. ``deal.propertyChange``.
            property_name: Required for ``propertyChange`` events.
            target_url:    Webhook target URL.

        Returns:
            Created subscription dict.
        """
        body: dict = {
            "eventType": event_type,
            "active": True,
        }
        if property_name:
            body["propertyName"] = property_name
        path = f"/webhooks/v3/{app_id}/subscriptions"
        return self._request("POST", path, body=body)

    # ── Core request logic ──────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Dispatch an authenticated request to HubSpot with retry and logging."""
        url = _HUBSPOT_BASE + path
        start_ts = time.monotonic()
        response: Optional[httpx.Response] = None
        response_body: Optional[dict] = None
        error_msg: Optional[str] = None

        try:
            response = self._execute_with_retry(method, url, body, params)
            duration_ms = int((time.monotonic() - start_ts) * 1000)

            try:
                response_body = response.json()
            except Exception:
                response_body = {"raw": response.text}

            if response.status_code >= 400:
                if response.status_code == 429:
                    raise HubSpotRateLimitError(
                        "HubSpot rate limit", status_code=429, body=response_body
                    )
                raise HubSpotPermanentError(
                    f"HubSpot error {response.status_code}: {response_body}",
                    status_code=response.status_code,
                    body=response_body,
                )

            self._log(
                method=method,
                endpoint=url,
                request_body=body,
                response_body=response_body,
                status_code=response.status_code,
                duration_ms=duration_ms,
                success=True,
            )
            return response_body or {}

        except (HubSpotRateLimitError, HubSpotTemporaryError, HubSpotPermanentError):
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            error_msg = str(__import__("sys").exc_info()[1])
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
        retry=retry_if_exception_type((HubSpotRateLimitError, HubSpotTemporaryError)),
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
        """Execute the HTTP request with retry decoration."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=body,
            )

        if resp.status_code == 429:
            raise HubSpotRateLimitError("HubSpot rate limit", status_code=429)
        if resp.status_code >= 500:
            raise HubSpotTemporaryError(
                f"HubSpot server error {resp.status_code}", status_code=resp.status_code
            )
        return resp

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
        """Persist an IntegrationLog row."""
        try:
            entry = IntegrationLog(
                deal_id=self._deal_id,
                direction=self._direction,
                system="hubspot",
                method=method,
                endpoint=endpoint,
                request_json=json.dumps(request_body) if request_body else None,
                response_json=json.dumps(response_body) if response_body else None,
                status_code=status_code,
                duration_ms=duration_ms,
                success=success,
                error_message=error_message,
            )
            self._db.add(entry)
            self._db.commit()
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to write HubSpot log entry: %s", exc)

