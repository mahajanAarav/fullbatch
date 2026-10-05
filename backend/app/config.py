"""Settings, read from environment variables (and .env locally)."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    # Where PayPal sends buyers back to (this API) and where we send them on to (the React app).
    public_api_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"
    # From the webhook you create in the PayPal developer dashboard.
    paypal_webhook_id: str = ""
    # Deadlines a seller types without a timezone are read in this one.
    timezone: str = "America/New_York"
    # Local-only sign-in that skips PayPal. NEVER enable on the deployed app.
    dev_login: bool = False
    # Outgoing email (verification codes). Any SMTP provider works, e.g. Gmail with an app password or Brevo's free tier.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    # Address lookups are limited to these countries (comma-separated ISO codes), so a bare ZIP means a US ZIP.
    geo_countries: str = "us"
    # What fullbatch keeps from each filled drop before paying the seller the rest.
    platform_fee_percent: float = 5.0

    @property
    def cookie_secure(self) -> bool:
        """Browsers only send Secure cookies over https, which is what the deployed app uses."""
        return self.public_api_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", ""),
        public_api_url=os.getenv("PUBLIC_API_URL", "http://localhost:8000").rstrip("/"),
        frontend_url=os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/"),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        timezone=os.getenv("TIMEZONE", "America/New_York"),
        dev_login=os.getenv("DEV_LOGIN", "") == "1",
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587") or 587),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        email_from=os.getenv("EMAIL_FROM", ""),
        geo_countries=os.getenv("GEO_COUNTRIES", "us") or "us",
        platform_fee_percent=float(os.getenv("PLATFORM_FEE_PERCENT", "5") or 5),
    )
