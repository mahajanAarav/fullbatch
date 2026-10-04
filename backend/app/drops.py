"""
Drop engine: creating drops, reserving stock, and releasing stale reservations.

Every function here takes a SQLAlchemy session and commits its own transaction.
Stock is never stored as a counter. It is computed from the orders table, so
expiring or voiding an order frees its units automatically.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.paypal import PayPalError
from app.models import (
    STOCK_HOLDING_STATUSES,
    Drop,
    DropEvent,
    DropStatus,
    Order,
    OrderStatus,
)

# PayPal holds last about 29 days, so drops must end well inside that window.
MAX_DROP_DAYS = 14
# How long an unapproved order keeps its stock before it is released.
RESERVATION_MINUTES = 15


class DropError(Exception):
    """Base class, so callers can catch every drop-engine problem at once."""


class InvalidDrop(DropError):
    pass


class DropNotFound(DropError):
    pass


class DropNotOpen(DropError):
    pass


class InvalidOrder(DropError):
    pass


class OrderNotFound(DropError):
    pass


class OrderNotPayable(DropError):
    pass


class DropNotDue(DropError):
    pass


class SoldOut(DropError):
    def __init__(self, remaining: int):
        self.remaining = remaining
        if remaining == 0:
            super().__init__("This drop is sold out.")
        else:
            super().__init__(f"Only {remaining} left.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_drop(
    session: Session,
    seller_id: int,
    item_name: str,
    unit_price: Decimal,
    quantity_total: int,
    minimum_units: int,
    deadline: datetime,
    max_per_buyer: int = 4,
    now: datetime | None = None,
) -> Drop:
    """Validate and insert a new open drop."""
    now = now or _now()

    if not item_name.strip():
        raise InvalidDrop("The item needs a name.")
    if unit_price <= 0:
        raise InvalidDrop("The price must be more than zero.")
    if quantity_total <= 0:
        raise InvalidDrop("The quantity must be more than zero.")
    if minimum_units <= 0 or minimum_units > quantity_total:
        raise InvalidDrop("The minimum must be at least 1 and no more than the quantity.")
    if deadline <= now:
        raise InvalidDrop("The deadline must be in the future.")
    if deadline > now + timedelta(days=MAX_DROP_DAYS):
        raise InvalidDrop(f"Drops can run at most {MAX_DROP_DAYS} days.")

    drop = Drop(
        seller_id=seller_id,
        item_name=item_name.strip(),
        unit_price=unit_price,
        quantity_total=quantity_total,
        minimum_units=minimum_units,
        max_per_buyer=max_per_buyer,
        deadline=deadline,
        status=DropStatus.OPEN,
    )
    session.add(drop)
    session.flush()  # assigns drop.id so the event below can point at it
    session.add(DropEvent(drop_id=drop.id, kind="drop_created"))
    session.commit()
    return drop


def units_taken(session: Session, drop_id: int) -> int:
    """Units used up by orders that are holding stock."""
    total = session.scalar(
        select(func.coalesce(func.sum(Order.quantity), 0)).where(
            Order.drop_id == drop_id,
            Order.status.in_(STOCK_HOLDING_STATUSES),
        )
    )
    return int(total)


def reserve_stock(
    session: Session,
    drop_id: int,
    buyer_name: str,
    buyer_email: str,
    chat_session_id: str,
    quantity: int,
    now: datetime | None = None,
) -> Order:
    """
    Reserve units for a buyer, safely under concurrency.

    The row lock on the drop (SELECT ... FOR UPDATE) makes buyers take turns.
    A second buyer waits at the lock, then counts the stock again and sees
    what the first buyer took. That is what stops the last unit being sold twice.
    """
    now = now or _now()

    # Take the lock first. Every check below happens while we hold it.
    drop = session.execute(
        select(Drop).where(Drop.id == drop_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if drop is None:
        session.rollback()
        raise DropNotFound(f"No drop with id {drop_id}.")

    try:
        if drop.status != DropStatus.OPEN or drop.deadline <= now:
            raise DropNotOpen("This drop is no longer taking orders.")
        if quantity <= 0:
            raise InvalidOrder("The quantity must be at least 1.")
        if quantity > drop.max_per_buyer:
            raise InvalidOrder(f"The limit is {drop.max_per_buyer} per buyer.")

        remaining = drop.quantity_total - units_taken(session, drop_id)
        if quantity > remaining:
            raise SoldOut(remaining)

        order = Order(
            drop_id=drop_id,
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            chat_session_id=chat_session_id,
            quantity=quantity,
            amount=drop.unit_price * quantity,
            status=OrderStatus.RESERVED,
            reserved_until=now + timedelta(minutes=RESERVATION_MINUTES),
        )
        session.add(order)
        session.flush()
        session.add(
            DropEvent(
                drop_id=drop_id,
                order_id=order.id,
                kind="order_reserved",
                detail={"quantity": quantity},
            )
        )
        session.commit()  # releases the lock
        return order
    except DropError:
        session.rollback()  # releases the lock without changing anything
        raise


def expire_stale_reservations(session: Session, now: datetime | None = None) -> int:
    """
    Mark reserved orders past their reserved_until time as expired.
    Returns how many were expired. Their stock is freed automatically,
    because stock is computed from order statuses.
    """
    now = now or _now()

    stale = session.scalars(
        select(Order)
        .where(Order.status == OrderStatus.RESERVED, Order.reserved_until <= now)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True)  # skip rows another worker is already handling
    ).all()

    for order in stale:
        order.status = OrderStatus.EXPIRED
        session.add(
            DropEvent(drop_id=order.drop_id, order_id=order.id, kind="order_expired")
        )
    session.commit()
    return len(stale)


# ---- checkout, authorization and settlement -------------------------------
#
# `paypal` below is a PayPalClient (or a test fake with the same methods).
# Drop code decides WHEN to call PayPal; the LLM never does.


def _log(session: Session, drop_id: int, kind: str, order_id: int | None = None, detail: dict | None = None):
    session.add(DropEvent(drop_id=drop_id, order_id=order_id, kind=kind, detail=detail))


def start_checkout(
    session: Session,
    paypal,
    order_id: int,
    return_url: str,
    cancel_url: str,
    now: datetime | None = None,
) -> str:
    """Create the PayPal order for a reservation. Returns the link the buyer approves."""
    now = now or _now()
    order = session.get(Order, order_id)
    if order is None:
        raise OrderNotFound(f"No order with id {order_id}.")
    if order.status != OrderStatus.RESERVED or order.reserved_until <= now:
        raise OrderNotPayable("This reservation has expired. Please start a new order.")
    drop = session.get(Drop, order.drop_id)

    # A fixed request id means asking twice returns the same PayPal order and link.
    paypal_order_id, link = paypal.create_order(
        amount=order.amount,
        currency=drop.currency,
        description=f"{drop.item_name} x{order.quantity}",
        return_url=return_url,
        cancel_url=cancel_url,
        custom_id=str(order.id),
        request_id=f"create-order-{order.id}",
    )
    order.paypal_order_id = paypal_order_id
    _log(session, order.drop_id, "checkout_started", order.id)
    session.commit()
    return link


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def confirm_authorization(session: Session, paypal, paypal_order_id: str) -> Order:
    """
    Called when the buyer has approved on PayPal: place the hold.

    The PayPal call is made WITHOUT any database lock (it can be slow, and
    other buyers must not wait for it). Afterwards we re-check under lock.
    If the order expired or the drop closed in the meantime, the hold we
    just made is released straight away, so no stray holds are left behind.
    """
    order = session.scalar(select(Order).where(Order.paypal_order_id == paypal_order_id))
    if order is None:
        raise OrderNotFound("No order matches that PayPal order.")
    if order.status == OrderStatus.AUTHORIZED:
        return order  # duplicate webhook or double click: nothing to do
    if order.status != OrderStatus.RESERVED:
        raise OrderNotPayable(f"This order is {order.status.value}.")
    order_id, drop_id = order.id, order.drop_id
    if session.get(Drop, drop_id).status != DropStatus.OPEN:
        raise DropNotOpen("This drop is no longer taking orders.")
    session.rollback()  # end the read transaction before the slow PayPal call

    authorization = paypal.authorize_order(paypal_order_id, request_id=f"authorize-{order_id}")

    # Re-check under lock. Always lock the drop first, then the order.
    drop = session.execute(
        select(Drop).where(Drop.id == drop_id).with_for_update(read=True).execution_options(populate_existing=True)
    ).scalar_one()
    order = session.execute(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()

    if order.status == OrderStatus.AUTHORIZED:
        session.rollback()
        return order  # another request finished first

    if order.status != OrderStatus.RESERVED or drop.status != DropStatus.OPEN:
        detail = {"authorization_id": authorization["id"]}
        try:
            paypal.void_authorization(authorization["id"], request_id=f"void-{order_id}")
            _log(session, drop_id, "late_hold_released", order_id, detail)
        except PayPalError as err:
            _log(session, drop_id, "late_hold_release_failed", order_id, {**detail, "error": str(err)[:200]})
        session.commit()
        raise OrderNotPayable("This order or drop closed before the payment was approved.")

    order.status = OrderStatus.AUTHORIZED
    order.paypal_authorization_id = authorization["id"]
    order.authorization_expires_at = _parse_time(authorization.get("expiration_time"))
    _log(session, drop_id, "order_authorized", order_id)
    session.commit()
    return order


def _lock_drop(session: Session, drop_id: int) -> Drop:
    drop = session.execute(
        select(Drop).where(Drop.id == drop_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if drop is None:
        session.rollback()
        raise DropNotFound(f"No drop with id {drop_id}.")
    return drop


def _expire_unpaid(session: Session, drop_id: int) -> None:
    """Reserved orders have no PayPal hold, so closing a drop just expires them."""
    for order in session.scalars(
        select(Order).where(Order.drop_id == drop_id, Order.status == OrderStatus.RESERVED)
    ):
        order.status = OrderStatus.EXPIRED
        _log(session, drop_id, "order_expired", order.id, {"reason": "drop_closed"})


def settle_drop(session: Session, paypal, drop_id: int, now: datetime | None = None) -> DropStatus:
    """
    Run at the deadline: capture every hold if the minimum was met, else void them all.

    Phase 1 decides filled or failed ONCE and saves that decision. Phase 2 then
    acts on each hold and commits after each one. If something crashes midway,
    running this again finishes the leftovers without recounting, which would
    otherwise change the answer because captured holds stop being "authorized".
    """
    now = now or _now()
    drop = _lock_drop(session, drop_id)

    if drop.status == DropStatus.OPEN:
        if drop.deadline > now:
            session.rollback()
            raise DropNotDue("The deadline has not passed yet.")
        _expire_unpaid(session, drop_id)
        units = int(
            session.scalar(
                select(func.coalesce(func.sum(Order.quantity), 0)).where(
                    Order.drop_id == drop_id, Order.status == OrderStatus.AUTHORIZED
                )
            )
        )
        filled = units >= drop.minimum_units
        drop.status = DropStatus.FILLED if filled else DropStatus.FAILED
        _log(
            session,
            drop_id,
            "drop_filled" if filled else "drop_failed",
            detail={"authorized_units": units, "minimum_units": drop.minimum_units},
        )
    session.commit()
    return _process_holds(session, paypal, drop_id, now)


def cancel_drop(session: Session, paypal, drop_id: int, now: datetime | None = None) -> DropStatus:
    """Seller cancels an open drop: every hold is released, nobody is charged."""
    now = now or _now()
    drop = _lock_drop(session, drop_id)
    if drop.status == DropStatus.OPEN:
        _expire_unpaid(session, drop_id)
        drop.status = DropStatus.CANCELLED
        _log(session, drop_id, "drop_cancelled")
    elif drop.status != DropStatus.CANCELLED:
        session.rollback()
        raise DropNotOpen("Only an open drop can be cancelled.")
    session.commit()
    return _process_holds(session, paypal, drop_id, now)


def _process_holds(session: Session, paypal, drop_id: int, now: datetime) -> DropStatus:
    """Capture (filled) or void (failed/cancelled) each remaining hold, one at a time."""
    status = session.scalar(select(Drop.status).where(Drop.id == drop_id))
    order_ids = session.scalars(
        select(Order.id)
        .where(Order.drop_id == drop_id, Order.status == OrderStatus.AUTHORIZED)
        .order_by(Order.id)
    ).all()

    for order_id in order_ids:
        try:
            order = session.execute(
                select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
            ).scalar_one()
            if order.status != OrderStatus.AUTHORIZED:
                session.rollback()  # someone else already handled it
                continue
            try:
                if status == DropStatus.FILLED:
                    paypal.capture_authorization(
                        order.paypal_authorization_id, request_id=f"capture-{order.id}"
                    )
                    order.status = OrderStatus.CAPTURED
                    _log(session, drop_id, "order_captured", order.id)
                else:
                    paypal.void_authorization(
                        order.paypal_authorization_id, request_id=f"void-{order.id}"
                    )
                    order.status = OrderStatus.VOIDED
                    _log(session, drop_id, "order_voided", order.id)
            except PayPalError as err:
                # One bad hold (e.g. expired) must not stop the rest.
                order.status = OrderStatus.FAILED
                _log(session, drop_id, "order_failed", order.id, {"error": str(err)[:200]})
            session.commit()
        except Exception:
            session.rollback()
            raise

    remaining = session.scalar(
        select(func.count()).select_from(Order).where(
            Order.drop_id == drop_id, Order.status == OrderStatus.AUTHORIZED
        )
    )
    if remaining == 0:
        drop = _lock_drop(session, drop_id)
        if drop.settled_at is None:
            drop.settled_at = now
        session.commit()
    return status
