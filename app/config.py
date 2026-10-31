"""
Application configuration using pydantic-settings.

All values are read from environment variables or the .env file at startup.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central settings object for the entire application."""

    # ── HubSpot ────────────────────────────────────────────────────────────
    hubspot_access_token: str = ""
    hubspot_portal_id: str = ""
    hubspot_webhook_secret: str = ""
    hubspot_closed_won_stage_id: str = "closedwon"

    # ── Zoho Books ─────────────────────────────────────────────────────────
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_redirect_uri: str = "http://localhost:8000/zoho/callback"
    zoho_refresh_token: str = ""
    zoho_org_id: str = ""
    zoho_region: str = "com"  # 'com', 'eu', 'in', 'au', 'jp'

    # ── Database ───────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./integration.db"

    # ── App ────────────────────────────────────────────────────────────────
    secret_key: str = "change-me"
    log_level: str = "INFO"
    environment: str = "development"

    # ── Business rules ─────────────────────────────────────────────────────
    vat_rate: float = 0.05  # 5% UAE VAT

    # ── Zoho webhook shared secret ─────────────────────────────────────────
    zoho_webhook_secret: str = ""

    # ── API key for internal endpoints ─────────────────────────────────────
    api_key: str = "dev-api-key"

    # ── Notifications (optional) ───────────────────────────────────────────
    slack_webhook_url: str = ""
    alert_email: str = ""

    # ── Rate limiting ──────────────────────────────────────────────────────
    zoho_rate_limit_per_min: int = 60
    zoho_daily_limit: int = 2000

    # ── Computed Zoho URLs ─────────────────────────────────────────────────
    @property
    def zoho_accounts_url(self) -> str:
        """Base URL for Zoho OAuth token operations."""
        return f"https://accounts.zoho.{self.zoho_region}"

    @property
    def zoho_books_url(self) -> str:
        """Base URL for Zoho Books REST API v3."""
        return f"https://www.zohoapis.{self.zoho_region}/books/v3"

    @property
    def hubspot_api_url(self) -> str:
        """Base URL for HubSpot API."""
        return "https://api.hubapi.com"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
