from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Thread

import pytest
from sqlalchemy import select

from app import drops
from app.models import DropEvent, Order, OrderStatus


def reserve(session, drop, quantity=1, name="Buyer", **kwargs):
    return drops.reserve_stock(
        session, drop.id, name, f"{name.lower()}@example.com", f"chat-{name}", quantity, **kwargs
    )


# ---- create_drop ----------------------------------------------------------

def test_create_drop_stores_values(make_drop):
    drop = make_drop()
    assert drop.status.value == "open"
    assert drop.unit_price == Decimal("9.00")
    assert drop.max_per_buyer == 4


@pytest.mark.parametrize(
    "overrides",
    [
        {"item_name": "  "},
        {"unit_price": Decimal("0")},
        {"quantity_total": 0},
        {"minimum_units": 0},
        {"minimum_units": 11},  # more than the 10 available
        {"deadline": datetime.now(timezone.utc) - timedelta(hours=1)},
        {"deadline": datetime.now(timezone.utc) + timedelta(days=drops.MAX_DROP_DAYS + 1)},
    ],
)
def test_create_drop_rejects_bad_input(make_drop, overrides):
    with pytest.raises(drops.InvalidDrop):
        make_drop(**overrides)


# ---- reserve_stock --------------------------------------------------------

def test_reserve_creates_reserved_order_with_expiry(session, make_drop):
    drop = make_drop()
    order = reserve(session, drop, quantity=2)
    assert order.status == OrderStatus.RESERVED
    assert order.amount == Decimal("18.00")
    assert order.reserved_until > datetime.now(timezone.utc)
    assert drops.units_taken(session, drop.id) == 2


def test_cannot_reserve_more_than_remaining(session, make_drop):
    drop = make_drop(quantity_total=5, minimum_units=1)
    reserve(session, drop, quantity=4, name="A")
    with pytest.raises(drops.SoldOut) as err:
        reserve(session, drop, quantity=2, name="B")
    assert err.value.remaining == 1
    assert drops.units_taken(session, drop.id) == 4  # the failed attempt changed nothing


def test_sold_out_message(session, make_drop):
    drop = make_drop(quantity_total=2, minimum_units=1)
    reserve(session, drop, quantity=2)
    with pytest.raises(drops.SoldOut, match="sold out"):
        reserve(session, drop, quantity=1, name="Late")


def test_per_buyer_limit(session, make_drop):
    drop = make_drop()
    with pytest.raises(drops.InvalidOrder):
        reserve(session, drop, quantity=5)  # limit is 4


def test_zero_quantity_rejected(session, make_drop):
    drop = make_drop()
    with pytest.raises(drops.InvalidOrder):
        reserve(session, drop, quantity=0)


def test_cannot_order_after_deadline(session, make_drop):
    drop = make_drop()
    later = drop.deadline + timedelta(minutes=1)
    with pytest.raises(drops.DropNotOpen):
        reserve(session, drop, now=later)


def test_unknown_drop(session):
    with pytest.raises(drops.DropNotFound):
        drops.reserve_stock(session, 999, "A", "a@example.com", "chat-A", 1)


# ---- expire_stale_reservations -------------------------------------------

def test_expiry_frees_stock(session, make_drop):
    drop = make_drop(quantity_total=3, minimum_units=1)
    order = reserve(session, drop, quantity=3)
    assert drops.units_taken(session, drop.id) == 3

    # Not expired yet: nothing changes.
    assert drops.expire_stale_reservations(session) == 0

    # Jump past the reservation window.
    later = order.reserved_until + timedelta(seconds=1)
    assert drops.expire_stale_reservations(session, now=later) == 1

    session.refresh(order)
    assert order.status == OrderStatus.EXPIRED
    assert drops.units_taken(session, drop.id) == 0
    reserve(session, drop, quantity=3, name="Next")  # the stock is available again


def test_expiry_leaves_authorized_orders_alone(session, make_drop):
    drop = make_drop()
    order = reserve(session, drop)
    order.status = OrderStatus.AUTHORIZED
    session.commit()
    later = order.reserved_until + timedelta(hours=1)
    assert drops.expire_stale_reservations(session, now=later) == 0


def test_events_are_logged(session, make_drop):
    drop = make_drop()
    reserve(session, drop)
    kinds = session.scalars(select(DropEvent.kind).order_by(DropEvent.id)).all()
    assert kinds == ["drop_created", "order_reserved"]


