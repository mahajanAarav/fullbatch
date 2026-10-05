"""
Paying sellers. When a drop fills and every hold has been captured, the money sits in the platform's
PayPal account. This sends the seller their share (what buyers paid, minus our fee) with the PayPal
Payouts API.

Safe to run over and over: a drop gets exactly one payout row (unique drop_id), and PayPal gets the
same `sender_batch_id` each time, so it cannot pay twice even if we crash right after sending.
"""

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Drop, DropStatus, Order, OrderStatus, Payout, Seller
from app.paypal import PayPalError

CENTS = Decimal("0.01")
# Statuses that need no more work. "unavailable" is retried; "pending" is re-checked with PayPal.
FINAL = ("success", "unclaimed", "denied", "skipped")


def split(gross: Decimal, fee_percent: float) -> tuple[Decimal, Decimal]:
    """(fee, net). The fee rounds to the cent and the seller gets exactly the rest."""
    fee = (gross * Decimal(str(fee_percent)) / 100).quantize(CENTS, rounding=ROUND_HALF_UP)
    return fee, gross - fee


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _batch_id(drop_id: int) -> str:
    return f"fullbatch-drop-{drop_id}"


def _ready(session: Session, drop: Drop) -> bool:
    """A drop is ready to pay out when it filled and no hold is waiting to be captured."""
    if drop.status != DropStatus.FILLED or drop.settled_at is None:
        return False
    waiting = session.scalar(
        select(Order.id).where(Order.drop_id == drop.id, Order.status == OrderStatus.AUTHORIZED).limit(1)
    )
    return waiting is None


def pay_drop(session: Session, paypal, drop_id: int, fee_percent: float) -> Payout | None:
    """Create (or retry, or re-check) the payout for one drop. Returns the row, or None if not ready."""
    drop = session.get(Drop, drop_id)
    if drop is None or not _ready(session, drop):
        return None

    payout = session.scalar(select(Payout).where(Payout.drop_id == drop_id).with_for_update())
    if payout is None:
        gross = sum(
            (o.amount for o in session.scalars(
                select(Order).where(Order.drop_id == drop_id, Order.status == OrderStatus.CAPTURED)
            )),
            Decimal("0"),
        )
        if gross <= 0:
            return None
        seller = session.get(Seller, drop.seller_id)
        owner = seller.user if seller else None
        fee, net = split(gross, fee_percent)
        payout = Payout(
            drop_id=drop_id, seller_id=drop.seller_id, gross=gross, fee=fee, net=net,
            currency=drop.currency, receiver_email=owner.email if owner else None,
        )
        if owner is None or owner.is_dev or not owner.paypal_payer_id or net <= 0:
            # Dev sign-ins have no real PayPal account to pay.
            payout.status, payout.detail = "skipped", "The shop owner has no PayPal account to pay."
        session.add(payout)
        session.commit()

    if payout.status in FINAL:
        return payout

    if payout.batch_id is None:  # never sent, or PayPal wasn't ready last time
        try:
            sent = paypal.create_payout(
                sender_batch_id=_batch_id(drop_id),
                receiver_email=payout.receiver_email,
                amount=payout.net,
                currency=payout.currency,
                note=f"fullbatch payout for “{drop.item_name}”",
                sender_item_id=f"drop-{drop_id}",
            )
            payout.batch_id, payout.status, payout.detail = sent["batch_id"], "pending", None
        except PayPalError as err:
            # Most often: Payouts isn't enabled on the account yet. Keep the row and try again later.
            payout.status, payout.detail = "unavailable", f"{err.status_code}: {err.body[:200]}"
        payout.updated_at = _now()
        session.commit()
        return payout

    return refresh_payout(session, paypal, payout)


def refresh_payout(session: Session, paypal, payout: Payout) -> Payout:
    """Ask PayPal how a sent payout is going and record the answer."""
    try:
        state = paypal.get_payout(payout.batch_id)
    except PayPalError:
        return payout  # try again next time
    payout.status, payout.detail = state["status"], state["detail"][:300] if state["detail"] else None
    payout.updated_at = _now()
    session.commit()
    return payout


def apply_batch_event(session: Session, batch_id: str, status: str, detail: str | None = None) -> bool:
    """A webhook told us a payout's final state. Returns whether we knew that batch."""
    payout = session.scalar(select(Payout).where(Payout.batch_id == batch_id).with_for_update())
    if payout is None:
        session.rollback()
        return False
    payout.status, payout.detail, payout.updated_at = status, detail, _now()
    session.commit()
    return True


def run_payouts(session_factory, paypal, fee_percent: float) -> list[int]:
    """Pay every settled, filled drop that still needs it. One failure never stops the rest."""
    with session_factory() as session:
        ids = session.scalars(
            select(Drop.id)
            .where(Drop.status == DropStatus.FILLED, Drop.settled_at.is_not(None))
            .where(~select(Payout.id).where(Payout.drop_id == Drop.id, Payout.status.in_(FINAL)).exists())
            .order_by(Drop.id)
        ).all()
    done = []
    for drop_id in ids:
        with session_factory() as session:
            try:
                if pay_drop(session, paypal, drop_id, fee_percent):
                    done.append(drop_id)
            except Exception:
                session.rollback()
    return done


# PayPal webhook event -> our payout status. Item events carry the final word on the money.
EVENT_STATUS = {
    "PAYMENT.PAYOUTSBATCH.SUCCESS": "success",
    "PAYMENT.PAYOUTSBATCH.DENIED": "denied",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": "success",
    "PAYMENT.PAYOUTS-ITEM.UNCLAIMED": "unclaimed",
    "PAYMENT.PAYOUTS-ITEM.DENIED": "denied",
    "PAYMENT.PAYOUTS-ITEM.FAILED": "denied",
    "PAYMENT.PAYOUTS-ITEM.RETURNED": "denied",
    "PAYMENT.PAYOUTS-ITEM.BLOCKED": "denied",
    "PAYMENT.PAYOUTS-ITEM.CANCELED": "denied",
}


def batch_id_of(resource: dict) -> str | None:
    """Batch events nest the id in batch_header; item events carry it directly."""
    return ((resource.get("batch_header") or {}).get("payout_batch_id")) or resource.get("payout_batch_id")
