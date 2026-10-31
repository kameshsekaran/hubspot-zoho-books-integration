"""
IntegrationLog ORM model.

Every outbound HTTP call made by this service (to HubSpot or Zoho Books) is
recorded as a row here.  This gives full auditability and is surfaced through
the /logs API endpoints.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IntegrationLog(Base):
    """
    Immutable audit record for a single outbound HTTP request.

    Fields are intentionally redundant (we store full request + response JSON)
    to make debugging self-contained without requiring external log aggregation.
    """

    __tablename__ = "integration_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # HubSpot deal ID that triggered this call (may be None for background ops).
    deal_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    # "crm_to_zoho" | "zoho_to_crm" | "setup" | "scheduler"
    direction: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # "hubspot" | "zoho_books"
    system: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # HTTP method: GET, POST, PATCH, PUT, DELETE
    method: Mapped[str] = mapped_column(String(8), nullable=False)

    # Full URL that was called (without query params that contain secrets).
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)

    # JSON-serialised request body (may be null for GET requests).
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # JSON-serialised response body.
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # HTTP status code returned by the remote API.
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # How long the call took in milliseconds.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # True when status_code indicates success (2xx).
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Short description of the error, if any.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<IntegrationLog id={self.id} system={self.system!r} "
            f"method={self.method!r} status={self.status_code} "
            f"success={self.success}>"
        )
