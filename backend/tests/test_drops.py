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
