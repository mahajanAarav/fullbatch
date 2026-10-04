"""HTTP-level tests: real Postgres, fake PayPal."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import drops
from app.config import Settings, get_settings
from app.db import get_session
from app.main import app, get_paypal
from app.models import DropEvent, Order, OrderStatus
from tests.fakes import FakePayPal

SETTINGS = Settings(
    public_api_url="http://api.test",
    frontend_url="http://app.test",
    paypal_webhook_id="WH-TEST",
)


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def client(session_factory, paypal):
    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: paypal
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def make_drop(client, **overrides):
    seller = client.post("/sellers", json={"name": "Test Bakery"}).json()
    body = dict(
        seller_id=seller["id"], item_name="Sourdough", unit_price="9.00",
        quantity_total=10, minimum_units=5, deadline=iso(timedelta(days=3)),
    )
    body.update(overrides)
    return client.post("/drops", json=body)


def order_body(**overrides):
    body = dict(buyer_name="Ann", buyer_email="ann@example.com", chat_session_id="chat-1", quantity=2)
    body.update(overrides)
    return body


# ---- seller side -----------------------------------------------------------

def test_create_and_read_drop(client):
    r = make_drop(client)
    assert r.status_code == 201
    drop = r.json()
    assert drop["unit_price"] == "9.00" and drop["units_remaining"] == 10 and drop["status"] == "open"
    assert client.get(f"/drops/{drop['id']}").json()["item_name"] == "Sourdough"


@pytest.mark.parametrize("overrides", [
    {"unit_price": "0"},
    {"quantity_total": 0},
    {"deadline": "2030-01-01T12:00:00"},                      # no timezone
    {"minimum_units": 11},                                    # more than the quantity (engine rule)
    {"deadline": iso(timedelta(days=drops.MAX_DROP_DAYS + 1))},  # too far away (engine rule)
])
def test_bad_drops_are_rejected(client, overrides):
    assert make_drop(client, **overrides).status_code == 422


def test_unknown_drop_is_404(client):
    assert client.get("/drops/999").status_code == 404


# ---- buyer side ------------------------------------------------------------

def test_order_reserves_stock_and_returns_paypal_link(client):
    drop = make_drop(client).json()
    r = client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=3))
    assert r.status_code == 201
    body = r.json()
    assert body["amount"] == "27.00"
    assert body["approval_url"].startswith("https://fake.paypal/approve/")
    assert client.get(f"/drops/{drop['id']}").json()["units_remaining"] == 7


def test_sold_out_returns_409_with_remaining(client):
    drop = make_drop(client, quantity_total=3, minimum_units=1).json()
    client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=2))
    r = client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=2, buyer_name="Ben"))
    assert r.status_code == 409 and r.json()["remaining"] == 1


def test_bad_order_inputs(client):
    drop = make_drop(client).json()
    url = f"/drops/{drop['id']}/orders"
    assert client.post(url, json=order_body(buyer_email="nope")).status_code == 422
    assert client.post(url, json=order_body(quantity=5)).status_code == 422   # limit is 4
    assert client.post("/drops/999/orders", json=order_body()).status_code == 404


def test_paypal_failure_releases_the_reservation(client, paypal):
    drop = make_drop(client).json()
    paypal.fail_create = True
    r = client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=4))
    assert r.status_code == 502
    assert client.get(f"/drops/{drop['id']}").json()["units_remaining"] == 10  # stock came back


def test_return_page_places_the_hold_and_redirects(client):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    r = client.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    assert r.status_code == 303
    assert r.headers["location"] == f"http://app.test/orders/{order['order_id']}?status=authorized"
    assert client.get(f"/orders/{order['order_id']}").json()["status"] == "authorized"


def test_return_page_with_unknown_token_redirects_to_error(client):
    r = client.get("/paypal/return", params={"token": "NOPE"})
    assert r.status_code == 303 and r.headers["location"].endswith("?status=error")


def test_return_after_reservation_expired(client, session_factory):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    with session_factory() as s:
        drops.expire_stale_reservations(s, now=datetime.now(timezone.utc) + timedelta(hours=1))
    r = client.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    assert r.headers["location"].endswith("?status=expired")


def test_cancel_page_frees_the_stock(client):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=4)).json()
    r = client.get("/paypal/cancel", params={"token": f"PPO-{order['order_id']}"})
    assert r.headers["location"].endswith("?status=cancelled")
    assert client.get(f"/drops/{drop['id']}").json()["units_remaining"] == 10


def test_seller_can_cancel_a_drop(client, paypal):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    client.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    r = client.post(f"/drops/{drop['id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert client.get(f"/orders/{order['order_id']}").json()["status"] == "voided"
    assert len(paypal.voided) == 1


# ---- webhook ---------------------------------------------------------------

def approved_event(paypal_order_id):
    return {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": paypal_order_id}}


def test_webhook_rejects_a_bad_signature(client, paypal):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    paypal.webhook_valid = False
    r = client.post("/paypal/webhook", json=approved_event(f"PPO-{order['order_id']}"))
    assert r.status_code == 400
    assert client.get(f"/orders/{order['order_id']}").json()["status"] == "reserved"  # nothing happened


def test_webhook_places_the_hold(client):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    r = client.post("/paypal/webhook", json=approved_event(f"PPO-{order['order_id']}"))
    assert r.status_code == 200 and r.json() == {"handled": True}
    assert client.get(f"/orders/{order['order_id']}").json()["status"] == "authorized"


def test_duplicate_webhook_is_harmless(client, paypal):
    drop = make_drop(client).json()
    order = client.post(f"/drops/{drop['id']}/orders", json=order_body()).json()
    event = approved_event(f"PPO-{order['order_id']}")
    assert client.post("/paypal/webhook", json=event).status_code == 200
    assert client.post("/paypal/webhook", json=event).status_code == 200
    assert paypal.request_ids.count(f"authorize-{order['order_id']}") == 1


def test_webhook_ignores_other_events_and_unknown_orders(client):
    other = client.post("/paypal/webhook", json={"event_type": "PAYMENT.AUTHORIZATION.CREATED", "resource": {}})
    assert other.status_code == 200 and other.json() == {"handled": False}
    unknown = client.post("/paypal/webhook", json=approved_event("PPO-NOPE"))
    assert unknown.status_code == 200 and unknown.json() == {"handled": False}


def test_webhook_needs_configuration(client):
    app.dependency_overrides[get_settings] = lambda: Settings()  # no webhook id
    assert client.post("/paypal/webhook", json={}).status_code == 503


# ---- deadline job ----------------------------------------------------------

def paid_order(client, drop_id, quantity, name):
    order = client.post(
        f"/drops/{drop_id}/orders", json=order_body(quantity=quantity, buyer_name=name)
    ).json()
    client.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    return order["order_id"]


def test_deadline_job_settles_due_drops_only(client, session_factory, paypal):
    filled = make_drop(client, minimum_units=2).json()
    missed = make_drop(client, minimum_units=5).json()
    not_due = make_drop(client, deadline=iso(timedelta(days=10)), minimum_units=1).json()
    a = paid_order(client, filled["id"], 2, "Ann")
    b = paid_order(client, missed["id"], 1, "Ben")
    c = paid_order(client, not_due["id"], 1, "Cat")

    later = datetime.now(timezone.utc) + timedelta(days=4)
    summary = drops.run_deadline_job(session_factory, paypal, now=later)

    assert sorted(summary["settled"]) == sorted([filled["id"], missed["id"]])
    assert summary["errors"] == []
    assert client.get(f"/orders/{a}").json()["status"] == "captured"
    assert client.get(f"/orders/{b}").json()["status"] == "voided"
    assert client.get(f"/orders/{c}").json()["status"] == "authorized"  # drop not due yet


def test_deadline_job_expires_stale_reservations(client, session_factory, paypal):
    drop = make_drop(client).json()
    client.post(f"/drops/{drop['id']}/orders", json=order_body(quantity=4))
    summary = drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert summary["expired"] == 1
    assert client.get(f"/drops/{drop['id']}").json()["units_remaining"] == 10


def test_deadline_job_finishes_a_half_settled_drop(client, session_factory, paypal):
    drop = make_drop(client, minimum_units=2).json()
    a = paid_order(client, drop["id"], 1, "Ann")
    b = paid_order(client, drop["id"], 1, "Ben")
    later = datetime.now(timezone.utc) + timedelta(days=4)
    with session_factory() as s:
        crash_auth = s.get(Order, b).paypal_authorization_id
    paypal.crash_on_capture = crash_auth

    first = drops.run_deadline_job(session_factory, paypal, now=later)
    assert first["errors"] and client.get(f"/orders/{b}").json()["status"] == "authorized"

    second = drops.run_deadline_job(session_factory, paypal, now=later)  # picks up the leftover
    assert second["errors"] == []
    assert client.get(f"/orders/{a}").json()["status"] == "captured"
    assert client.get(f"/orders/{b}").json()["status"] == "captured"
    assert len(paypal.captured) == 2  # each charged exactly once


def test_deadline_job_one_bad_drop_does_not_block_others(client, session_factory, paypal):
    bad = make_drop(client, minimum_units=1).json()
    good = make_drop(client, minimum_units=1).json()
    paid_order(client, bad["id"], 1, "Ann")
    good_order = paid_order(client, good["id"], 1, "Ben")
    with session_factory() as s:
        paypal.crash_on_capture = s.scalar(
            select(Order.paypal_authorization_id).where(Order.drop_id == bad["id"])
        )
    summary = drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))
    assert len(summary["errors"]) == 1 and summary["errors"][0]["drop_id"] == bad["id"]
    assert client.get(f"/orders/{good_order}").json()["status"] == "captured"
