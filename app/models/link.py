"""
DealLink ORM model.

Stores the mapping between a HubSpot deal and all Zoho Books objects that were
created for it (customer, sales order, invoices, payments).  Also tracks a
``sync_state`` snapshot so the zoho→crm sync can detect which fields changed
and avoid unnecessary HubSpot API calls.
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DealLink(Base):
    """
    Bi-directional ID mapping: HubSpot deal ↔ Zoho Books objects.

    One row per HubSpot deal; updated in-place as new Zoho objects are created
    or as payment/invoice state changes.
    """

    __tablename__ = "deal_links"
    __table_args__ = (UniqueConstraint("hs_deal_id", name="uq_deal_links_hs_deal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # HubSpot deal ID (string format that HubSpot uses)
    hs_deal_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    # ── Zoho IDs ────────────────────────────────────────────────────────────
    zoho_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zoho_so_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    zoho_so_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # JSON arrays of invoice / payment IDs, e.g. '["INV-001", "INV-002"]'
    zoho_invoice_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    zoho_payment_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Sync state snapshot (last values written to HubSpot) ────────────────
    # Stored as JSON so we can compare arbitrary dicts without defining
    # individual columns for each tracked field.
    sync_state: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamps ──────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Helpers ─────────────────────────────────────────────────────────────

    def get_invoice_ids(self) -> list[str]:
        """Return the list of Zoho invoice IDs (deserialised from JSON)."""
        if not self.zoho_invoice_ids:
            return []
        return json.loads(self.zoho_invoice_ids)

    def set_invoice_ids(self, ids: list[str]) -> None:
        """Serialise and store a list of Zoho invoice IDs."""
        self.zoho_invoice_ids = json.dumps(ids)

    def get_payment_ids(self) -> list[str]:
        """Return the list of Zoho payment IDs (deserialised from JSON)."""
        if not self.zoho_payment_ids:
            return []
        return json.loads(self.zoho_payment_ids)

    def set_payment_ids(self, ids: list[str]) -> None:
        """Serialise and store a list of Zoho payment IDs."""
        self.zoho_payment_ids = json.dumps(ids)

    def get_sync_state(self) -> dict[str, Any]:
        """Return the last-synced property snapshot as a dict."""
        if not self.sync_state:
            return {}
        return json.loads(self.sync_state)

    def set_sync_state(self, state: dict[str, Any]) -> None:
        """Serialise and store the current sync-state snapshot."""
        self.sync_state = json.dumps(state)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<DealLink id={self.id} hs_deal_id={self.hs_deal_id!r} "
            f"zoho_so_id={self.zoho_so_id!r}>"
        )
