"""
fullbatch HTTP API.

Public:       GET /health, GET /drops, GET /drops/{id}
Sign-in:      /auth/... (see app/auth.py)
Seller:       POST /shop, POST /drops, GET /me/drops, POST /drops/{id}/cancel
Buyer:        POST /drops/{id}/orders, GET /me/orders, GET /orders/{id}
Chat:         POST /chat/seller, POST /chat/buyer, GET /chat/{role}/history
PayPal:       GET /paypal/return, GET /paypal/cancel, POST /paypal/webhook

Who is calling always comes from the sign-in cookie. Nothing in a request body can
name a different seller or buyer.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import agent, auth, drops, geo, payouts, planner, studio_ai
from app.auth import buyer_chat_id, require_shop, require_user, require_verified_email
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_geocoder, get_llm, get_paypal
from app.llm import LLMError
from app.models import ChatMessage, Drop, Order, OrderStatus, Payout, Seller, User
from app.paypal import PayPalError
from app.ratelimit import RateLimiter

app = FastAPI(title="fullbatch")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_url],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.include_router(auth.router)
app.include_router(studio_ai.router)


# ---- turn drop-engine errors into proper HTTP answers ----------------------

def _error(status: int):
    async def handler(request: Request, exc: Exception):
        body = {"error": str(exc)}
        if isinstance(exc, drops.SoldOut):
            body["remaining"] = exc.remaining
        return JSONResponse(status_code=status, content=body)
    return handler


for _exc, _status in [
    (drops.DropNotFound, 404),
    (drops.OrderNotFound, 404),
    (drops.SellerNotVerified, 403),
    (drops.SoldOut, 409),
    (drops.DropNotOpen, 409),
    (drops.OrderNotPayable, 409),
    (drops.DropNotDue, 409),
    (drops.InvalidDrop, 422),
    (drops.AddressNotFound, 422),
    (drops.LookupUnavailable, 503),
    (drops.InvalidOrder, 422),
]:
    app.add_exception_handler(_exc, _error(_status))
app.add_exception_handler(LLMError, _error(503))


# ---- request bodies --------------------------------------------------------

class ShopIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DropIn(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    unit_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    quantity_total: int = Field(gt=0)
    minimum_units: int = Field(gt=0)
    deadline: AwareDatetime  # must include a timezone, so "5pm" is never ambiguous
    max_per_buyer: int = Field(default=4, gt=0)
    # Where, and how buyers receive it. The address is looked up on the server.
    pickup_address: str = Field(min_length=3, max_length=300)
    pickup_notes: str | None = Field(default=None, max_length=300)
    offers_pickup: bool = True
    offers_delivery: bool = False
    delivery_radius_miles: float | None = Field(default=None, gt=0, le=31)
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0, le=50, max_digits=6, decimal_places=2)


class OrderIn(BaseModel):
    quantity: int
    fulfillment: Literal["pickup", "delivery"] = "pickup"
    delivery_address: str | None = Field(default=None, max_length=300)


class GeocodeIn(BaseModel):
    query: str = Field(min_length=3, max_length=300)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


# ---- helpers ---------------------------------------------------------------

def frontend_order_url(settings: Settings, order_id: int | None, status: str) -> str:
    path = f"/orders/{order_id}" if order_id is not None else "/orders"
    return f"{settings.frontend_url}{path}?status={status}"


def my_drop(session: Session, shop: Seller, drop_id: int) -> Drop:
    """One of the signed-in seller's own drops. Someone else's looks exactly like a missing one."""
    drop = session.get(Drop, drop_id)
    if drop is None or drop.seller_id != shop.id:
        raise drops.DropNotFound(f"No drop with id {drop_id}.")
    return drop


# The exact address and notes are shared only once the buyer's hold is approved (or the order is complete).
APPROVED = ("authorized", "captured")


def order_view(session: Session, order: Order) -> dict:
    drop = session.get(Drop, order.drop_id)
    progress = drops.drop_progress(session, drop)
    approved = order.status.value in APPROVED
    return {
        "can_pay": order.status == OrderStatus.RESERVED and order.reserved_until > datetime.now(timezone.utc),
        "fulfillment": order.fulfillment,
        "delivery_fee": str(order.delivery_fee),
        "area": drop.pickup_area,
        "pickup_address": drop.pickup_address if approved and order.fulfillment == "pickup" else None,
        "pickup_notes": drop.pickup_notes if approved and order.fulfillment == "pickup" else None,
        "delivery_address": order.delivery_address,  # the buyer's own address, only ever shown back to them
        "id": order.id,
        "status": order.status.value,
        "quantity": order.quantity,
        "amount": str(order.amount),
        "currency": drop.currency,
        "reserved_until": order.reserved_until.isoformat(),
        "drop_id": drop.id,
        "item_name": drop.item_name,
        "drop_status": drop.status.value,
        "deadline": drop.deadline.isoformat(),
        "minimum_units": drop.minimum_units,
        "paid_up_units": progress["paid_up_units"],
        # The PayPal side of this order, so the buyer can see exactly where their money is.
        "paypal": {
            "order_id": order.paypal_order_id,
            "authorization_id": order.paypal_authorization_id,
            "hold_expires": order.authorization_expires_at.isoformat() if order.authorization_expires_at else None,
            "capture_id": order.paypal_capture_id,
        },
        "timeline": drops.order_timeline(session, order),
    }


# ---- public ----------------------------------------------------------------

@app.get("/config")
def public_config(settings: Settings = Depends(get_settings)):
    """What the browser needs to load PayPal's buttons. The client id is public by design; the secret never leaves the server."""
    return {"paypal_client_id": settings.paypal_client_id or None, "currency": "USD", "demo_mode": settings.demo_mode}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/drops")
