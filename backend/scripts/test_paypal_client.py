"""
Real-sandbox check of app/paypal.py (the PayPalClient the app actually uses).

Beyond authorize / capture / void, this checks the retry-safety claim: sending
the same POST twice with the same PayPal-Request-Id should return the FIRST
result instead of performing the action again. That is what stops a retry
from charging a buyer twice.

INTERACTIVE: you must open the printed links and approve as the sandbox BUYER.

Run from the repo root with the virtual environment active:
    python backend/scripts/test_paypal_client.py
"""

import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.paypal import PayPalError, from_env  # noqa: E402

RUN = uuid.uuid4().hex[:8]  # makes request ids unique to this run
results: list[tuple[str, bool]] = []


def check(name: str, passed: bool) -> None:
    results.append((name, passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")


def new_order(client, label: str) -> str:
    """Create an order twice with the same request id and have the buyer approve it."""
    rid = f"test-{RUN}-create-{label}"
    kwargs = dict(
        amount=Decimal("18.00"), currency="USD", description=f"fullbatch client test {label}",
        return_url="https://example.com/approved", cancel_url="https://example.com/cancelled",
        custom_id=f"test-{label}", request_id=rid,
    )
    order_id, link = client.create_order(**kwargs)
    again_id, _ = client.create_order(**kwargs)
    check(f"[{label}] same request id returns the same PayPal order", order_id == again_id)

    print(f"\n  Open this link and log in as your sandbox BUYER (Personal) account:\n  {link}\n")
    while True:
        input("  Press Enter after you approve the payment... ")
        status = client._request("GET", f"/v2/checkout/orders/{order_id}", step="check order").json()["status"]
        if status == "APPROVED":
            return order_id
        print(f"  Order status is {status}, not APPROVED yet.")


def authorize_twice(client, order_id: str, label: str) -> dict:
    rid = f"test-{RUN}-authorize-{label}"
    first = client.authorize_order(order_id, request_id=rid)
    second = client.authorize_order(order_id, request_id=rid)
    print(f"  Hold: {first['id']} expires {first.get('expiration_time')}")
    check(f"[{label}] authorize with same request id returns the same hold", first["id"] == second["id"])
    return first


def main() -> None:
    client = from_env()
    client.get_token()
    print("Logged in to the PayPal sandbox using app/paypal.py.")

    print("\nROUND 1: hold, then charge twice with the same request id")
    order_id = new_order(client, "r1")
    auth = authorize_twice(client, order_id, "r1")
    check("[r1] hold status is CREATED", auth["status"] == "CREATED")
    rid = f"test-{RUN}-capture-r1"
    cap1 = client.capture_authorization(auth["id"], request_id=rid)
    cap2 = client.capture_authorization(auth["id"], request_id=rid)
    print(f"  Charged: {cap1['id']} (status {cap1['status']})")
    check("[r1] capture succeeded", cap1["status"] == "COMPLETED")
    check("[r1] repeated capture is the SAME charge, not a second one", cap1["id"] == cap2["id"])
    try:
        client.capture_authorization(auth["id"], request_id=f"test-{RUN}-capture-r1-different")
        check("[r1] a capture with a NEW request id is refused (already captured)", False)
    except PayPalError as err:
        print(f"  (A new request id was refused as expected: HTTP {err.status_code})")
        check("[r1] a capture with a NEW request id is refused (already captured)", True)

    print("\nROUND 2: hold, then release")
    order_id = new_order(client, "r2")
    auth = authorize_twice(client, order_id, "r2")
    rid = f"test-{RUN}-void-r2"
    client.void_authorization(auth["id"], request_id=rid)
    try:
        client.void_authorization(auth["id"], request_id=rid)
        check("[r2] repeated void with same request id is accepted", True)
    except PayPalError as err:
        print(f"  (Repeated void answered HTTP {err.status_code}: {err.body[:120]})")
        check("[r2] repeated void with same request id is accepted", False)
    time.sleep(1)
    status = client._request("GET", f"/v2/payments/authorizations/{auth['id']}", step="check hold").json()["status"]
    print(f"  Hold status: {status}")
    check("[r2] hold is VOIDED", status == "VOIDED")

    print("\nSUMMARY")
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    client.close()
    if not all(p for _, p in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
