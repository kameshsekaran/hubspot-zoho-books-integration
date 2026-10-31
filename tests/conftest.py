"""
Pytest configuration and shared fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.job import IntegrationJob, JobStatus, JobType
from app.models.link import DealLink
from app.models.log import IntegrationLog

# ---------------------------------------------------------------------------
# In-memory test database
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite:///:memory:"

engine_test = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
)
SessionTest = sessionmaker(
    bind=engine_test, autocommit=False, autoflush=False, expire_on_commit=False
)


@pytest.fixture(scope="function")
def db():
    """Provide a clean in-memory SQLite session for each test."""
    # Import ZohoToken here to ensure it's registered on Base
    from app.zoho.auth import ZohoToken  # noqa: F401

    Base.metadata.create_all(bind=engine_test)
    session = SessionTest()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine_test)


@pytest.fixture(scope="function")
def client(db):
    """FastAPI test client with the test DB injected via dependency override."""
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# HubSpot fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_hs_deal():
    """A minimal HubSpot deal response dict."""
    return {
        "id": "hs_deal_789",
        "properties": {
            "dealname": "TechNova Solutions — ERP Implementation",
            "amount": "126000",
            "closedate": "1761753600000",  # epoch ms for 2025-10-30
            "dealstage": "closedwon",
            "deal_currency_code": "AED",
            "hubspot_owner_id": "owner_001",
        },
        "associations": {
            "companies": {"results": [{"id": "co_001"}]},
            "contacts": {"results": [{"id": "ct_001"}]},
        },
    }


@pytest.fixture
def mock_hs_company():
    return {
        "id": "co_001",
        "properties": {"name": "TechNova Solutions LLC", "phone": "+971-4-000-0000"},
    }


@pytest.fixture
def mock_hs_contact():
    return {
        "id": "ct_001",
        "properties": {
            "email": "cto@technova.ae",
            "firstname": "Mohammed",
            "lastname": "Al-Rashidi",
            "phone": "+971-50-000-0000",
        },
    }


@pytest.fixture
def mock_hs_owner():
    return {"firstName": "Alice", "lastName": "Chen", "id": "owner_001"}


# ---------------------------------------------------------------------------
# Zoho fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_zoho_customer():
    return {
        "contact_id": "zb_cust_456",
        "contact_name": "TechNova Solutions LLC",
        "email": "cto@technova.ae",
        "currency_code": "AED",
    }


@pytest.fixture
def mock_zoho_so():
    return {
        "salesorder_id": "zb_so_001",
        "salesorder_number": "SO-00042",
        "total": 126000,
        "status": "open",
    }


@pytest.fixture
def mock_zoho_invoices():
    return [
        {
            "invoice_id": "zb_inv_001",
            "invoice_number": "INV-0071",
            "total": 50400,
            "balance": 50400,
            "payment_made": 0,
            "status": "draft",
            "due_date": "2026-10-30",
        },
        {
            "invoice_id": "zb_inv_002",
            "invoice_number": "INV-0072",
            "total": 50400,
            "balance": 50400,
            "payment_made": 0,
            "status": "draft",
            "due_date": "2026-11-29",
        },
        {
            "invoice_id": "zb_inv_003",
            "invoice_number": "INV-0073",
            "total": 25200,
            "balance": 25200,
            "payment_made": 0,
            "status": "draft",
            "due_date": "2027-01-28",
        },
    ]


@pytest.fixture
def deal_link(db):
    """A pre-existing DealLink with Zoho IDs fully populated."""
    link = DealLink(
        hs_deal_id="hs_deal_789",
        zoho_customer_id="zb_cust_456",
        zoho_so_id="zb_so_001",
        zoho_so_number="SO-00042",
    )
    link.set_invoice_ids(["zb_inv_001", "zb_inv_002", "zb_inv_003"])
    db.add(link)
    db.commit()
    db.refresh(link)
    return link

