"""
HubSpot CRM API client.

Provides thin, logged wrappers around the HubSpot REST API v3 endpoints used
by this integration:

* Deal CRUD
* Company / contact association lookups
* Custom property updates
* Deal notes (Engagements API)

Every outbound request is recorded in ``IntegrationLog``.
"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.log import IntegrationLog

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.hubapi.com"
_DEALS_URL = f"{_BASE_URL}/crm/v3/objects/deals"
_ASSOCIATIONS_URL = f"{_BASE_URL}/crm/v3/associations"
_ENGAGEMENTS_URL = f"{_BASE_URL}/engagements/v1/engagements"
_PROPERTIES_URL = f"{_BASE_URL}/crm/v3/properties/deals"


class HubSpotClient:
    """
    HubSpot private-app API client.

    Args:
        db:        Open SQLAlchemy session used for request logging.
        deal_id:   Optional deal ID for log attribution.
        direction: Sync direction label for logs.
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
        self._headers = {
            "Authorization": f"Bearer {settings.hubspot_access_token}",
            "Content-Type": "application/json",
        }

    # ── Public methods ──────────────────────────────────────────────────────

    def get_deal(self, deal_id: str, extra_properties: Optional[list[str]] = None) -> dict:
        """
        Fetch a HubSpot deal with its properties.

        Args:
            deal_id:          HubSpot deal ID.
            extra_properties: Additional property names to include beyond
                              the default set.

        Returns:
            Deal dict with a ``properties`` sub-dict.
        """
        default_props = [
            "dealname", "amount", "dealstage", "pipeline",
            "closedate", "hubspot_owner_id", "description",
            "zoho_so_id", "zoho_so_number", "zoho_customer_id",
            "zoho_invoice_ids", "zoho_sync_status",
        ]
        props = list(set(default_props + (extra_properties or [])))
        params = {"properties": ",".join(props), "associations": "contacts,companies"}
        return self._request("GET", f"{_DEALS_URL}/{deal_id}", params=params)

    def get_deal_company(self, deal_id: str) -> Optional[dict]:
        """
        Fetch the first company associated with a HubSpot deal.

        Args:
            deal_id: HubSpot deal ID.

        Returns:
            Company dict or None if no association exists.
        """
        url = f"{_ASSOCIATIONS_URL}/deals/companies/batch/read"
        body = {"inputs": [{"id": deal_id}]}
        resp = self._request("POST", url, body=body)
        results = resp.get("results", [])
        if not results:
            return None
        company_ids = [a["id"] for a in results[0].get("to", [])]
        if not company_ids:
            return None
        return self._get_company(company_ids[0])

    def get_deal_contact(self, deal_id: str) -> Optional[dict]:
        """
        Fetch the first contact associated with a HubSpot deal.

        Args:
            deal_id: HubSpot deal ID.

        Returns:
            Contact dict or None.
        """
        url = f"{_ASSOCIATIONS_URL}/deals/contacts/batch/read"
        body = {"inputs": [{"id": deal_id}]}
        resp = self._request("POST", url, body=body)
        results = resp.get("results", [])
        if not results:
            return None
        contact_ids = [a["id"] for a in results[0].get("to", [])]
        if not contact_ids:
            return None
        return self._get_contact(contact_ids[0])

    def update_deal_properties(self, deal_id: str, properties: dict) -> dict:
        """
        Update HubSpot deal properties (PATCH – only changed fields).

        Args:
            deal_id:    HubSpot deal ID.
            properties: Dict mapping property name → new value.

        Returns:
            Updated deal dict from the API.
        """
        if not properties:
            logger.debug("update_deal_properties called with empty dict – skipping.")
            return {}
        body = {"properties": properties}
        return self._request("PATCH", f"{_DEALS_URL}/{deal_id}", body=body)

    def create_deal_note(self, deal_id: str, body_text: str) -> dict:
        """
        Add a plain-text note (Engagement) to a HubSpot deal.

        Args:
            deal_id:   HubSpot deal ID.
            body_text: Note content.

        Returns:
            Created engagement dict.
        """
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {
                "dealIds": [int(deal_id)],
                "contactIds": [],
                "companyIds": [],
                "ownerIds": [],
            },
            "metadata": {"body": body_text},
        }
        return self._request("POST", _ENGAGEMENTS_URL, body=payload)

    def list_deals_by_stage(self, stage_id: str, limit: int = 100) -> list[dict]:
        """
        Return all deals in the given pipeline stage.

        Args:
            stage_id: HubSpot pipeline stage ID string.
            limit:    Maximum number of results (up to 100 per page; this
                      implementation fetches a single page).

        Returns:
            List of deal dicts.
        """
        params = {
            "filterGroups": json.dumps([
                {
                    "filters": [
                        {
                            "propertyName": "dealstage",
                            "operator": "EQ",
                            "value": stage_id,
                        }
                    ]
                }
            ]),
            "properties": "dealname,amount,dealstage,closedate",
            "limit": limit,
        }
        resp = self._request("GET", f"{_BASE_URL}/crm/v3/objects/deals/search", params=params)
        return resp.get("results", [])

    def list_properties(self) -> list[dict]:
        """
        Return all deal property definitions from HubSpot.

        Returns:
            List of property dicts.
        """
        resp = self._request("GET", _PROPERTIES_URL)
        return resp.get("results", [])

    def create_property(self, prop_def: dict) -> dict:
        """
        Create a custom deal property in HubSpot.

        Args:
            prop_def: Property definition dict.

        Returns:
            Created property dict.
        """
        return self._request("POST", _PROPERTIES_URL, body=prop_def)

    # ── Private helpers ─────────────────────────────────────────────────────

    def _get_company(self, company_id: str) -> dict:
        """Fetch a single HubSpot company by ID."""
        props = "name,domain,city,country,phone,address,address2,zip"
        return self._request(
            "GET",
            f"{_BASE_URL}/crm/v3/objects/companies/{company_id}",
            params={"properties": props},
        )

    def _get_contact(self, contact_id: str) -> dict:
        """Fetch a single HubSpot contact by ID."""
        props = "firstname,lastname,email,phone,jobtitle"
        return self._request(
            "GET",
            f"{_BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            params={"properties": props},
        )

    def _request(
        self,
        method: str,
        url: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """
        Execute a single HTTP request to HubSpot and log the result.

        Args:
            method: HTTP method string.
            url:    Full request URL.
            body:   Optional JSON body.
            params: Optional query-string params.

        Returns:
            Parsed JSON response body.

        Raises:
            httpx.HTTPStatusError: On non-2xx responses after logging.
        """
        start_ts = time.monotonic()
        response: Optional[httpx.Response] = None
        response_body: Optional[dict] = None
        error_msg: Optional[str] = None
        success = False

        try:
            with httpx.Client(timeout=30) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=self._headers,
                    params=params,
                    json=body,
                )

            duration_ms = int((time.monotonic() - start_ts) * 1000)

            try:
                response_body = response.json()
            except Exception:
                response_body = {"raw": response.text}

            response.raise_for_status()
            success = True
            return response_body or {}

        except httpx.HTTPStatusError as exc:
            error_msg = f"HubSpot HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            raise

        except Exception as exc:
            error_msg = str(exc)
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            raise

        finally:
            self._log(
                method=method,
                endpoint=url,
                request_body=body,
                response_body=response_body,
                status_code=response.status_code if response else None,
                duration_ms=duration_ms,
                success=success,
                error_message=error_msg,
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
        """Persist an IntegrationLog row for the completed request."""
        try:
            log_entry = IntegrationLog(
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
                created_at=datetime.utcnow(),
            )
            self._db.add(log_entry)
            self._db.commit()
        except Exception as log_exc:  # pragma: no cover
            logger.error("Failed to write HubSpot integration log: %s", log_exc)
