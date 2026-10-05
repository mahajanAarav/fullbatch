"""The drop planner: reports on finished drops, and rules for what to run next."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app import planner
from app.models import Drop, DropStatus, Order, OrderStatus

TZ = "America/New_York"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)  # a Monday


def make_settled(session, seller, *, total=20, minimum=10, price="9.00", status=DropStatus.FILLED,
                 orders=(), window_hours=72, days_ago=5, item="Sourdough"):
    """A finished drop. `orders` is [(hours after the drop opened, quantity, OrderStatus)]."""
    created = NOW - timedelta(days=days_ago) - timedelta(hours=window_hours)
    drop = Drop(seller_id=seller.id, item_name=item, unit_price=Decimal(price), quantity_total=total,
                minimum_units=minimum, max_per_buyer=4, deadline=created + timedelta(hours=window_hours),
                status=status, created_at=created, settled_at=created + timedelta(hours=window_hours))
    session.add(drop)
    session.flush()
    for hours, qty, st in orders:
        placed = created + timedelta(hours=hours)
        session.add(Order(drop_id=drop.id, buyer_name="B", buyer_email="b@example.com", chat_session_id="c",
                          quantity=qty, amount=Decimal(price) * qty, status=st,
                          reserved_until=placed + timedelta(minutes=15), created_at=placed))
    session.commit()
    return drop


C, V = OrderStatus.CAPTURED, OrderStatus.VOIDED


def sold_out_fast(session, seller, **kw):
    # 20 units, minimum 10, all captured, minimum reached 10h into a 72h window
    return make_settled(session, seller, orders=[(5, 4, C), (10, 6, C), (30, 5, C), (60, 5, C)], **kw)


# ---- the report -------------------------------------------------------------------

def test_report_counts_only_real_demand(session, seller):
    drop = make_settled(session, seller, orders=[
        (5, 4, C), (10, 6, C),
        (20, 3, OrderStatus.EXPIRED),    # never had a hold: not demand
        (22, 2, OrderStatus.RESERVED),   # same
    ])
    r = planner.drop_report(session, drop, TZ)
    assert r["committed_units"] == 10 and r["committed_orders"] == 2 and r["average_order_size"] == 5.0
    assert r["sell_through"] == 0.5 and r["minimum_ratio"] == 1.0
    assert r["hours_to_minimum"] == 10.0 and r["revenue_collected"] == 90.0


def test_report_on_a_missed_drop_counts_voided_holds_as_demand(session, seller):
    drop = make_settled(session, seller, status=DropStatus.FAILED, orders=[(5, 3, V), (40, 2, V)])
    r = planner.drop_report(session, drop, TZ)
    assert r["committed_units"] == 5 and r["minimum_ratio"] == 0.5
    assert r["hours_to_minimum"] is None and r["revenue_collected"] == 0 and r["revenue_not_collected"] == 45.0


def test_report_notices_a_last_day_rush(session, seller):
    drop = make_settled(session, seller, orders=[(5, 2, C), (60, 8, C)])  # 8 of 10 units in the final 24h
    assert planner.drop_report(session, drop, TZ)["share_in_last_day"] == 0.8


def test_report_finds_the_busiest_local_weekday_and_hour(session, seller):
    drop = make_settled(session, seller, orders=[(3, 5, C), (4, 5, C)])
    r = planner.drop_report(session, drop, TZ)
    local = (drop.created_at + timedelta(hours=3)).astimezone(ZoneInfo(TZ))
    assert r["busiest_weekday"] == local.strftime("%A") and r["busiest_hour"] == local.hour


# ---- recommendations ---------------------------------------------------------------

def rec_for(session, seller, *drops):
    reports = planner.settled_reports(session, seller.id, TZ)
    return planner.recommend(reports, TZ, NOW)


def test_with_no_history_it_says_so_and_offers_a_starter(session, seller):
    out = planner.recommend([], TZ, NOW)
    assert out["enough_data"] is False and out["confidence"] == "none"
    assert out["recommended"]["quantity_total"] == 12 and out["recommended"]["minimum_units"] == 6


def test_a_fast_sell_out_raises_quantity_and_nudges_the_price(session, seller):
    sold_out_fast(session, seller)
    out = rec_for(session, seller)
    r = out["recommended"]
    assert r["quantity_total"] == 25 and r["minimum_units"] == 12          # 20 x 1.25, minimum scaled
    assert r["unit_price"] == 9.75                                         # 9.00 x 1.07 = 9.63, to the nearest quarter
    assert any("sold 20 of 20" in x for x in out["reasons"]) and any("price" in x.lower() for x in out["reasons"])


def test_a_slow_sell_out_does_not_touch_the_price(session, seller):
    make_settled(session, seller, orders=[(50, 10, C), (60, 10, C)])  # sold out, but late: minimum only reached at 50h of 72
    r = rec_for(session, seller)["recommended"]
    assert r["quantity_total"] == 25 and r["unit_price"] == 9.0


def test_a_comfortable_fill_keeps_the_size(session, seller):
    make_settled(session, seller, orders=[(5, 6, C), (30, 6, C)])  # 12 of 20: 60%
    out = rec_for(session, seller)
    assert out["recommended"]["quantity_total"] == 20 and any("about right" in x for x in out["reasons"])


def test_leftovers_shrink_the_batch_but_never_below_the_minimum(session, seller):
    make_settled(session, seller, total=40, minimum=10, orders=[(5, 5, C), (10, 5, C)])  # 10 of 40: 25%
    r = rec_for(session, seller)["recommended"]
    assert r["quantity_total"] == 12 and r["minimum_units"] <= r["quantity_total"]  # ceil(10 x 1.2)


def test_a_miss_with_real_interest_lowers_the_minimum_and_runs_longer(session, seller):
    make_settled(session, seller, status=DropStatus.FAILED, minimum=10, orders=[(5, 4, V), (30, 3, V)])  # 7 of 10
    out = rec_for(session, seller)
    r = out["recommended"]
    assert r["minimum_units"] == 8 and r["unit_price"] == 9.0               # ceil(7 x 1.1), price untouched
    assert r["duration_days"] == 5                                          # 3 days x 1.5, rounded
    assert any("real interest" in x for x in out["reasons"]) and any("longer" in x for x in out["reasons"])


def test_a_miss_with_little_interest_suggests_a_small_test(session, seller):
    make_settled(session, seller, status=DropStatus.FAILED, minimum=10, orders=[(5, 2, V)])  # 2 of 10
    out = rec_for(session, seller)
    assert out["recommended"]["minimum_units"] == 3 and any("promote" in x for x in out["reasons"])


def test_a_drop_nobody_ordered_still_gives_a_sane_answer(session, seller):
    make_settled(session, seller, status=DropStatus.FAILED, orders=[])
    r = rec_for(session, seller)["recommended"]
    assert r["minimum_units"] >= 2 and r["quantity_total"] >= r["minimum_units"]


def test_confidence_grows_with_history_and_is_honest_about_it(session, seller):
    sold_out_fast(session, seller, days_ago=30)
    assert rec_for(session, seller)["confidence"] == "low" and "single drop" in rec_for(session, seller)["caveat"]
    sold_out_fast(session, seller, days_ago=20)
    assert rec_for(session, seller)["confidence"] == "medium"
    sold_out_fast(session, seller, days_ago=10)
    out = rec_for(session, seller)
    assert out["confidence"] == "high" and out["caveat"] is None and len(out["based_on"]) == 3


def test_the_newest_finished_drop_drives_the_advice(session, seller):
    sold_out_fast(session, seller, days_ago=30, item="Old hit")
    make_settled(session, seller, status=DropStatus.FAILED, minimum=10, orders=[(5, 2, V)], days_ago=2, item="Recent flop")
    assert rec_for(session, seller)["recommended"]["item_name"] == "Recent flop"


def test_halves_round_up_not_to_even():
    assert [planner._round_half_up(x) for x in (4.5, 5.5, 2.4, 2.6)] == [5, 6, 2, 3]


def test_prices_move_in_quarter_dollar_steps():
    assert planner._round_price(9.63) == Decimal("9.75") and planner._round_price(9.1) == Decimal("9.00")


def test_the_suggested_close_is_a_future_evening_in_local_time(session, seller):
    sold_out_fast(session, seller)
    deadline = datetime.fromisoformat(rec_for(session, seller)["recommended"]["deadline"])
    assert deadline > NOW + timedelta(hours=24)
    assert deadline.astimezone(ZoneInfo(TZ)).hour == 18
    assert deadline <= NOW + timedelta(days=planner.STARTER["duration_days"] + 8)


def test_only_finished_drops_of_this_seller_count(session, seller):
    sold_out_fast(session, seller)
    make_settled(session, seller, status=DropStatus.OPEN, orders=[(1, 2, C)], item="Still running")
    reports = planner.settled_reports(session, seller.id, TZ)
    assert [r["item_name"] for r in reports] == ["Sourdough"]
    assert planner.settled_reports(session, seller.id + 99, TZ) == []