def list_drops(session: Session = Depends(get_session)):
    """Drops currently taking orders (what a buyer can choose from)."""
    return {"drops": [drops.drop_progress(session, d) for d in drops.list_open_drops(session)]}


@app.get("/shops/{shop_id}")
def get_shop(shop_id: int, session: Session = Depends(get_session)):
    """
    A shop's public page: who they are, where (neighborhood only), and how their drops have gone.
    It is built from real activity, never from anything the seller typed about themselves, and it carries
    no addresses, emails or buyer names.
    """
    shop = session.get(Seller, shop_id)
    if shop is None:
        raise drops.DropNotFound(f"No shop with id {shop_id}.")
    all_drops = session.scalars(select(Drop).where(Drop.seller_id == shop.id).order_by(Drop.id.desc())).all()
    filled = [d for d in all_drops if d.status.value == "filled"]
    units_delivered = int(
        session.scalar(
            select(func.coalesce(func.sum(Order.quantity), 0)).where(
                Order.drop_id.in_([d.id for d in filled] or [0]), Order.status == OrderStatus.CAPTURED
            )
        )
    )
    open_drops = [d for d in all_drops if d.status.value == "open" and d.deadline > datetime.now(timezone.utc)]
    return {
        "id": shop.id,
        "name": shop.name,
        "verified": shop.verified,
        "member_since": shop.created_at.isoformat(),
        "area": next((d.pickup_area for d in all_drops if d.pickup_area), None),
        "drops_filled": len(filled),
        "drops_total": sum(1 for d in all_drops if d.status.value in ("filled", "failed")),
        "units_delivered": units_delivered,
        "open_drops": [drops.drop_progress(session, d) for d in open_drops],
    }


@app.get("/drops/{drop_id}")
def get_drop(drop_id: int, session: Session = Depends(get_session)):
    drop = session.get(Drop, drop_id)
    if drop is None:
        raise drops.DropNotFound(f"No drop with id {drop_id}.")
    return drops.drop_summary(session, drop)


# ---- seller routes ---------------------------------------------------------

@app.post("/shop", status_code=201)
def create_shop(body: ShopIn, user: User = Depends(require_verified_email), session: Session = Depends(get_session)):
    """The signed-in user opens their shop. One shop per person."""
    if auth.shop_of(session, user) is not None:
        raise HTTPException(409, "You already have a shop.")
    shop = Seller(name=body.name.strip(), user_id=user.id)
    session.add(shop)
    session.commit()
    return {"id": shop.id, "name": shop.name, "verified": shop.verified}


