"""
PayPal sandbox flow test for fullbatch.

Proves the three PayPal actions the product depends on:
  1. authorize - hold a buyer's payment without charging it
  2. capture   - charge a held payment   (the "drop fills" path)
  3. void      - release a held payment  (the "drop falls short" path)

Run from the repo root with the virtual environment active:
    python backend/scripts/test_paypal_flow.py
"""

import os
import sys
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Sandbox only. This script can never touch real money.
BASE_URL = "https://api-m.sandbox.paypal.com"
AMOUNT = "18.00"
CURRENCY = "USD"

# This file lives at backend/scripts/, so the repo root is two folders up.
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")


def fail(step: str, response: httpx.Response) -> None:
    """Print PayPal's full error and stop."""
    print(f"\nFAILED at: {step}")
    print(f"HTTP {response.status_code}")
    print(response.text)
    sys.exit(1)


def request_id() -> dict:
    """A unique ID per request so a retry can never double-charge."""
    return {"PayPal-Request-Id": str(uuid.uuid4())}


def get_token(client: httpx.Client) -> str:
    """Exchange the client ID and secret for a temporary access token."""
    r = client.post(
        "/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
    )
    if r.status_code != 200:
        fail("get access token", r)
    return r.json()["access_token"]


def create_order(client: httpx.Client, description: str) -> tuple[str, str]:
    """Create an order in AUTHORIZE mode. Returns (order_id, approval_link)."""
    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [
            {
                "description": description,
                "amount": {"currency_code": CURRENCY, "value": AMOUNT},
            }
        ],
        "payment_source": {
            "paypal": {
                "experience_context": {
                    "user_action": "PAY_NOW",
                    "shipping_preference": "NO_SHIPPING",
                    "return_url": "https://example.com/approved",
                    "cancel_url": "https://example.com/cancelled",
                }
            }
        },
    }
    r = client.post("/v2/checkout/orders", json=body, headers=request_id())
    if r.status_code not in (200, 201):
        fail("create order", r)
    order = r.json()
    link = next(
        (l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")),
        None,
    )
    if link is None:
        fail("find approval link in order response", r)
    return order["id"], link


def wait_for_approval(client: httpx.Client, order_id: str, link: str) -> None:
    """Show the approval link and wait until PayPal confirms the buyer approved."""
    print("\n  Open this link and log in as your sandbox BUYER (Personal) account:")
    print(f"  {link}\n")
    while True:
        input("  Press Enter after you approve the payment... ")
        r = client.get(f"/v2/checkout/orders/{order_id}")
        if r.status_code != 200:
            fail("check order status", r)
        status = r.json()["status"]
        if status == "APPROVED":
            print("  Buyer approval confirmed.")
            return
        print(f"  Order status is {status}, not APPROVED yet. Finish approving, then try again.")


def authorize_order(client: httpx.Client, order_id: str) -> dict:
    """Place the hold. Returns the authorization (id, status, expiration_time)."""
    r = client.post(
        f"/v2/checkout/orders/{order_id}/authorize",
        json={},
        headers=request_id(),
    )
    if r.status_code not in (200, 201):
        fail("authorize order", r)
    return r.json()["purchase_units"][0]["payments"]["authorizations"][0]


def capture_authorization(client: httpx.Client, authorization_id: str) -> dict:
    """Charge a held payment in full."""
    r = client.post(
        f"/v2/payments/authorizations/{authorization_id}/capture",
        json={"final_capture": True},
        headers={**request_id(), "Prefer": "return=representation"},
    )
    if r.status_code not in (200, 201):
        fail("capture authorization", r)
    return r.json()


def void_authorization(client: httpx.Client, authorization_id: str) -> None:
    """Release a held payment so the buyer is never charged."""
    r = client.post(
        f"/v2/payments/authorizations/{authorization_id}/void",
        headers={"Content-Type": "application/json"},
    )
    if r.status_code not in (200, 204):
        fail("void authorization", r)


def get_authorization_status(client: httpx.Client, authorization_id: str) -> str:
    """Ask PayPal for the current status of a hold."""
    r = client.get(f"/v2/payments/authorizations/{authorization_id}")
    if r.status_code != 200:
        fail("look up authorization", r)
    return r.json()["status"]


def hold_payment(client: httpx.Client, description: str) -> dict:
    """Shared first half of both rounds: create order, get approval, place hold."""
    order_id, link = create_order(client, description)
    print(f"  Order created: {order_id}")
    wait_for_approval(client, order_id, link)
    authorization = authorize_order(client, order_id)
    print(f"  Hold placed: {authorization['id']} (status {authorization['status']})")
    print(f"  Hold expires: {authorization.get('expiration_time', 'not provided')}")
    return authorization


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Missing PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET.")
        print(f"Add them to {REPO_ROOT / '.env'} and run again.")
        sys.exit(1)

    results: list[tuple[str, bool]] = []

    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        client.headers["Authorization"] = f"Bearer {get_token(client)}"
        print("Logged in to the PayPal sandbox.")

        print(f"\nROUND 1: hold ${AMOUNT}, then charge it (the drop fills)")
        auth_1 = hold_payment(client, "fullbatch test: drop fills")
        results.append(("Authorize (hold placed)", auth_1["status"] == "CREATED"))
        capture = capture_authorization(client, auth_1["id"])
        print(f"  Charged: {capture['id']} (status {capture['status']})")
        results.append(("Capture (hold charged)", capture["status"] == "COMPLETED"))

        print(f"\nROUND 2: hold ${AMOUNT}, then release it (the drop falls short)")
        auth_2 = hold_payment(client, "fullbatch test: drop falls short")
        void_authorization(client, auth_2["id"])
        status = get_authorization_status(client, auth_2["id"])
        print(f"  Released: {auth_2['id']} (status {status})")
        results.append(("Void (hold released)", status == "VOIDED"))

    print("\nSUMMARY")
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not all(passed for _, passed in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
