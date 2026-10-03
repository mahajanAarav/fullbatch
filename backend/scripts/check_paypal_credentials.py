"""
Quick check that the PayPal sandbox credentials in .env work.

Requests an access token and prints only the result. It never prints
the client ID, the secret, or the token.

Run from the repo root with the virtual environment active:
    python backend/scripts/check_paypal_credentials.py
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Sandbox only.
BASE_URL = "https://api-m.sandbox.paypal.com"

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET in .env.")
        sys.exit(1)

    r = httpx.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    if r.status_code == 200:
        print("OK: PayPal accepted the credentials (HTTP 200).")
        return

    error = r.json().get("error", "unknown") if r.headers.get("content-type", "").startswith("application/json") else "unknown"
    print(f"FAILED: HTTP {r.status_code}, error: {error}")
    if error == "invalid_client":
        print("PayPal does not recognize this client ID / secret pair.")
        print("Create a Merchant sandbox app and copy both values from its page.")
    sys.exit(1)


if __name__ == "__main__":
    main()
