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

from app import geo
from app.paypal import PayPalError
from app.models import (
    STOCK_HOLDING_STATUSES,
    ChatMessage,
    Drop,
    DropEvent,
    DropStatus,
    Order,
    OrderStatus,
    Seller,
    conversation_id,
)

# PayPal holds last about 29 days, so drops must end well inside that window.
MAX_DROP_DAYS = 14
# How long an unapproved order keeps its stock before it is released.
RESERVATION_MINUTES = 15
MAX_DELIVERY_KM = 50.0   # about 31 miles: a home baker's van, not a courier network
MAX_DELIVERY_FEE = Decimal("50")


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


class SellerNotVerified(DropError):
    pass


class AddressNotFound(DropError):
    pass


class LookupUnavailable(DropError):
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


def locate(geocoder, address: str) -> geo.Place:
    """Turn an address the caller typed into a place, with errors a person can act on."""
    if geocoder is None:
        raise LookupUnavailable("Address lookup isn't available right now.")
    try:
        place = geocoder.lookup(address)
    except geo.GeocodeError:
        raise LookupUnavailable("We couldn't look up that address just now. Please try again in a moment.")
    if place is None:
        raise AddressNotFound("We couldn't find that address. Check the street, city and ZIP code.")
    return place


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
    *,
    offers_pickup: bool = True,
    offers_delivery: bool = False,
    pickup_address: str | None = None,
    pickup_area: str | None = None,
    pickup_notes: str | None = None,
    pickup_lat: float | None = None,
    pickup_lng: float | None = None,
    delivery_radius_km: float | None = None,
    delivery_fee: Decimal = Decimal("0"),
) -> Drop:
    """Validate and insert a new open drop. Its location must already be resolved (see locate)."""
    now = now or _now()

    # Only shops whose owner has a PayPal-verified account may open drops. The rule lives here so
    # every way of creating a drop (web form, chat assistant, scripts) is covered.
    seller = session.get(Seller, seller_id)
    if seller is None:
        raise InvalidDrop("Unknown seller.")
    if not seller.verified:
        raise SellerNotVerified(
            "Your PayPal account isn't verified yet, so you can't open drops. "
            "Verify your PayPal account, then sign in again."
        )

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

    # Where, and how buyers receive it. Every drop has a location, because that is how buyers find it.
    if not (offers_pickup or offers_delivery):
        raise InvalidDrop("Offer pickup, delivery, or both.")
    if not (pickup_address and pickup_address.strip()) or pickup_lat is None or pickup_lng is None:
        raise InvalidDrop("Add the address buyers will collect from (or that deliveries start from).")
    if len(pickup_address) > 300 or (pickup_notes and len(pickup_notes) > 300):
        raise InvalidDrop("The address and pickup notes can each be up to 300 characters.")
    if offers_delivery:
        if delivery_radius_km is None or not 0 < delivery_radius_km <= MAX_DELIVERY_KM:
            raise InvalidDrop(f"Set a delivery distance of up to {MAX_DELIVERY_KM / geo.KM_PER_MILE:.0f} miles.")
        if not 0 <= delivery_fee <= MAX_DELIVERY_FEE:
            raise InvalidDrop(f"The delivery fee can be $0 to ${MAX_DELIVERY_FEE}.")
    else:
        delivery_radius_km, delivery_fee = None, Decimal("0")

    drop = Drop(
        offers_pickup=offers_pickup,
        offers_delivery=offers_delivery,
        pickup_address=pickup_address.strip(),
        pickup_area=(pickup_area or "").strip() or None,
        pickup_notes=(pickup_notes or "").strip() or None,
        pickup_lat=pickup_lat,
        pickup_lng=pickup_lng,
        delivery_radius_km=delivery_radius_km,
        delivery_fee=delivery_fee,
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
    buyer_user_id: int | None = None,
    *,
    fulfillment: str = "pickup",
    delivery_address: str | None = None,
    delivery_lat: float | None = None,
    delivery_lng: float | None = None,
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

        if buyer_user_id is not None:  # the limit is per person, across all their orders on this drop
            already = int(
                session.scalar(
                    select(func.coalesce(func.sum(Order.quantity), 0)).where(
                        Order.drop_id == drop_id,
                        Order.buyer_user_id == buyer_user_id,
                        Order.status.in_(STOCK_HOLDING_STATUSES),
                    )
                )
            )
            if already + quantity > drop.max_per_buyer:
                left = max(drop.max_per_buyer - already, 0)
                raise InvalidOrder(
                    f"The limit is {drop.max_per_buyer} per person, and you already have {already}. "
                    + (f"You can add {left} more." if left else "You've reached it.")
                )

        fee = Decimal("0")
        if fulfillment == "pickup":
            if not drop.offers_pickup:
                raise InvalidOrder("This drop is delivery only.")
        elif fulfillment == "delivery":
            if not drop.offers_delivery:
                raise InvalidOrder("This drop doesn't offer delivery. Choose pickup instead.")
            if not (delivery_address and delivery_address.strip()) or delivery_lat is None or delivery_lng is None:
                raise InvalidOrder("Add the address to deliver to.")
            away = geo.haversine_km(drop.pickup_lat, drop.pickup_lng, delivery_lat, delivery_lng)
            if away > drop.delivery_radius_km:
                raise InvalidOrder(
                    f"That address is {away / geo.KM_PER_MILE:.1f} miles away. "
                    f"This seller delivers within {drop.delivery_radius_km / geo.KM_PER_MILE:.1f} miles."
                )
            fee = drop.delivery_fee
        else:
            raise InvalidOrder("Choose pickup or delivery.")

        order = Order(
            fulfillment=fulfillment,
            delivery_address=delivery_address.strip()[:300] if fulfillment == "delivery" else None,
            delivery_lat=delivery_lat if fulfillment == "delivery" else None,
            delivery_lng=delivery_lng if fulfillment == "delivery" else None,
            delivery_fee=fee,
            drop_id=drop_id,
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            buyer_user_id=buyer_user_id,
            chat_session_id=chat_session_id,
            quantity=quantity,
            amount=drop.unit_price * quantity + fee,
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


def _money(order: Order, currency: str) -> str:
    return f"${order.amount:,.2f}" if currency == "USD" else f"{order.amount:,.2f} {currency}"


def _chat_note(session: Session, order: Order, drop: Drop, kind: str) -> None:
    """
    Tell the buyer, in their chat, what just happened to their payment. It is stored with
    the conversation, so it shows up even if they approved in another tab, and the
    assistant knows about it too if they ask.
    """
    what = f"{_money(order, drop.currency)} for {order.quantity} \u00d7 {drop.item_name}"
    if order.fulfillment == "delivery":
        where = f" It will be delivered to {order.delivery_address}."
    else:
        notes = f" {drop.pickup_notes}" if drop.pickup_notes else ""
        where = f" Pickup address: {drop.pickup_address}.{notes}" if drop.pickup_address else ""
    text = {
        "authorized": (
            f"**Payment on hold.** {what} is now on hold with PayPal. You have **not** been charged. "
            f"You will only be charged if the drop reaches its minimum of {drop.minimum_units} units by the "
            f"deadline. Otherwise the hold is released automatically.{where}"
        ),
        "captured": f"**Payment complete.** The drop reached its minimum, so {what} was charged. Thank you!",
        "voided": (
            f"**Hold released.** The drop did not reach its minimum, so the hold for {what} was released. "
            "You were not charged."
        ),
        "failed": (
            f"**Payment problem.** We could not complete the payment for {what}. "
            "Please contact the seller. You have not been charged twice."
        ),
    }[kind]
    session.add(ChatMessage(conversation_id=conversation_id("buyer", order.chat_session_id), role="assistant", text=text))


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
    _chat_note(session, order, drop, "authorized")
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
    drop = session.get(Drop, drop_id)
    status = drop.status
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
                    _chat_note(session, order, drop, "captured")
                else:
                    paypal.void_authorization(
                        order.paypal_authorization_id, request_id=f"void-{order.id}"
                    )
                    order.status = OrderStatus.VOIDED
                    _log(session, drop_id, "order_voided", order.id)
                    _chat_note(session, order, drop, "voided")
            except PayPalError as err:
                # One bad hold (e.g. expired) must not stop the rest.
                order.status = OrderStatus.FAILED
                _log(session, drop_id, "order_failed", order.id, {"error": str(err)[:200]})
                _chat_note(session, order, drop, "failed")
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


def release_reservation(session: Session, order_id: int, reason: str = "released") -> None:
    """Give a reservation's stock back right away (buyer cancelled, or checkout failed)."""
    order = session.execute(
        select(Order).where(Order.id == order_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if order is not None and order.status == OrderStatus.RESERVED:
        order.status = OrderStatus.EXPIRED
        _log(session, order.drop_id, "order_expired", order.id, {"reason": reason})
    session.commit()


def run_deadline_job(session_factory, paypal, now: datetime | None = None) -> dict:
    """
    The scheduled job. Safe to run as often as you like, from several places at once.
      1. expire unpaid reservations whose time is up
      2. settle every open drop that is past its deadline
      3. finish drops that were decided but not fully processed (e.g. after a crash)
    A problem with one drop is recorded and never stops the others.
    """
    now = now or _now()
    summary = {"expired": 0, "settled": [], "errors": []}

    with session_factory() as session:
        summary["expired"] = expire_stale_reservations(session, now)
        due = session.scalars(
            select(Drop.id).where(Drop.status == DropStatus.OPEN, Drop.deadline <= now)
        ).all()
        unfinished = session.scalars(
            select(Drop.id).where(
                Drop.status != DropStatus.OPEN,
                select(Order.id)
                .where(Order.drop_id == Drop.id, Order.status == OrderStatus.AUTHORIZED)
                .exists(),
            )
        ).all()

    for drop_id in [*due, *unfinished]:
        with session_factory() as session:
            try:
                settle_drop(session, paypal, drop_id, now=now)
                summary["settled"].append(drop_id)
            except Exception as err:  # keep going: one bad drop must not block the rest
                session.rollback()
                summary["errors"].append({"drop_id": drop_id, "error": str(err)[:200]})
    return summary


def drop_summary(session: Session, drop: Drop) -> dict:
    """A drop as plain data, with live stock numbers. Shared by the API and the agent."""
    taken = units_taken(session, drop.id)
    return {
        "id": drop.id,
        "seller_id": drop.seller_id,
        "shop_name": drop.seller.name,
        # Where it is, publicly: a neighborhood and rounded coordinates. The exact address and the
        # pickup notes are shared only after a buyer's hold is approved.
        "offers_pickup": drop.offers_pickup,
        "offers_delivery": drop.offers_delivery,
        "area": drop.pickup_area,
        "lat": None if drop.pickup_lat is None else geo.public_coord(drop.pickup_lat),
        "lng": None if drop.pickup_lng is None else geo.public_coord(drop.pickup_lng),
        "delivery_radius_km": drop.delivery_radius_km,
        "delivery_fee": str(drop.delivery_fee),
        "item_name": drop.item_name,
        "unit_price": str(drop.unit_price),
        "currency": drop.currency,
        "quantity_total": drop.quantity_total,
        "minimum_units": drop.minimum_units,
        "max_per_buyer": drop.max_per_buyer,
        "deadline": drop.deadline.isoformat(),
        "status": drop.status.value,
        "units_taken": taken,
        "units_remaining": drop.quantity_total - taken,
    }


def place_order(
    session: Session,
    paypal,
    drop_id: int,
    buyer_name: str,
    buyer_email: str,
    chat_session_id: str,
    quantity: int,
    return_url: str,
    cancel_url: str,
    buyer_user_id: int | None = None,
    *,
    fulfillment: str = "pickup",
    delivery_address: str | None = None,
    delivery_lat: float | None = None,
    delivery_lng: float | None = None,
) -> tuple[Order, str]:
    """
    Reserve stock and start PayPal checkout. Returns (order, approval_link).
    If PayPal fails, the reservation is released at once instead of holding
    the stock for 15 minutes, and the PayPalError is re-raised.
    """
    order = reserve_stock(
        session, drop_id, buyer_name, buyer_email, chat_session_id, quantity, buyer_user_id=buyer_user_id,
        fulfillment=fulfillment, delivery_address=delivery_address, delivery_lat=delivery_lat, delivery_lng=delivery_lng,
    )
    try:
        link = start_checkout(session, paypal, order.id, return_url, cancel_url)
    except PayPalError:
        session.rollback()
        release_reservation(session, order.id, reason="checkout_failed")
        raise
    return order, link


def drop_progress(session: Session, drop: Drop) -> dict:
    """A drop with live stock numbers AND how far along it is toward its minimum."""
    counts = dict(
        session.execute(
            select(Order.status, func.coalesce(func.sum(Order.quantity), 0))
            .where(Order.drop_id == drop.id)
            .group_by(Order.status)
        ).all()
    )
    units = {status.value: int(counts.get(status, 0)) for status in OrderStatus}
    # Only buyers who approved on PayPal (a hold is in place) count toward the minimum.
    paid_up = units["authorized"] + units["captured"]
    return {
        **drop_summary(session, drop),
        "units_by_order_status": units,
        "paid_up_units": paid_up,
        "minimum_met_so_far": paid_up >= drop.minimum_units,
    }


def list_open_drops(session: Session, now: datetime | None = None, limit: int = 20) -> list[Drop]:
    """Drops that are still taking orders, soonest deadline first."""
    now = now or _now()
    return list(
        session.scalars(
            select(Drop)
            .where(Drop.status == DropStatus.OPEN, Drop.deadline > now)
            .order_by(Drop.deadline)
            .limit(limit)
        )
    )
