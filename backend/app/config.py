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
    )