@app.post("/drops", status_code=201)
def create_drop(
    body: DropIn,
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    geocoder=Depends(get_geocoder),
):
    # create_drop refuses shops whose owner is not PayPal-verified (SellerNotVerified -> 403).
    place = drops.locate(geocoder, body.pickup_address)
    fields = body.model_dump(exclude={"pickup_address", "delivery_radius_miles"})
    drop = drops.create_drop(
        session,
        seller_id=shop.id,
        pickup_address=body.pickup_address,
        pickup_area=place.area,
        pickup_lat=place.lat,
        pickup_lng=place.lng,
        delivery_radius_km=None if body.delivery_radius_miles is None else body.delivery_radius_miles * geo.KM_PER_MILE,
        **fields,
    )
    return drops.drop_summary(session, drop)


geocode_limiter = RateLimiter(limit=30, window=600)  # address checks per seller per 10 minutes


@app.post("/geo/check")
def check_address(body: GeocodeIn, user: User = Depends(require_user), geocoder=Depends(get_geocoder)):
    """Lets a seller confirm that an address was understood, before they post a drop with it."""
    if not geocode_limiter.allow(user.id):
        raise HTTPException(429, "That's a lot of address checks. Please wait a few minutes.")
    place = drops.locate(geocoder, body.query)
    return {"area": place.area, "label": place.label}


@app.get("/me/drops")
def my_drops(shop: Seller = Depends(require_shop), session: Session = Depends(get_session)):
    """The signed-in seller's drops, newest first, with progress toward each minimum."""
    rows = session.scalars(
        select(Drop).where(Drop.seller_id == shop.id).order_by(Drop.id.desc()).limit(50)
    ).all()
    return {"drops": [drops.drop_progress(session, d) for d in rows]}


@app.get("/me/analytics")
def my_analytics(shop: Seller = Depends(require_shop), session: Session = Depends(get_session)):
    """
    The seller's drops and orders as flat rows for the dashboard. Buyers' names and emails are left
    out on purpose: a seller sees what sold, not who bought. Measures are precomputed per row so
    the dashboard can simply sum them.
    """
    drop_rows = session.scalars(select(Drop).where(Drop.seller_id == shop.id).order_by(Drop.id)).all()
    drops_out, orders_out = [], []
    for d in drop_rows:
        p = drops.drop_progress(session, d)
        u = p["units_by_order_status"]
        approved = p["paid_up_units"]
        drops_out.append({
            "drop_id": d.id,
            "item_name": d.item_name,
            "status": d.status.value,
            "is_open": 1 if d.status.value == "open" else 0,
            "unit_price": float(d.unit_price),
            "quantity_total": d.quantity_total,
            "minimum_units": d.minimum_units,
            "units_approved": approved,
            "units_reserved": u["reserved"],
            "fill_percent": round(100 * approved / d.minimum_units),
            "deadline": d.deadline.isoformat(),
            "created_at": d.created_at.isoformat(),
        })
        for o in session.scalars(select(Order).where(Order.drop_id == d.id).order_by(Order.id)):
            status = o.status.value
            approved_order = status in ("authorized", "captured")
            orders_out.append({
                "order_id": o.id,
                "drop_id": d.id,
                "item_name": d.item_name,
                "status": status,
                "quantity": o.quantity,
                "amount": float(o.amount),
                "created_at": o.created_at.isoformat(),
                "units_approved": o.quantity if approved_order else 0,
                "value_approved": float(o.amount) if approved_order else 0.0,
                "amount_on_hold": float(o.amount) if status == "authorized" else 0.0,
                "amount_collected": float(o.amount) if status == "captured" else 0.0,
            })
    return {"drops": drops_out, "orders": orders_out}


