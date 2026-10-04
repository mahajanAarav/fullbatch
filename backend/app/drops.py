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
        select(Drop).where(Drop.id == drop_id).with_for_update()
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
        .with_for_update(skip_locked=True)  # skip rows another worker is already handling
    ).all()

    for order in stale:
        order.status = OrderStatus.EXPIRED
        session.add(
            DropEvent(drop_id=order.drop_id, order_id=order.id, kind="order_expired")
        )
    session.commit()
    return len(stale)
