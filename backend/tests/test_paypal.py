"""Tests for the real PayPalClient, with the HTTP layer faked (no network)."""

import json
from decimal import Decimal

import httpx
import pytest

from app.paypal import SANDBOX_BASE_URL, PayPalClient, PayPalError


def make_client(handler):
    return PayPalClient("id", "secret", transport=httpx.MockTransport(handler))


def ok_handler(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if path == "/v2/checkout/orders":
            return httpx.Response(201, json={
                "id": "ORD1",
                "links": [{"rel": "payer-action", "href": "https://paypal.test/approve"}],
            })
        if path.endswith("/authorize"):
            return httpx.Response(201, json={"purchase_units": [{"payments": {"authorizations": [
                {"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-11-01T00:00:00Z"}]}}]})
        if path.endswith("/capture"):
            return httpx.Response(201, json={"status": "COMPLETED"})
        if path.endswith("/void"):
            return httpx.Response(204)
        return httpx.Response(404, text="unexpected path")
    return handler


def test_refuses_non_sandbox_url():
    with pytest.raises(ValueError):
        PayPalClient("id", "secret", base_url="https://api-m.paypal.com")


def test_requires_credentials():
    with pytest.raises(ValueError):
        PayPalClient("", "")


def test_token_is_fetched_once_and_reused():
    seen = []
    client = make_client(ok_handler(seen))
    client.void_authorization("A1")
    client.void_authorization("A2")
    assert sum(r.url.path == "/v1/oauth2/token" for r in seen) == 1


def test_every_post_has_a_request_id_and_they_differ():
    seen = []
    client = make_client(ok_handler(seen))
    client.void_authorization("A1")
    client.void_authorization("A1")
    ids = [r.headers["PayPal-Request-Id"] for r in seen if r.url.path.endswith("/void")]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_stable_request_id_is_passed_through():
    seen = []
    client = make_client(ok_handler(seen))
    client.capture_authorization("A1", request_id="capture-42")
    capture = next(r for r in seen if r.url.path.endswith("/capture"))
    assert capture.headers["PayPal-Request-Id"] == "capture-42"
    assert capture.headers["Authorization"] == "Bearer tok"


def test_create_order_sends_authorize_intent_and_returns_link():
    seen = []
    client = make_client(ok_handler(seen))
    order_id, link = client.create_order(
        amount=Decimal("18"), currency="USD", description="Loaf x2",
        return_url="https://x/ok", cancel_url="https://x/no", custom_id="7",
    )
    assert (order_id, link) == ("ORD1", "https://paypal.test/approve")
    body = json.loads(next(r for r in seen if r.url.path == "/v2/checkout/orders").content)
    assert body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"]["value"] == "18.00"
    assert body["purchase_units"][0]["custom_id"] == "7"


def test_authorize_returns_authorization():
    client = make_client(ok_handler([]))
    auth = client.authorize_order("ORD1")
    assert auth["id"] == "AUTH1" and auth["expiration_time"] == "2026-11-01T00:00:00Z"


def test_errors_become_paypal_error():
    def handler(request):
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(422, text="AUTHORIZATION_EXPIRED")

    client = make_client(handler)
    with pytest.raises(PayPalError) as err:
        client.capture_authorization("A1")
    assert err.value.status_code == 422 and "AUTHORIZATION_EXPIRED" in err.value.body


def test_bad_credentials_raise_paypal_error():
    client = make_client(lambda r: httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(PayPalError) as err:
        client.get_token()
    assert err.value.status_code == 401
