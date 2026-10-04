"""
PayPal sandbox client for fullbatch.

Small wrapper around the PayPal REST API (Orders v2 and Payments v2):
  create_order            start a checkout, returns the link the buyer approves
  authorize_order         place a hold on the approved payment (no charge)
  capture_authorization   charge a hold
  void_authorization      release a hold

Rules enforced here:
  - Sandbox only. The client refuses to talk to any other host.
  - Every POST carries a PayPal-Request-Id. Pass a stable one per logical
    action (e.g. "capture-42") so a retry returns PayPal's first result
    instead of charging twice. If you pass none, a random one is used.
  - The LLM never calls this module. Only backend code does.
"""

import os
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import load_dotenv

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """PayPal answered with an error, or the response was not what we expected."""

    def __init__(self, step: str, status_code: int, body: str):
        self.step = step
        self.status_code = status_code
        self.body = body
        super().__init__(f"PayPal {step} failed (HTTP {status_code}): {body[:300]}")


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = SANDBOX_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        if base_url != SANDBOX_BASE_URL:
            raise ValueError("fullbatch only talks to the PayPal sandbox.")
        if not client_id or not client_secret:
            raise ValueError("PayPal client ID and secret are required.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=base_url, timeout=30, transport=transport)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    # ---- plumbing ---------------------------------------------------------

    def get_token(self) -> str:
        """Return a valid access token, fetching a new one only when needed."""
        with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            r = self._http.post(
                "/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
            )
            if r.status_code != 200:
                raise PayPalError("get access token", r.status_code, r.text)
            data = r.json()
            self._token = data["access_token"]
            # Refresh a minute early so a token never expires mid-request.
            self._token_expires_at = time.monotonic() + max(data.get("expires_in", 300) - 60, 0)
            return self._token

    def _request(
        self,
        method: str,
        path: str,
        *,
        step: str,
        ok: tuple[int, ...] = (200, 201),
        json: dict | None = None,
        request_id: str | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        all_headers = {"Authorization": f"Bearer {self.get_token()}"}
        if method == "POST":
            all_headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
        if headers:
            all_headers.update(headers)
        r = self._http.request(method, path, json=json, headers=all_headers)
        if r.status_code not in ok:
            raise PayPalError(step, r.status_code, r.text)
        return r

    # ---- the four actions -------------------------------------------------

    def create_order(
        self,
        *,
        amount: Decimal,
        currency: str,
        description: str,
        return_url: str,
        cancel_url: str,
        custom_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[str, str]:
        """Create an AUTHORIZE-mode order. Returns (paypal_order_id, approval_link)."""
        unit: dict = {
            "description": description[:127],
            "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
        }
        if custom_id:
            unit["custom_id"] = custom_id  # our own order id, echoed back in webhooks
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [unit],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        r = self._request(
            "POST", "/v2/checkout/orders", step="create order", json=body, request_id=request_id
        )
        order = r.json()
        link = next(
            (l["href"] for l in order.get("links", []) if l["rel"] in ("payer-action", "approve")),
            None,
        )
        if link is None:
            raise PayPalError("find approval link", r.status_code, r.text)
        return order["id"], link

    def authorize_order(self, paypal_order_id: str, *, request_id: str | None = None) -> dict:
        """Place the hold on an approved order. Returns id, status and expiration_time."""
        r = self._request(
            "POST",
            f"/v2/checkout/orders/{paypal_order_id}/authorize",
            step="authorize order",
            json={},
            request_id=request_id,
        )
        try:
            return r.json()["purchase_units"][0]["payments"]["authorizations"][0]
        except (KeyError, IndexError):
            raise PayPalError("read authorization", r.status_code, r.text)

    def capture_authorization(self, authorization_id: str, *, request_id: str | None = None) -> dict:
        """Charge a hold in full."""
        r = self._request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/capture",
            step="capture authorization",
            json={"final_capture": True},
            request_id=request_id,
            headers={"Prefer": "return=representation"},
        )
        return r.json()

    def void_authorization(self, authorization_id: str, *, request_id: str | None = None) -> None:
        """Release a hold so the buyer is never charged."""
        self._request(
            "POST",
            f"/v2/payments/authorizations/{authorization_id}/void",
            step="void authorization",
            ok=(200, 204),
            request_id=request_id,
            headers={"Content-Type": "application/json"},
        )

    def verify_webhook_signature(self, *, headers, event: dict, webhook_id: str) -> bool:
        """
        Ask PayPal whether a webhook really came from PayPal.
        `headers` are the request headers PayPal sent (lookup is case-insensitive in
        FastAPI/Starlette). Never trust a webhook without calling this first.
        """
        fields = {
            "transmission_id": "paypal-transmission-id",
            "transmission_time": "paypal-transmission-time",
            "cert_url": "paypal-cert-url",
            "auth_algo": "paypal-auth-algo",
            "transmission_sig": "paypal-transmission-sig",
        }
        body = {name: headers.get(header) for name, header in fields.items()}
        if not all(body.values()):
            return False  # missing signature headers: cannot be a real PayPal webhook
        body["webhook_id"] = webhook_id
        body["webhook_event"] = event
        r = self._request(
            "POST", "/v1/notifications/verify-webhook-signature",
            step="verify webhook signature", json=body, ok=(200,),
        )
        return r.json().get("verification_status") == "SUCCESS"

    def close(self) -> None:
        self._http.close()


def from_env() -> PayPalClient:
    """Build a client from PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env."""
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return PayPalClient(os.getenv("PAYPAL_CLIENT_ID", ""), os.getenv("PAYPAL_CLIENT_SECRET", ""))