@app.get("/me/payouts")
def my_payouts(
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """What the platform has paid (or will pay) this shop through PayPal Payouts, and the fee taken."""
    rows = session.execute(
        select(Payout, Drop.item_name).join(Drop, Drop.id == Payout.drop_id)
        .where(Payout.seller_id == shop.id).order_by(Payout.id.desc())
    ).all()
    # The point of a drop: when one misses its minimum, nothing is made or charged. Count what that saved.
    saved_units, released = session.execute(
        select(func.coalesce(func.sum(Order.quantity), 0), func.coalesce(func.sum(Order.amount), 0))
        .join(Drop, Drop.id == Order.drop_id)
        .where(Drop.seller_id == shop.id, Order.status == OrderStatus.VOIDED)
    ).one()
    return {
        "waste_avoided_units": int(saved_units),
        "holds_released": str(released),
        "fee_percent": settings.platform_fee_percent,
        "payouts": [{
            "drop_id": p.drop_id, "item_name": name, "gross": str(p.gross), "fee": str(p.fee), "net": str(p.net),
            "currency": p.currency, "status": p.status, "detail": p.detail, "batch_id": p.batch_id,
            "updated_at": p.updated_at.isoformat(),
        } for p, name in rows],
    }


@app.get("/me/plan")
def my_plan(
    drop_id: int | None = None,
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """How the seller's finished drops went, and a suggestion for the next one."""
    reports = planner.settled_reports(session, shop.id, settings.timezone)
    if drop_id is not None:
        match = [r for r in reports if r["drop_id"] == drop_id]
        if not match:
            raise drops.DropNotFound("That drop isn't finished yet, or isn't one of yours.")
        reports = match + [r for r in reports if r["drop_id"] != drop_id]  # asked-about drop leads
    return {"recommendation": planner.recommend(reports, settings.timezone), "reports": reports}


@app.get("/me/drops/{drop_id}/orders")
def my_drop_orders(drop_id: int, shop: Seller = Depends(require_shop), session: Session = Depends(get_session)):
    """
    The orders on one of the seller's drops that they now need to fulfil. Buyers appear by first name
    and last initial, and a delivery address is included only for orders whose hold is approved.
    """
    drop = my_drop(session, shop, drop_id)
    rows = session.scalars(select(Order).where(Order.drop_id == drop.id).order_by(Order.id)).all()

    def display(name: str) -> str:
        parts = name.split()
        return parts[0] if len(parts) < 2 else f"{parts[0]} {parts[-1][0]}."

    return {
        "orders": [
            {
                "order_id": o.id,
                "buyer": display(o.buyer_name),
                "quantity": o.quantity,
                "status": o.status.value,
                "fulfillment": o.fulfillment,
                "delivery_address": o.delivery_address if o.status.value in APPROVED else None,
                "amount": str(o.amount),
            }
            for o in rows
            if o.status.value in APPROVED  # approved orders to fulfil; not abandoned reservations or released holds
        ]
    }


@app.post("/drops/{drop_id}/cancel")
def cancel_drop(
    drop_id: int,
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
):
    my_drop(session, shop, drop_id)  # only the owner can cancel
    status = drops.cancel_drop(session, paypal, drop_id)
    return {"id": drop_id, "status": status.value}


@app.post("/drops/{drop_id}/settle-now")
def settle_now(
    drop_id: int,
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    """
    Demo only (DEMO_MODE=1): move the deadline to now and settle, so a live demo shows the capture or
    release and the payout without waiting days. It runs the exact same settlement as the timer.
    """
    if not settings.demo_mode:
        raise HTTPException(404, "Not found.")
    drop = my_drop(session, shop, drop_id)
    if drop.status.value != "open":
        raise drops.DropNotOpen("This drop is already closed.")
    drop.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.commit()
    status = drops.settle_drop(session, paypal, drop_id)
    if status.value == "filled":
        payouts.pay_drop(session, paypal, drop_id, settings.platform_fee_percent)  # pay the seller right away
    return {"id": drop_id, "status": status.value}


# ---- buyer routes ----------------------------------------------------------

@app.post("/drops/{drop_id}/orders", status_code=201)
def place_order(
    drop_id: int,
    body: OrderIn,
    user: User = Depends(require_verified_email),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
    geocoder=Depends(get_geocoder),
):
    """Reserve stock for the signed-in buyer, then start PayPal checkout. They approve at approval_url."""
    delivery = {}
    if body.fulfillment == "delivery":
        if not (body.delivery_address and body.delivery_address.strip()):
            raise drops.InvalidOrder("Add the address to deliver to.")
        place = drops.locate(geocoder, body.delivery_address)
        delivery = {"delivery_address": body.delivery_address, "delivery_lat": place.lat, "delivery_lng": place.lng}
    try:
        order, approval_url = drops.place_order(
            session,
            paypal,
            drop_id,
            user.name,
            user.email,
            buyer_chat_id(user),
            body.quantity,
            return_url=f"{settings.public_api_url}/paypal/return",
            cancel_url=f"{settings.public_api_url}/paypal/cancel",
            buyer_user_id=user.id,
            fulfillment=body.fulfillment,
            **delivery,
        )
    except PayPalError:
        raise HTTPException(502, "Could not start PayPal checkout. Please try again.")
    return {
        "order_id": order.id,
        "paypal_order_id": order.paypal_order_id,  # what PayPal's in-page buttons need
        "approval_url": approval_url,
        "amount": str(order.amount),
        "reserved_until": order.reserved_until.isoformat(),
    }


def _own_order(session: Session, user: User, order_id: int) -> Order:
    order = session.get(Order, order_id)
    if order is None or order.buyer_user_id != user.id:  # other people's orders look like missing ones
        raise drops.OrderNotFound(f"No order with id {order_id}.")
    return order


@app.post("/orders/{order_id}/pay")
def pay_order(
    order_id: int,
    user: User = Depends(require_verified_email),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    """
    Pick up an unfinished checkout: the PayPal approval link for a reservation that is still held.
    Asking twice returns the same PayPal order, because the request id is fixed.
    """
    order = _own_order(session, user, order_id)
    try:
        link = drops.start_checkout(
            session, paypal, order.id,
            return_url=f"{settings.public_api_url}/paypal/return",
            cancel_url=f"{settings.public_api_url}/paypal/cancel",
        )
    except PayPalError:
        raise HTTPException(502, "Could not reach PayPal. Please try again.")
    return {"approval_url": link}


@app.post("/orders/{order_id}/confirm")
def confirm_order(
    order_id: int,
    user: User = Depends(require_verified_email),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
):
    """
    The buyer approved inside PayPal's in-page buttons: place the hold now. This does the same
    thing the redirect return and the webhook do, and it is safe to run after either of them.
    """
    order = _own_order(session, user, order_id)
    if not order.paypal_order_id:
        raise drops.OrderNotPayable("This order has no PayPal checkout yet.")
    try:
        drops.confirm_authorization(session, paypal, order.paypal_order_id)
    except PayPalError:
        raise HTTPException(502, "PayPal could not place the hold. Nothing was charged. Please try again.")
    session.expire_all()
    return order_view(session, session.get(Order, order_id))


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, user: User = Depends(require_user), session: Session = Depends(get_session)):
    """Give up a reservation that has not been paid for yet. Approved orders are a commitment and stay."""
    order = _own_order(session, user, order_id)
    if order.status != OrderStatus.RESERVED:
        raise drops.OrderNotPayable("Only a reservation you haven't paid for yet can be cancelled.")
    drops.release_reservation(session, order.id, reason="buyer_cancelled")
    session.refresh(order)
    return order_view(session, order)


@app.get("/me/orders")
def my_orders(user: User = Depends(require_user), session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Order).where(Order.buyer_user_id == user.id).order_by(Order.id.desc()).limit(20)
    ).all()
    return {"orders": [order_view(session, o) for o in rows]}


@app.get("/orders/{order_id}")
def get_order(order_id: int, user: User = Depends(require_user), session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if order is None or order.buyer_user_id != user.id:  # other people's orders look like missing ones
        raise drops.OrderNotFound(f"No order with id {order_id}.")
    return order_view(session, order)


# ---- PayPal routes ---------------------------------------------------------

@app.get("/paypal/return")
def paypal_return(
    token: str,  # PayPal puts its own order id in `token`
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    """The buyer approved on PayPal and was sent back here: place the hold, then go to the app."""
    order = session.scalar(select(Order).where(Order.paypal_order_id == token))
    if order is None:
        return RedirectResponse(frontend_order_url(settings, None, "error"), status_code=303)
    order_id = order.id
    try:
        drops.confirm_authorization(session, paypal, token)
        status = "authorized"
    except (drops.OrderNotPayable, drops.DropNotOpen):
        status = "expired"
    except PayPalError:
        status = "error"
    return RedirectResponse(frontend_order_url(settings, order_id, status), status_code=303)


@app.get("/paypal/cancel")
def paypal_cancel(
    token: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """The buyer backed out on PayPal: free their stock straight away."""
    order = session.scalar(select(Order).where(Order.paypal_order_id == token))
    if order is None:
        return RedirectResponse(frontend_order_url(settings, None, "error"), status_code=303)
    order_id = order.id
    drops.release_reservation(session, order_id, reason="buyer_cancelled")
    return RedirectResponse(frontend_order_url(settings, order_id, "cancelled"), status_code=303)


def _handle_webhook(session: Session, paypal, settings: Settings, headers, raw_body: bytes) -> dict:
    if not settings.paypal_webhook_id:
        raise HTTPException(503, "Webhooks are not configured (PAYPAL_WEBHOOK_ID).")
    try:
        event = json.loads(raw_body)
    except ValueError:
        raise HTTPException(400, "Body is not JSON.")
    if not paypal.verify_webhook_signature(
        headers=headers, event=event, webhook_id=settings.paypal_webhook_id
    ):
        raise HTTPException(400, "Webhook signature check failed.")

    # We only act on "buyer approved". Everything else is acknowledged and ignored.
    if event.get("event_type") == "CHECKOUT.ORDER.APPROVED":
        try:
            drops.confirm_authorization(session, paypal, event["resource"]["id"])
            return {"handled": True}
        except (drops.OrderNotFound, drops.OrderNotPayable, drops.DropNotOpen):
            # Nothing more to do. Answer 200 so PayPal does not retry forever.
            return {"handled": False}
    kind = event.get("event_type", "")
    resource = event.get("resource") or {}
    if kind in payouts.EVENT_STATUS:
        batch = payouts.batch_id_of(resource)
        detail = (resource.get("transaction_status") or kind)[:300]
        return {"handled": bool(batch) and payouts.apply_batch_event(session, batch, payouts.EVENT_STATUS[kind], detail)}
    if kind in drops.MONEY_EVENTS or kind in drops.AUTH_EVENTS:
        return {"handled": drops.record_money_event(session, kind, resource)}
    return {"handled": False}


@app.post("/paypal/webhook")
async def paypal_webhook(
    request: Request,
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    """
    Backup path for the return page (a buyer may approve and then close the tab).
    PayPalError is left to bubble up as a 500, so PayPal retries the delivery later.
    """
    raw_body = await request.body()
    return await run_in_threadpool(
        _handle_webhook, session, paypal, settings, request.headers, raw_body
    )


# ---- chat ------------------------------------------------------------------

@app.post("/chat/seller")
def chat_seller(
    body: ChatIn,
    user: User = Depends(require_user),
    shop: Seller = Depends(require_shop),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    llm=Depends(get_llm),
    settings: Settings = Depends(get_settings),
    geocoder=Depends(get_geocoder),
):
    reply = agent.run_turn(
        session, llm, paypal, settings, "seller", f"shop-{shop.id}", body.message,
        seller_id=shop.id, user=user, geocoder=geocoder,
    )
    return {"reply": reply}


@app.post("/chat/buyer")
def chat_buyer(
    body: ChatIn,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    llm=Depends(get_llm),
    settings: Settings = Depends(get_settings),
    geocoder=Depends(get_geocoder),
):
    reply = agent.run_turn(
        session, llm, paypal, settings, "buyer", buyer_chat_id(user), body.message, user=user, geocoder=geocoder
    )
    return {"reply": reply}


@app.get("/chat/{role}/history")
def chat_history(
    role: str,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """What the chat window shows after a reload: only the visible user and assistant text."""
    if role == "buyer":
        session_id = buyer_chat_id(user)
    elif role == "seller":
        shop = auth.shop_of(session, user)
        if shop is None:
            return {"messages": []}
        session_id = f"shop-{shop.id}"
    else:
        raise HTTPException(404, "Unknown chat role.")
    rows = session.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == agent.conversation_id(role, session_id),
            ChatMessage.role.in_(("user", "assistant")),
            ChatMessage.text.is_not(None),
        )
        .order_by(ChatMessage.id)
    ).all()
    return {"messages": [{"role": r.role, "text": r.text} for r in rows]}
