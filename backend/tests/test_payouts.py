"""Paying sellers after a drop fills: amounts, idempotency, and the ways PayPal can say no."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app import drops, payouts
from app.models import Payout
from tests.fakes import FakePayPal
from tests.test_settlement import after_deadline, authorized


@pytest.fixture
def paypal():
    return FakePayPal()


def filled_drop(session, make_drop, paypal, **overrides):
    """A drop with 6 units paid (minimum 5), past its deadline and fully settled."""
    drop = make_drop(**overrides)
    authorized(session, paypal, drop, 3, "Ann")
    authorized(session, paypal, drop, 3, "Bob")
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    return drop


def test_split_rounds_fee_to_the_cent_and_seller_gets_the_rest():
    assert payouts.split(Decimal("54.00"), 5.0) == (Decimal("2.70"), Decimal("51.30"))
    fee, net = payouts.split(Decimal("10.10"), 5.0)  # 0.505 rounds up
    assert fee == Decimal("0.51") and fee + net == Decimal("10.10")


def test_filled_drop_pays_seller_gross_minus_fee(session, session_factory, make_drop, paypal):
    drop = filled_drop(session, make_drop, paypal)
    payouts.run_payouts(session_factory, paypal, 5.0)
    p = session.scalar(select(Payout).where(Payout.drop_id == drop.id))
    assert (p.gross, p.fee, p.net) == (Decimal("54.00"), Decimal("2.70"), Decimal("51.30"))
    assert p.status == "pending" and p.batch_id == f"PAYOUT-fullbatch-drop-{drop.id}"
    assert paypal.payouts == [{
        "sender_batch_id": f"fullbatch-drop-{drop.id}", "receiver": "bea@example.com",
        "amount": Decimal("51.30"), "currency": "USD",
    }]


def test_running_again_never_pays_twice(session, session_factory, make_drop, paypal):
    filled_drop(session, make_drop, paypal)
    for _ in range(3):
        payouts.run_payouts(session_factory, paypal, 5.0)
    assert len(paypal.payouts) == 1
    assert len(session.scalars(select(Payout)).all()) == 1


def test_failed_drop_pays_nothing(session, session_factory, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 2, "Ann")  # below the minimum of 5
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert paypal.payouts == [] and session.scalar(select(Payout.id)) is None


def test_open_drop_pays_nothing(session, session_factory, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 3, "Ann")
    authorized(session, paypal, drop, 3, "Bob")
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert paypal.payouts == []


def test_no_payout_while_a_hold_is_still_waiting_for_capture(session, session_factory, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 3, "Ann")
    bob = authorized(session, paypal, drop, 3, "Bob")
    paypal.crash_on_capture = bob.paypal_authorization_id  # settlement dies midway
    with pytest.raises(RuntimeError):
        drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert paypal.payouts == []
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))  # recovery
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert len(paypal.payouts) == 1


def test_only_captured_money_is_paid_out(session, session_factory, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 3, "Ann")
    bob = authorized(session, paypal, drop, 3, "Bob")
    paypal.fail_capture.add(bob.paypal_authorization_id)  # Bob's hold expired: not paid
    drops.settle_drop(session, paypal, drop.id, now=after_deadline(drop))
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert paypal.payouts[0]["amount"] == Decimal("25.65")  # 27.00 minus 5%


def test_payouts_not_enabled_is_recorded_and_retried(session, session_factory, make_drop, paypal):
    drop = filled_drop(session, make_drop, paypal)
    paypal.payout_unavailable = True
    payouts.run_payouts(session_factory, paypal, 5.0)
    session.expire_all()
    p = session.scalar(select(Payout).where(Payout.drop_id == drop.id))
    assert p.status == "unavailable" and "403" in p.detail and p.batch_id is None
    paypal.payout_unavailable = False  # the seller enables Payouts; the next run sends it
    payouts.run_payouts(session_factory, paypal, 5.0)
    session.expire_all()
    assert session.scalar(select(Payout.status)) == "pending" and len(paypal.payouts) == 1


def test_pending_payout_is_rechecked_until_final(session, session_factory, make_drop, paypal):
    filled_drop(session, make_drop, paypal)
    paypal.payout_state = "pending"
    payouts.run_payouts(session_factory, paypal, 5.0)
    payouts.run_payouts(session_factory, paypal, 5.0)
    session.expire_all()
    assert session.scalar(select(Payout.status)) == "pending"
    paypal.payout_state = "success"
    payouts.run_payouts(session_factory, paypal, 5.0)
    session.expire_all()
    assert session.scalar(select(Payout.status)) == "success"
    payouts.run_payouts(session_factory, paypal, 5.0)  # final: no more calls, no more sends
    assert len(paypal.payouts) == 1


def test_dev_account_is_skipped_not_paid(session, session_factory, seller_user, make_drop, paypal):
    seller_user.is_dev = True
    session.commit()
    filled_drop(session, make_drop, paypal)
    payouts.run_payouts(session_factory, paypal, 5.0)
    session.expire_all()
    assert session.scalar(select(Payout.status)) == "skipped" and paypal.payouts == []


def test_webhook_event_updates_a_known_batch_only(session, session_factory, make_drop, paypal):
    drop = filled_drop(session, make_drop, paypal)
    payouts.run_payouts(session_factory, paypal, 5.0)
    assert payouts.apply_batch_event(session, f"PAYOUT-fullbatch-drop-{drop.id}", "success") is True
    assert payouts.apply_batch_event(session, "PAYOUT-unknown", "success") is False
    session.expire_all()
    assert session.scalar(select(Payout.status)) == "success"


def test_deadline_job_pays_sellers_end_to_end(session, session_factory, make_drop, paypal):
    drop = make_drop()
    authorized(session, paypal, drop, 3, "Ann")
    authorized(session, paypal, drop, 3, "Bob")
    summary = drops.run_deadline_job(session_factory, paypal, now=after_deadline(drop), fee_percent=10.0)
    assert summary["payouts"] == [drop.id]
    assert paypal.payouts[0]["amount"] == Decimal("48.60")  # 54.00 minus 10%


# ---- webhooks --------------------------------------------------------------

def test_record_money_event_logs_once_and_ignores_unknown(session, session_factory, make_drop, paypal):
    from app.models import DropEvent
    drop = filled_drop(session, make_drop, paypal)
    order = session.scalars(select(drops.Order).where(drops.Order.drop_id == drop.id)).first()
    res = {"id": order.paypal_capture_id, "status": "COMPLETED"}
    for _ in range(2):  # PayPal delivers duplicates
        assert drops.record_money_event(session, "PAYMENT.CAPTURE.COMPLETED", res) is True
    kinds = [e.kind for e in session.scalars(select(DropEvent).where(DropEvent.order_id == order.id))]
    assert kinds.count("paypal_capture_completed") == 1
    assert drops.record_money_event(session, "PAYMENT.CAPTURE.COMPLETED", {"id": "CAP-NOPE"}) is False


def test_refund_notice_finds_its_capture_through_the_up_link(session, make_drop, paypal):
    drop = filled_drop(session, make_drop, paypal)
    order = session.scalars(select(drops.Order).where(drops.Order.drop_id == drop.id)).first()
    refund = {"id": "REFUND-1", "status": "COMPLETED", "links": [
        {"rel": "up", "href": f"https://api.sandbox.paypal.com/v2/payments/captures/{order.paypal_capture_id}"}]}
    assert drops.record_money_event(session, "PAYMENT.CAPTURE.REFUNDED", refund) is True


def test_payout_event_names(session):
    assert payouts.batch_id_of({"batch_header": {"payout_batch_id": "B1"}}) == "B1"
    assert payouts.batch_id_of({"payout_batch_id": "B2", "transaction_status": "SUCCESS"}) == "B2"


def test_order_timeline_tells_the_paypal_story(session, make_drop, paypal):
    drop = filled_drop(session, make_drop, paypal)
    order = session.scalars(select(drops.Order).where(drops.Order.drop_id == drop.id)).first()
    steps = drops.order_timeline(session, order)
    assert [s["kind"] for s in steps] == ["reserved", "checkout_started", "order_authorized", "hold_reauthorized", "order_captured"]  # 3 days passed: renewed
    assert steps[-1]["via"] == "Payments API · capture"