# ---- the race -------------------------------------------------------------

def test_concurrent_buyers_never_oversell(session_factory, make_drop):
    """
    20 buyers all try to take 1 unit of a 5-unit drop at the same moment.
    Exactly 5 must succeed and 15 must be told it is sold out.
    """
    drop = make_drop(quantity_total=5, minimum_units=1)
    buyers = 20
    barrier = Barrier(buyers)  # makes every thread start at the same instant
    results = []

    def buyer(n: int):
        with session_factory() as s:
            barrier.wait()
            try:
                drops.reserve_stock(s, drop.id, f"B{n}", f"b{n}@example.com", f"chat-{n}", 1)
                results.append("ok")
            except drops.SoldOut:
                results.append("sold_out")

    threads = [Thread(target=buyer, args=(n,)) for n in range(buyers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 5
    assert results.count("sold_out") == 15
    with session_factory() as s:
        assert drops.units_taken(s, drop.id) == 5
        assert len(s.scalars(select(Order)).all()) == 5


# ---- seller verification ---------------------------------------------------

def test_an_unverified_seller_cannot_open_a_drop(session, seller_user, seller, make_drop):
    seller_user.paypal_verified = False
    session.commit()
    with pytest.raises(drops.SellerNotVerified, match="isn't verified"):
        make_drop()


def test_a_shop_with_no_owner_is_not_verified(session, make_drop):
    from app.models import Seller
    orphan = Seller(name="Legacy shop")  # created before accounts existed
    session.add(orphan)
    session.commit()
    with pytest.raises(drops.SellerNotVerified):
        make_drop(seller_id=orphan.id)


def test_unknown_seller_is_refused(make_drop):
    with pytest.raises(drops.InvalidDrop, match="Unknown seller"):
        make_drop(seller_id=9999)


# ---- location, pickup and delivery ---------------------------------------------------------------

def test_every_drop_needs_an_address_and_a_way_to_receive_it(session, seller, make_drop):
    with pytest.raises(drops.InvalidDrop, match="address"):
        make_drop(pickup_address=None, pickup_lat=None, pickup_lng=None)
    with pytest.raises(drops.InvalidDrop, match="address"):
        make_drop(pickup_address="   ")
    with pytest.raises(drops.InvalidDrop, match="pickup, delivery, or both"):
        make_drop(offers_pickup=False, offers_delivery=False)
    with pytest.raises(drops.InvalidDrop, match="300 characters"):
        make_drop(pickup_notes="x" * 301)


def test_delivery_needs_a_sensible_radius_and_fee(session, seller, make_drop):
    for radius in (None, 0, -1, 51):
        with pytest.raises(drops.InvalidDrop, match="delivery distance"):
            make_drop(offers_delivery=True, delivery_radius_km=radius)
    for fee in (Decimal("-1"), Decimal("50.01")):
        with pytest.raises(drops.InvalidDrop, match="delivery fee"):
            make_drop(offers_delivery=True, delivery_radius_km=5, delivery_fee=fee)
    ok = make_drop(offers_delivery=True, delivery_radius_km=5, delivery_fee=Decimal("4.50"))
    assert ok.offers_delivery and ok.delivery_radius_km == 5 and ok.delivery_fee == Decimal("4.50")


def test_a_pickup_only_drop_ignores_stray_delivery_settings(session, seller, make_drop):
    d = make_drop(offers_delivery=False, delivery_radius_km=9, delivery_fee=Decimal("5"))
    assert d.delivery_radius_km is None and d.delivery_fee == Decimal("0")


def test_a_delivery_only_drop_is_allowed_and_refuses_pickup_orders(session, seller, make_drop):
    d = make_drop(offers_pickup=False, offers_delivery=True, delivery_radius_km=5)
    with pytest.raises(drops.InvalidOrder, match="delivery only"):
        drops.reserve_stock(session, d.id, "A", "a@x.co", "c", 1)


def order_delivery(session, drop, lat, lng, address="10 Some St", qty=2):
    return drops.reserve_stock(session, drop.id, "A", "a@x.co", "c", qty, fulfillment="delivery",
                               delivery_address=address, delivery_lat=lat, delivery_lng=lng)


def test_delivery_inside_the_radius_adds_the_fee_to_what_paypal_holds(session, seller, make_drop):
    d = make_drop(offers_delivery=True, delivery_radius_km=5, delivery_fee=Decimal("3.00"))
    o = order_delivery(session, d, 40.7580, -73.9855)            # about 1 km away
    assert o.fulfillment == "delivery" and o.delivery_fee == Decimal("3.00") and o.amount == Decimal("21.00")
    assert o.delivery_address == "10 Some St"
    pickup = drops.reserve_stock(session, d.id, "B", "b@x.co", "c2", 2)
    assert pickup.fulfillment == "pickup" and pickup.amount == Decimal("18.00") and pickup.delivery_address is None


def test_delivery_outside_the_radius_is_refused_with_a_clear_distance(session, seller, make_drop):
    d = make_drop(offers_delivery=True, delivery_radius_km=5)
    with pytest.raises(drops.InvalidOrder, match=r"miles away.*within 3\.1 miles"):
        order_delivery(session, d, 41.15, -73.9857)               # about 45 km away
    assert drops.units_taken(session, d.id) == 0                  # nothing was reserved


def test_the_radius_edge_is_inclusive_and_just_past_it_is_not(session, seller, make_drop):
    from app.geo import haversine_km

    d = make_drop(offers_delivery=True, delivery_radius_km=2.0)
    # find a point just inside and just outside 2 km due north of the shop
    deg = 2.0 / 111.19
    inside = order_delivery(session, d, d.pickup_lat + deg * 0.98, d.pickup_lng)
    assert haversine_km(d.pickup_lat, d.pickup_lng, inside.delivery_lat, inside.delivery_lng) <= 2.0
    with pytest.raises(drops.InvalidOrder):
        order_delivery(session, d, d.pickup_lat + deg * 1.05, d.pickup_lng)


def test_delivery_is_refused_when_not_offered_or_without_an_address(session, seller, make_drop):
    pickup_only = make_drop()
    with pytest.raises(drops.InvalidOrder, match="doesn't offer delivery"):
        order_delivery(session, pickup_only, 40.7484, -73.9857)
    d = make_drop(offers_delivery=True, delivery_radius_km=5)
    for addr, lat, lng in ((None, 40.75, -73.98), ("  ", 40.75, -73.98), ("10 St", None, None)):
        with pytest.raises(drops.InvalidOrder, match="address to deliver to"):
            drops.reserve_stock(session, d.id, "A", "a@x.co", "c", 1, fulfillment="delivery",
                                delivery_address=addr, delivery_lat=lat, delivery_lng=lng)
    with pytest.raises(drops.InvalidOrder, match="pickup or delivery"):
        drops.reserve_stock(session, d.id, "A", "a@x.co", "c", 1, fulfillment="teleport")


def test_public_drop_data_has_the_area_but_never_the_exact_address_or_notes(session, seller, make_drop):
    d = make_drop(pickup_notes="Ring the side bell", offers_delivery=True, delivery_radius_km=5, delivery_fee=Decimal("2"))
    shown = drops.drop_summary(session, d)
    assert shown["area"] == "Koreatown, New York" and shown["offers_pickup"] and shown["offers_delivery"]
    assert (shown["lat"], shown["lng"]) == (40.75, -73.99)          # rounded to about a kilometre
    assert "350 5th" not in str(shown) and "side bell" not in str(shown) and "pickup_address" not in shown


def test_the_hold_confirmation_tells_the_buyer_where_to_collect_or_where_it_is_going(session, seller, make_drop):
    from tests.test_settlement import notes_for
    from tests.fakes import FakePayPal

    pp = FakePayPal()
    d = make_drop(pickup_notes="Ring the side bell", offers_delivery=True, delivery_radius_km=5)
    pick = drops.reserve_stock(session, d.id, "A", "a@x.co", "chat-pick", 1)
    deliv = order_delivery(session, d, 40.7580, -73.9855)
    for o in (pick, deliv):
        drops.start_checkout(session, pp, o.id, "r", "c")
        drops.confirm_authorization(session, pp, f"PPO-{o.id}")
    assert "Pickup address: 350 5th Ave, New York, NY. Ring the side bell" in notes_for(session, "chat-pick")[0]
    assert "delivered to 10 Some St" in notes_for(session, "c")[0]
