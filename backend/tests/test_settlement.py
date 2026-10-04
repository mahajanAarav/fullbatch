"""Checkout, authorization, settlement and cancel, using a fake PayPal."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import drops
from app.models import Drop, DropEvent, DropStatus, Order, OrderStatus
from tests.fakes import FakePayPal

URLS = ("https://app/return", "https://app/cancel")


@pytest.fixture
def paypal():
    return FakePayPal()


def reserve(session, drop, quantity, name):
    return drops.reserve_stock(
        session, drop.id, name, f"{name.lower()}@example.com", f"chat-{name}", quantity
    )


def authorized(session, paypal, drop, quantity, name):
    """Reserve, start checkout, and have the 'buyer approve': an AUTHORIZED order."""
    order = reserve(session, drop, quantity, name)
    drops.start_checkout(session, paypal, order.id, *URLS)
    return drops.confirm_authorization(session, paypal, f"PPO-{order.id}")


def statuses(session, drop):
    return {o.buyer_name: o.status for o in session.scalars(select(Order).where(Order.drop_id == drop.id))}


def after_deadline(drop):
    return drop.deadline + timedelta(seconds=1)


# ---- start_checkout --------------------------------------------------------

def test_start_checkout_returns_link_and_stores_paypal_order(session, make_drop, paypal):
    drop = make_drop()
    order = reserve(session, drop, 2, "Ann")
    link = drops.start_checkout(session, paypal, order.id, *URLS)
    assert link == f"https://fake.paypal/approve/{order.id}"
    assert order.paypal_order_id == f"PPO-{order.id}"
    assert paypal.request_ids == [f"create-order-{order.id}"]  # stable id, safe to retry


def test_start_checkout_rejects_expired_reservation(session, make_drop, paypal):
    drop = make_drop()
    order = reserve(session, drop, 1, "Ann")
    with pytest.raises(drops.OrderNotPayable):
        drops.start_checkout(session, paypal, order.id, *URLS, now=order.reserved_until + timedelta(seconds=1))


# ---- confirm_authorization -------------------------------------------------

def test_confirm_places_hold(session, make_drop, paypal):
    drop = make_drop()
    order = authorized(session, paypal, drop, 2, "Ann")
    assert order.status == OrderStatus.AUTHORIZED
    assert order.paypal_authorization_id == f"AUTH-PPO-{order.id}"
    assert order.authorization_expires_at is not None


def test_confirm_twice_is_harmless(session, make_drop, paypal):
    drop = make_drop()
    order = authorized(session, paypal, drop, 1, "Ann")
    again = drops.confirm_authorization(session, paypal, order.paypal_order_id)
    assert again.status == OrderStatus.AUTHORIZED
    assert paypal.request_ids.count(f"authorize-{order.id}") == 1  # no second PayPal call


def test_confirm_rejects_expired_order_without_calling_paypal(session, make_drop, paypal):
    drop = make_drop()
    order = reserve(session, drop, 1, "Ann")
    drops.start_checkout(session, paypal, order.id, *URLS)
    drops.expire_stale_reservations(session, now=order.reserved_until + timedelta(seconds=1))
    with pytest.raises(drops.OrderNotPayable):
        drops.confirm_authorization(session, paypal, order.paypal_order_id)
    assert f"authorize-{order.id}" not in paypal.request_ids


def test_hold_made_during_a_race_is_released_straight_away(session, session_factory, make_drop, paypal):
    """The drop is cancelled while PayPal is placing the hold: that hold must not linger."""
    drop = make_drop()
    order = reserve(session, drop, 1, "Ann")
    drops.start_checkout(session, paypal, order.id, *URLS)

    def cancel_in_another_session():
        with session_factory() as other:
            other.get(Drop, drop.id).status = DropStatus.CANCELLED
            other.commit()

    paypal.on_authorize = cancel_in_another_session
    with pytest.raises(drops.OrderNotPayable):
        drops.confirm_authorization(session, paypal, order.paypal_order_id)
    assert paypal.voided == [f"AUTH-PPO-{order.id}"]
    session.refresh(order)
    assert order.status == OrderStatus.RESERVED  # never became authorized


# ---- settle_drop -----------------------------------------------------------

def test_settle_before_deadline_is_refused(session, make_drop, paypal):
    drop = make_drop()
    with pytest.raises(drops.DropNotDue):
        drops.settle_drop(session, paypal, drop.id)


def test_minimum_met_captures_every_hold(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=5)
    authorized(session, paypal, drop, 2, "Ann")
    authorized(session, paypal, drop, 2, "Ben")
    authorized(session, paypal, drop, 2, "Cat")  # 6 units >= 5

    result = drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))

    assert result == DropStatus.FILLED
    assert set(statuses(session, drop).values()) == {OrderStatus.CAPTURED}
    assert len(paypal.captured) == 3 and paypal.voided == []
    session.refresh(drop)
    assert drop.status == DropStatus.FILLED and drop.settled_at is not None


def test_minimum_missed_voids_every_hold(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=5)
    authorized(session, paypal, drop, 2, "Ann")
    authorized(session, paypal, drop, 1, "Ben")  # 3 units < 5

    result = drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))

    assert result == DropStatus.FAILED
    assert set(statuses(session, drop).values()) == {OrderStatus.VOIDED}
    assert paypal.captured == [] and len(paypal.voided) == 2


def test_unpaid_reservations_are_expired_and_never_counted(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=3)
    authorized(session, paypal, drop, 2, "Ann")
    reserve(session, drop, 4, "Ben")  # reserved, never approved

    result = drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))

    assert result == DropStatus.FAILED  # only Ann's 2 units count
    assert statuses(session, drop) == {"Ann": OrderStatus.VOIDED, "Ben": OrderStatus.EXPIRED}


def test_one_failed_capture_does_not_stop_the_others(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=2)
    ann = authorized(session, paypal, drop, 1, "Ann")
    authorized(session, paypal, drop, 1, "Ben")
    paypal.fail_capture.add(ann.paypal_authorization_id)

    result = drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))

    assert result == DropStatus.FILLED
    assert statuses(session, drop) == {"Ann": OrderStatus.FAILED, "Ben": OrderStatus.CAPTURED}
    kinds = session.scalars(select(DropEvent.kind)).all()
    assert "order_failed" in kinds


def test_settling_twice_does_not_charge_twice(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=1)
    authorized(session, paypal, drop, 1, "Ann")
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    assert len(paypal.captured) == 1


def test_rerun_after_a_crash_finishes_without_recapturing(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=2)
    ann = authorized(session, paypal, drop, 1, "Ann")
    ben = authorized(session, paypal, drop, 1, "Ben")
    paypal.crash_on_capture = ben.paypal_authorization_id  # dies on the second capture

    with pytest.raises(RuntimeError):
        drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    assert statuses(session, drop) == {"Ann": OrderStatus.CAPTURED, "Ben": OrderStatus.AUTHORIZED}

    # The decision (FILLED) was saved before the crash, so the rerun completes it.
    result = drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    assert result == DropStatus.FILLED
    assert statuses(session, drop) == {"Ann": OrderStatus.CAPTURED, "Ben": OrderStatus.CAPTURED}
    assert paypal.captured.count(ann.paypal_authorization_id) == 1  # never charged twice


def test_capture_uses_stable_request_ids(session, make_drop, paypal):
    drop = make_drop(quantity_total=10, minimum_units=1)
    order = authorized(session, paypal, drop, 1, "Ann")
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    assert f"capture-{order.id}" in paypal.request_ids


# ---- cancel_drop -----------------------------------------------------------

def test_cancel_voids_holds_and_expires_unpaid(session, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 3, "Ann")
    reserve(session, drop, 1, "Ben")

    result = drops.cancel_drop(session, paypal, drop.id)

    assert result == DropStatus.CANCELLED
    assert statuses(session, drop) == {"Ann": OrderStatus.VOIDED, "Ben": OrderStatus.EXPIRED}
    assert paypal.captured == []
    with pytest.raises(drops.DropNotOpen):
        reserve(session, drop, 1, "Late")


def test_cannot_cancel_a_settled_drop(session, make_drop, paypal):
    drop = make_drop(minimum_units=1)
    authorized(session, paypal, drop, 1, "Ann")
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    with pytest.raises(drops.DropNotOpen):
        drops.cancel_drop(session, paypal, drop.id)


# ---- chat notes: the buyer is told what happened to their payment -------------

def notes_for(session, chat_session_id):
    from app.models import ChatMessage, conversation_id
    return session.scalars(
        select(ChatMessage.text)
        .where(ChatMessage.conversation_id == conversation_id("buyer", chat_session_id))
        .order_by(ChatMessage.id)
    ).all()


def test_hold_adds_a_note_to_the_buyers_chat_once(session, make_drop, paypal):
    drop = make_drop(minimum_units=5)
    order = reserve(session, drop, 2, "Ann")
    drops.start_checkout(session, paypal, order.id, *URLS)
    drops.confirm_authorization(session, paypal, f"PPO-{order.id}")
    drops.confirm_authorization(session, paypal, f"PPO-{order.id}")  # duplicate webhook
    notes = notes_for(session, "chat-Ann")
    assert len(notes) == 1
    assert "on hold" in notes[0] and "not** been charged" in notes[0] and "$18.00" in notes[0] and "minimum of 5" in notes[0]


def test_notes_go_only_to_the_right_buyer(session, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 1, "Ann")
    assert len(notes_for(session, "chat-Ann")) == 1
    assert notes_for(session, "chat-Ben") == []


def test_settlement_tells_buyers_charged_or_released(session, make_drop, paypal):
    filled = make_drop(minimum_units=1)
    authorized(session, paypal, filled, 1, "Ann")
    drops.settle_drop(session, paypal, filled.id, now=after_deadline(filled))
    assert "Payment complete" in notes_for(session, "chat-Ann")[-1]

    missed = make_drop(minimum_units=5)
    authorized(session, paypal, missed, 1, "Ben")
    drops.settle_drop(session, paypal, missed.id, now=after_deadline(missed))
    last = notes_for(session, "chat-Ben")[-1]
    assert "Hold released" in last and "not charged" in last


def test_a_failed_capture_tells_the_buyer_there_was_a_problem(session, make_drop, paypal):
    drop = make_drop(minimum_units=1)
    order = authorized(session, paypal, drop, 1, "Ann")
    paypal.fail_capture.add(order.paypal_authorization_id)
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    assert "Payment problem" in notes_for(session, "chat-Ann")[-1]
