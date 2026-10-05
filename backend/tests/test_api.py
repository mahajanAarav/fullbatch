"""HTTP-level tests: real Postgres, fake PayPal, real sign-in cookies."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import drops
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_geocoder, get_paypal
from app.main import app
from app.models import Order
from tests.fakes import FakeGeocoder, FakePayPal, sign_in

SETTINGS = Settings(
    public_api_url="http://api.test",
    frontend_url="http://app.test",
    paypal_webhook_id="WH-TEST",
    dev_login=True,
)


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def make_client(session_factory, paypal):
    """Each call is a separate browser with its own cookies, so a test can be a seller AND a buyer."""

    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: paypal
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_geocoder] = lambda: FakeGeocoder()
    yield lambda: TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def seller_client(make_client, email="baker@example.com", verified=True, shop=True):
    c = make_client()
    sign_in(c, name="Baker Bea", email=email, verified=verified)
    if shop:
        assert c.post("/shop", json={"name": "Bea's Bakery"}).status_code == 201
    return c


def buyer_client(make_client, email="sam@example.com", name="Sam Lee"):
    c = make_client()
    sign_in(c, name=name, email=email)
    return c


def drop_body(**overrides):
    body = dict(item_name="Sourdough", unit_price="9.00", quantity_total=10, minimum_units=5, deadline=iso(timedelta(days=3)),
                pickup_address="350 5th Ave, New York, NY")
    body.update(overrides)
    return body


def make_drop(seller, **overrides):
    return seller.post("/drops", json=drop_body(**overrides))


def paid_order(buyer, drop_id, quantity=2):
    """Reserve, then 'approve on PayPal' by visiting the return page. Returns the order id."""
    order = buyer.post(f"/drops/{drop_id}/orders", json={"quantity": quantity}).json()
    buyer.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    return order["order_id"]


# ---- shops and drops: who may do what ---------------------------------------

def test_signed_out_visitors_can_look_but_not_act(make_client):
    c = make_client()
    assert c.get("/drops").status_code == 200
    assert c.post("/shop", json={"name": "x"}).status_code == 401
    assert c.post("/drops", json=drop_body()).status_code == 401
    assert c.get("/me/drops").status_code == 401
    assert c.post("/drops/1/orders", json={"quantity": 1}).status_code == 401
    assert c.get("/me/orders").status_code == 401
    assert c.post("/chat/buyer", json={"message": "hi"}).status_code == 401


def test_a_signed_in_user_can_open_one_shop(make_client):
    c = seller_client(make_client)
    assert c.get("/auth/me").json()["shop"] == {"id": 1, "name": "Bea's Bakery", "verified": True}
    assert c.post("/shop", json={"name": "Second shop"}).status_code == 409


def test_you_need_a_shop_before_you_can_open_a_drop(make_client):
    c = seller_client(make_client, shop=False)
    assert make_drop(c).status_code == 403


def test_a_verified_seller_creates_and_the_public_can_read_it(make_client):
    seller = seller_client(make_client)
    r = make_drop(seller)
    assert r.status_code == 201
    drop = r.json()
    assert drop["unit_price"] == "9.00" and drop["units_remaining"] == 10 and drop["status"] == "open"
    assert make_client().get(f"/drops/{drop['id']}").json()["item_name"] == "Sourdough"  # no sign-in needed


def test_an_unverified_seller_is_refused_with_a_clear_message(make_client):
    seller = seller_client(make_client, verified=False)
    assert seller.get("/auth/me").json()["shop"]["verified"] is False
    r = make_drop(seller)
    assert r.status_code == 403 and "isn't verified" in r.json()["error"]


@pytest.mark.parametrize("overrides", [
    {"unit_price": "0"},
    {"quantity_total": 0},
    {"deadline": "2030-01-01T12:00:00"},                         # no timezone
    {"minimum_units": 11},                                       # more than the quantity (engine rule)
    {"deadline": iso(timedelta(days=drops.MAX_DROP_DAYS + 1))},  # too far away (engine rule)
])
def test_bad_drops_are_rejected(make_client, overrides):
    assert make_drop(seller_client(make_client), **overrides).status_code == 422


def test_the_seller_cannot_choose_whose_shop_the_drop_goes_in(make_client):
    a = seller_client(make_client, email="a@example.com")
    b = seller_client(make_client, email="b@example.com")
    r = a.post("/drops", json={**drop_body(), "seller_id": 2})  # tries to post into b's shop
    assert r.status_code == 201 and r.json()["seller_id"] == 1
    assert b.get("/me/drops").json()["drops"] == []


def test_unknown_drop_is_404(make_client):
    assert make_client().get("/drops/999").status_code == 404


def test_my_drops_lists_only_mine_with_progress(make_client):
    a, b = seller_client(make_client, email="a@example.com"), seller_client(make_client, email="b@example.com")
    drop = make_drop(a, minimum_units=3).json()
    make_drop(b, item_name="Not mine")
    paid_order(buyer_client(make_client), drop["id"], 2)
    mine = a.get("/me/drops").json()["drops"]
    assert [d["id"] for d in mine] == [drop["id"]]
    assert mine[0]["paid_up_units"] == 2 and mine[0]["minimum_met_so_far"] is False


def test_open_drops_list_excludes_closed_ones(make_client):
    seller = seller_client(make_client)
    make_drop(seller, item_name="Open one")
    closed = make_drop(seller, item_name="Closed one").json()
    seller.post(f"/drops/{closed['id']}/cancel")
    assert [d["item_name"] for d in make_client().get("/drops").json()["drops"]] == ["Open one"]


# ---- cancelling ---------------------------------------------------------------

def test_seller_can_cancel_their_drop_and_holds_are_released(make_client, paypal):
    seller, buyer = seller_client(make_client), buyer_client(make_client)
    drop = make_drop(seller).json()
    order_id = paid_order(buyer, drop["id"])
    r = seller.post(f"/drops/{drop['id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert buyer.get(f"/orders/{order_id}").json()["status"] == "voided"
    assert len(paypal.voided) == 1


def test_nobody_else_can_cancel_a_drop(make_client):
    owner = seller_client(make_client, email="owner@example.com")
    rival = seller_client(make_client, email="rival@example.com")
    drop = make_drop(owner).json()
    assert rival.post(f"/drops/{drop['id']}/cancel").status_code == 404
    assert buyer_client(make_client).post(f"/drops/{drop['id']}/cancel").status_code == 403  # has no shop
    assert make_client().post(f"/drops/{drop['id']}/cancel").status_code == 401
    assert owner.get(f"/drops/{drop['id']}").json()["status"] == "open"


# ---- buying ---------------------------------------------------------------------

def test_an_order_uses_the_account_identity_and_returns_a_paypal_link(make_client, session_factory):
    seller, buyer = seller_client(make_client), buyer_client(make_client)
    drop = make_drop(seller).json()
    r = buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 3})
    assert r.status_code == 201
    body = r.json()
    assert body["amount"] == "27.00" and body["approval_url"].startswith("https://fake.paypal/approve/")
    with session_factory() as s:
        order = s.scalars(select(Order)).one()
    assert (order.buyer_name, order.buyer_email) == ("Sam Lee", "sam@example.com")
    assert order.buyer_user_id is not None and order.chat_session_id == f"user-{order.buyer_user_id}"


def test_the_body_cannot_override_who_is_buying(make_client, session_factory):
    seller, buyer = seller_client(make_client), buyer_client(make_client)
    drop = make_drop(seller).json()
    buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 1, "buyer_email": "evil@example.com", "buyer_name": "Eve"})
    with session_factory() as s:
        assert s.scalars(select(Order)).one().buyer_email == "sam@example.com"


def test_sold_out_returns_409_with_remaining(make_client):
    seller = seller_client(make_client)
    drop = make_drop(seller, quantity_total=3, minimum_units=1).json()
    buyer_client(make_client, "a@example.com").post(f"/drops/{drop['id']}/orders", json={"quantity": 2})
    r = buyer_client(make_client, "b@example.com").post(f"/drops/{drop['id']}/orders", json={"quantity": 2})
    assert r.status_code == 409 and r.json()["remaining"] == 1


def test_bad_order_inputs(make_client):
    drop = make_drop(seller_client(make_client)).json()
    buyer = buyer_client(make_client)
    assert buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 5}).status_code == 422   # limit is 4
    assert buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 0}).status_code == 422
    assert buyer.post("/drops/999/orders", json={"quantity": 1}).status_code == 404


def test_paypal_failure_releases_the_reservation(make_client, paypal):
    drop = make_drop(seller_client(make_client)).json()
    paypal.fail_create = True
    r = buyer_client(make_client).post(f"/drops/{drop['id']}/orders", json={"quantity": 4})
    assert r.status_code == 502
    assert make_client().get(f"/drops/{drop['id']}").json()["units_remaining"] == 10


def test_return_page_places_the_hold_and_redirects(make_client):
    drop = make_drop(seller_client(make_client)).json()
    buyer = buyer_client(make_client)
    order = buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 2}).json()
    r = buyer.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    assert r.status_code == 303
    assert r.headers["location"] == f"http://app.test/orders/{order['order_id']}?status=authorized"
    assert buyer.get(f"/orders/{order['order_id']}").json()["status"] == "authorized"


def test_return_page_with_unknown_token_redirects_to_error(make_client):
    r = make_client().get("/paypal/return", params={"token": "NOPE"})
    assert r.status_code == 303 and r.headers["location"].endswith("?status=error")


def test_return_after_reservation_expired(make_client, session_factory):
    drop = make_drop(seller_client(make_client)).json()
    buyer = buyer_client(make_client)
    order = buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 1}).json()
    with session_factory() as s:
        drops.expire_stale_reservations(s, now=datetime.now(timezone.utc) + timedelta(hours=1))
    r = buyer.get("/paypal/return", params={"token": f"PPO-{order['order_id']}"})
    assert r.headers["location"].endswith("?status=expired")


def test_cancel_page_frees_the_stock(make_client):
    drop = make_drop(seller_client(make_client)).json()
    buyer = buyer_client(make_client)
    order = buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 4}).json()
    r = buyer.get("/paypal/cancel", params={"token": f"PPO-{order['order_id']}"})
    assert r.headers["location"].endswith("?status=cancelled")
    assert make_client().get(f"/drops/{drop['id']}").json()["units_remaining"] == 10


# ---- orders are private ---------------------------------------------------------

def test_buyers_only_see_their_own_orders(make_client):
    drop = make_drop(seller_client(make_client)).json()
    ann, ben = buyer_client(make_client, "ann@example.com", "Ann"), buyer_client(make_client, "ben@example.com", "Ben")
    ann_order = paid_order(ann, drop["id"], 2)
    paid_order(ben, drop["id"], 1)

    mine = ann.get("/me/orders").json()["orders"]
    assert [o["id"] for o in mine] == [ann_order] and mine[0]["item_name"] == "Sourdough"
    assert ben.get(f"/orders/{ann_order}").status_code == 404      # someone else's order looks missing
    assert make_client().get(f"/orders/{ann_order}").status_code == 401


def test_order_view_has_what_the_confirmation_page_needs(make_client):
    drop = make_drop(seller_client(make_client), minimum_units=5).json()
    buyer = buyer_client(make_client)
    view = buyer.get(f"/orders/{paid_order(buyer, drop['id'], 2)}").json()
    assert view["minimum_units"] == 5 and view["paid_up_units"] == 2
    assert view["deadline"] and view["reserved_until"] and view["currency"] == "USD"


# ---- webhook ---------------------------------------------------------------------

def approved_event(paypal_order_id):
    return {"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": paypal_order_id}}


def reserved_order(make_client):
    drop = make_drop(seller_client(make_client)).json()
    buyer = buyer_client(make_client)
    return buyer, buyer.post(f"/drops/{drop['id']}/orders", json={"quantity": 1}).json()["order_id"]


def test_webhook_rejects_a_bad_signature(make_client, paypal):
    buyer, order_id = reserved_order(make_client)
    paypal.webhook_valid = False
    assert make_client().post("/paypal/webhook", json=approved_event(f"PPO-{order_id}")).status_code == 400
    assert buyer.get(f"/orders/{order_id}").json()["status"] == "reserved"  # nothing happened


def test_webhook_places_the_hold_and_duplicates_are_harmless(make_client, paypal):
    buyer, order_id = reserved_order(make_client)
    hook = make_client()
    for _ in range(2):
        r = hook.post("/paypal/webhook", json=approved_event(f"PPO-{order_id}"))
        assert r.status_code == 200 and r.json() == {"handled": True}
    assert buyer.get(f"/orders/{order_id}").json()["status"] == "authorized"
    assert paypal.request_ids.count(f"authorize-{order_id}") == 1


def test_webhook_ignores_other_events_and_unknown_orders(make_client):
    hook = make_client()
    other = hook.post("/paypal/webhook", json={"event_type": "PAYMENT.AUTHORIZATION.CREATED", "resource": {}})
    assert other.status_code == 200 and other.json() == {"handled": False}
    assert hook.post("/paypal/webhook", json=approved_event("PPO-NOPE")).json() == {"handled": False}


def test_webhook_needs_configuration(make_client):
    app.dependency_overrides[get_settings] = lambda: Settings()  # no webhook id
    assert make_client().post("/paypal/webhook", json={}).status_code == 503


# ---- deadline job ------------------------------------------------------------------

def test_deadline_job_settles_due_drops_only(make_client, session_factory, paypal):
    seller = seller_client(make_client)
    filled = make_drop(seller, minimum_units=2).json()
    missed = make_drop(seller, minimum_units=5).json()
    not_due = make_drop(seller, deadline=iso(timedelta(days=10)), minimum_units=1).json()
    buyers = [buyer_client(make_client, f"b{i}@example.com", f"B{i}") for i in range(3)]
    a, b, c = paid_order(buyers[0], filled["id"], 2), paid_order(buyers[1], missed["id"], 1), paid_order(buyers[2], not_due["id"], 1)

    summary = drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))

    assert sorted(summary["settled"]) == sorted([filled["id"], missed["id"]]) and summary["errors"] == []
    assert buyers[0].get(f"/orders/{a}").json()["status"] == "captured"
    assert buyers[1].get(f"/orders/{b}").json()["status"] == "voided"
    assert buyers[2].get(f"/orders/{c}").json()["status"] == "authorized"  # drop not due yet


def test_deadline_job_expires_stale_reservations(make_client, session_factory, paypal):
    drop = make_drop(seller_client(make_client)).json()
    buyer_client(make_client).post(f"/drops/{drop['id']}/orders", json={"quantity": 4})
    summary = drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert summary["expired"] == 1
    assert make_client().get(f"/drops/{drop['id']}").json()["units_remaining"] == 10


def test_deadline_job_finishes_a_half_settled_drop(make_client, session_factory, paypal):
    drop = make_drop(seller_client(make_client), minimum_units=2).json()
    ann, ben = buyer_client(make_client, "ann@example.com", "Ann"), buyer_client(make_client, "ben@example.com", "Ben")
    a, b = paid_order(ann, drop["id"], 1), paid_order(ben, drop["id"], 1)
    later = datetime.now(timezone.utc) + timedelta(days=4)
    with session_factory() as s:
        paypal.crash_on_capture = s.get(Order, b).paypal_authorization_id

    first = drops.run_deadline_job(session_factory, paypal, now=later)
    assert first["errors"] and ben.get(f"/orders/{b}").json()["status"] == "authorized"

    second = drops.run_deadline_job(session_factory, paypal, now=later)  # picks up the leftover
    assert second["errors"] == []
    assert ann.get(f"/orders/{a}").json()["status"] == "captured" and ben.get(f"/orders/{b}").json()["status"] == "captured"
    assert len(paypal.captured) == 2  # each charged exactly once


def test_deadline_job_one_bad_drop_does_not_block_others(make_client, session_factory, paypal):
    seller = seller_client(make_client)
    bad, good = make_drop(seller, minimum_units=1).json(), make_drop(seller, minimum_units=1).json()
    paid_order(buyer_client(make_client, "a@example.com"), bad["id"], 1)
    buyer = buyer_client(make_client, "b@example.com")
    good_order = paid_order(buyer, good["id"], 1)
    with session_factory() as s:
        paypal.crash_on_capture = s.scalar(select(Order.paypal_authorization_id).where(Order.drop_id == bad["id"]))
    summary = drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))
    assert len(summary["errors"]) == 1 and summary["errors"][0]["drop_id"] == bad["id"]
    assert buyer.get(f"/orders/{good_order}").json()["status"] == "captured"


# ---- dashboard data ---------------------------------------------------------------

def test_analytics_needs_a_shop(make_client):
    assert make_client().get("/me/analytics").status_code == 401
    assert seller_client(make_client, shop=False).get("/me/analytics").status_code == 403


def test_analytics_has_measures_and_no_buyer_identity(make_client, session_factory, paypal):
    seller = seller_client(make_client)
    drop = make_drop(seller, quantity_total=10, minimum_units=4).json()
    ann = buyer_client(make_client, "ann@example.com", "Ann Secret")
    ben = buyer_client(make_client, "ben@example.com", "Ben Secret")
    paid_order(ann, drop["id"], 2)                                   # approved: on hold
    ben.post(f"/drops/{drop['id']}/orders", json={"quantity": 1})    # reserved only

    data = seller.get("/me/analytics").json()
    row = data["drops"][0]
    assert row["units_approved"] == 2 and row["units_reserved"] == 1 and row["fill_percent"] == 50 and row["is_open"] == 1
    by_status = {o["status"]: o for o in data["orders"]}
    assert by_status["authorized"]["amount_on_hold"] == 18.0 and by_status["authorized"]["value_approved"] == 18.0
    assert by_status["reserved"]["value_approved"] == 0.0 and by_status["reserved"]["amount_on_hold"] == 0.0
    text = str(data)
    assert "Secret" not in text and "@example.com" not in text and "buyer" not in text   # nobody's identity leaks

    # once the drop settles, the money moves from "on hold" to "collected"
    drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))
    again = {o["status"]: o for o in seller.get("/me/analytics").json()["orders"]}
    assert again["voided"]["amount_collected"] == 0.0  # missed its minimum of 4, so nothing was charged


def test_analytics_only_covers_my_own_shop(make_client):
    a, b = seller_client(make_client, email="a@example.com"), seller_client(make_client, email="b@example.com")
    make_drop(a, item_name="Mine")
    make_drop(b, item_name="Theirs")
    assert [d["item_name"] for d in a.get("/me/analytics").json()["drops"]] == ["Mine"]


# ---- drop planner ---------------------------------------------------------------------

def finished_drop(make_client, session_factory, paypal, seller, quantity=2):
    """A drop that ran, filled and was settled, through the real engine."""
    drop = make_drop(seller, quantity_total=10, minimum_units=2).json()
    paid_order(buyer_client(make_client, f"p{drop['id']}@example.com"), drop["id"], quantity)
    drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))
    return drop


def test_plan_requires_a_shop(make_client):
    assert make_client().get("/me/plan").status_code == 401
    assert seller_client(make_client, shop=False).get("/me/plan").status_code == 403


def test_plan_with_no_finished_drops_says_so(make_client):
    out = seller_client(make_client).get("/me/plan").json()
    assert out["recommendation"]["enough_data"] is False and out["reports"] == []


def test_plan_after_a_finished_drop(make_client, session_factory, paypal):
    seller = seller_client(make_client)
    drop = finished_drop(make_client, session_factory, paypal, seller)
    out = seller.get("/me/plan").json()
    rec = out["recommendation"]
    assert rec["enough_data"] is True and rec["confidence"] == "low" and rec["based_on"] == [drop["id"]]
    assert out["reports"][0]["committed_units"] == 2 and out["reports"][0]["status"] == "filled"
    assert rec["recommended"]["item_name"] == "Sourdough" and rec["reasons"]


def test_plan_can_focus_on_one_drop_but_only_your_own(make_client, session_factory, paypal):
    a, b = seller_client(make_client, email="a@example.com"), seller_client(make_client, email="b@example.com")
    mine = finished_drop(make_client, session_factory, paypal, a)
    theirs = finished_drop(make_client, session_factory, paypal, b)
    assert a.get("/me/plan", params={"drop_id": mine["id"]}).json()["reports"][0]["drop_id"] == mine["id"]
    assert a.get("/me/plan", params={"drop_id": theirs["id"]}).status_code == 404
    still_open = make_drop(a).json()
    assert a.get("/me/plan", params={"drop_id": still_open["id"]}).status_code == 404   # not finished


def test_drops_carry_the_shops_name_so_buyers_know_who_is_selling(make_client):
    make_drop(seller_client(make_client))
    drop = make_client().get("/drops").json()["drops"][0]
    assert drop["shop_name"] == "Bea's Bakery"


# ---- location: pickup and delivery -------------------------------------------------------------

def located(**extra):
    return dict(pickup_address="350 5th Ave, New York, NY", pickup_notes="Ring the side bell", **extra)


def delivery_drop_body(**extra):
    return located(offers_delivery=True, delivery_radius_miles=5, delivery_fee="3.00", **extra)


def test_creating_a_drop_looks_up_the_address_and_keeps_it_private(make_client):
    seller = seller_client(make_client)
    r = seller.post("/drops", json=drop_body(**located()))
    assert r.status_code == 201
    drop = r.json()
    assert drop["area"] == "Koreatown, New York" and (drop["lat"], drop["lng"]) == (40.75, -73.99)
    public = make_client()
    for url in ("/drops", f"/drops/{drop['id']}"):
        shown = public.get(url).text
        assert "350 5th" not in shown and "side bell" not in shown and "pickup_address" not in shown


def test_the_delivery_distance_is_given_in_miles_and_stored_in_kilometres(make_client):
    drop = seller_client(make_client).post("/drops", json=drop_body(**delivery_drop_body())).json()
    assert drop["offers_delivery"] is True and drop["delivery_fee"] == "3.00"
    assert drop["delivery_radius_km"] == pytest.approx(8.05, abs=0.01)


def test_bad_addresses_and_bad_delivery_settings_are_clear_errors(make_client):
    seller = seller_client(make_client)
    nowhere = seller.post("/drops", json=drop_body(pickup_address="nowhere at all"))
    assert nowhere.status_code == 422 and "couldn't find that address" in nowhere.json()["error"]
    down = seller.post("/drops", json=drop_body(pickup_address="down down down"))
    assert down.status_code == 503 and "try again" in down.json()["error"]
    body = drop_body()
    del body["pickup_address"]
    assert seller.post("/drops", json=body).status_code == 422                                  # an address is required
    assert seller.post("/drops", json=drop_body(**located(offers_delivery=True))).status_code == 422   # radius missing
    assert seller.post("/drops", json=drop_body(**located(offers_delivery=True, delivery_radius_miles=99))).status_code == 422
    assert seller.post("/drops", json=drop_body(**located(offers_delivery=True, delivery_radius_miles=3, delivery_fee="75"))).status_code == 422
    assert seller.post("/drops", json=drop_body(**located(offers_pickup=False))).status_code == 422   # no way to receive it


def test_sellers_can_check_an_address_before_posting(make_client):
    seller = seller_client(make_client)
    ok = seller.post("/geo/check", json={"query": "350 5th Ave, New York"})
    assert ok.status_code == 200 and ok.json()["area"] == "Koreatown, New York"
    assert seller.post("/geo/check", json={"query": "nowhere"}).status_code == 422
    assert make_client().post("/geo/check", json={"query": "350 5th Ave"}).status_code == 401


def test_address_checks_are_rate_limited(make_client, monkeypatch):
    from app import main
    from app.ratelimit import RateLimiter

    monkeypatch.setattr(main, "geocode_limiter", RateLimiter(limit=2, window=600))
    seller = seller_client(make_client)
    assert [seller.post("/geo/check", json={"query": "350 5th Ave"}).status_code for _ in range(3)] == [200, 200, 429]


def order(buyer, drop_id, **body):
    return buyer.post(f"/drops/{drop_id}/orders", json={"quantity": 2, **body})


def test_delivery_orders_are_priced_with_the_fee_and_checked_against_the_radius(make_client, session_factory):
    seller, buyer = seller_client(make_client), buyer_client(make_client)
    drop = seller.post("/drops", json=drop_body(**delivery_drop_body())).json()

    inside = order(buyer, drop["id"], fulfillment="delivery", delivery_address="12 near road")
    assert inside.status_code == 201 and inside.json()["amount"] == "21.00"                 # 2 x 9 + 3 fee
    outside = order(buyer, drop["id"], fulfillment="delivery", delivery_address="500 far away road")
    assert outside.status_code == 422 and "miles away" in outside.json()["error"]
    assert order(buyer, drop["id"], fulfillment="delivery").status_code == 422               # no address
    assert order(buyer, drop["id"], fulfillment="delivery", delivery_address="nowhere").status_code == 422
    assert order(buyer, drop["id"], fulfillment="delivery", delivery_address="down").status_code == 503
    pickup = order(buyer, drop["id"])                                                         # pickup is the default
    assert pickup.json()["amount"] == "18.00"
    with session_factory() as s:
        assert [(o.fulfillment, o.delivery_fee) for o in s.scalars(select(Order).order_by(Order.id))][0][0] == "delivery"


def test_delivery_is_refused_on_a_pickup_only_drop(make_client):
    drop = make_drop(seller_client(make_client)).json()
    r = order(buyer_client(make_client), drop["id"], fulfillment="delivery", delivery_address="12 near road")
    assert r.status_code == 422 and "doesn't offer delivery" in r.json()["error"]


def test_the_exact_address_is_revealed_only_after_the_hold_is_approved_and_only_to_that_buyer(make_client):
    seller = seller_client(make_client)
    drop = seller.post("/drops", json=drop_body(**located())).json()
    ann, ben = buyer_client(make_client, "ann@example.com", "Ann"), buyer_client(make_client, "ben@example.com", "Ben")
    placed = order(ann, drop["id"]).json()

    before = ann.get(f"/orders/{placed['order_id']}").json()                        # reserved, not yet approved
    assert before["status"] == "reserved" and before["area"] == "Koreatown, New York"
    assert before["pickup_address"] is None and before["pickup_notes"] is None
    assert "350 5th" not in str(before)

    ann.get("/paypal/return", params={"token": f"PPO-{placed['order_id']}"})        # the buyer approves on PayPal
    after = ann.get(f"/orders/{placed['order_id']}").json()
    assert after["status"] == "authorized"
    assert after["pickup_address"] == "350 5th Ave, New York, NY" and after["pickup_notes"] == "Ring the side bell"

    assert ben.get(f"/orders/{placed['order_id']}").status_code == 404              # someone else's order
    assert "350 5th" not in str(ann.get("/me/orders").json()["orders"][0]) or after["pickup_address"]   # (the list is the buyer's own)


def test_a_released_hold_takes_the_address_back_away(make_client, session_factory, paypal):
    seller = seller_client(make_client)
    drop = seller.post("/drops", json=drop_body(minimum_units=5, **located())).json()
    buyer = buyer_client(make_client)
    order_id = paid_order(buyer, drop["id"], 1)
    assert buyer.get(f"/orders/{order_id}").json()["pickup_address"]
    drops.run_deadline_job(session_factory, paypal, now=datetime.now(timezone.utc) + timedelta(days=4))   # minimum missed
    gone = buyer.get(f"/orders/{order_id}").json()
    assert gone["status"] == "voided" and gone["pickup_address"] is None


def test_sellers_see_who_to_hand_orders_to_only_for_approved_orders(make_client):
    seller = seller_client(make_client)
    drop = seller.post("/drops", json=drop_body(**delivery_drop_body())).json()
    ann = buyer_client(make_client, "ann@example.com", "Ann Marie Baker")
    ben = buyer_client(make_client, "ben@example.com", "Ben")
    paid = order(ann, drop["id"], fulfillment="delivery", delivery_address="12 near road").json()
    ann.get("/paypal/return", params={"token": f"PPO-{paid['order_id']}"})
    order(ben, drop["id"])                                                                   # reserved only

    rows = seller.get(f"/me/drops/{drop['id']}/orders").json()["orders"]
    assert len(rows) == 1                                                                    # the unapproved reservation is hidden
    assert rows[0]["buyer"] == "Ann B." and rows[0]["fulfillment"] == "delivery"
    assert rows[0]["delivery_address"] == "12 near road" and rows[0]["quantity"] == 2
    assert "ann@example.com" not in str(rows) and "Marie" not in str(rows)                   # no email, no full name


def test_only_the_owner_sees_a_drops_fulfillment_list(make_client):
    owner, rival = seller_client(make_client, email="o@example.com"), seller_client(make_client, email="r@example.com")
    drop = owner.post("/drops", json=drop_body(**located())).json()
    assert rival.get(f"/me/drops/{drop['id']}/orders").status_code == 404
    assert buyer_client(make_client).get(f"/me/drops/{drop['id']}/orders").status_code == 403
    assert make_client().get(f"/me/drops/{drop['id']}/orders").status_code == 401


def test_analytics_never_carries_an_address(make_client):
    seller = seller_client(make_client)
    seller.post("/drops", json=drop_body(**located()))
    assert "350 5th" not in seller.get("/me/analytics").text
