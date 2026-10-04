"""
fullbatch HTTP API.

Seller side:  POST /sellers, POST /drops, GET /drops/{id}, POST /drops/{id}/cancel
Buyer side:   POST /drops/{id}/orders, GET /orders/{id}
PayPal:       GET /paypal/return, GET /paypal/cancel, POST /paypal/webhook

Known gap: there is no seller login yet, so the seller routes are open.
"""

import json
from decimal import Decimal
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import drops
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Drop, Order, Seller
from app.paypal import PayPalError, from_env

app = FastAPI(title="fullbatch")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().frontend_url],
    allow_methods=["*"],
    allow_headers=["*"],
)


@lru_cache
def get_paypal():
    """One PayPal client for the whole process, so the access token is reused."""
    return from_env()


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
    (drops.SoldOut, 409),
    (drops.DropNotOpen, 409),
    (drops.OrderNotPayable, 409),
    (drops.DropNotDue, 409),
    (drops.InvalidDrop, 422),
    (drops.InvalidOrder, 422),
]:
    app.add_exception_handler(_exc, _error(_status))


# ---- request bodies --------------------------------------------------------

class SellerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DropIn(BaseModel):
    seller_id: int
    item_name: str = Field(min_length=1, max_length=200)
    unit_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    quantity_total: int = Field(gt=0)
    minimum_units: int = Field(gt=0)
    deadline: AwareDatetime  # must include a timezone, so "5pm" is never ambiguous
    max_per_buyer: int = Field(default=4, gt=0)


class OrderIn(BaseModel):
    buyer_name: str = Field(min_length=1, max_length=120)
    buyer_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    chat_session_id: str = Field(min_length=1, max_length=64)
    quantity: int


# ---- helpers ---------------------------------------------------------------

def drop_summary(session: Session, drop: Drop) -> dict:
    taken = drops.units_taken(session, drop.id)
    return {
        "id": drop.id,
        "seller_id": drop.seller_id,
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


def frontend_order_url(settings: Settings, order_id: int | None, status: str) -> str:
    path = f"/orders/{order_id}" if order_id is not None else "/orders"
    return f"{settings.frontend_url}{path}?status={status}"


# ---- seller routes ---------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/sellers", status_code=201)
def create_seller(body: SellerIn, session: Session = Depends(get_session)):
    seller = Seller(name=body.name.strip())
    session.add(seller)
    session.commit()
    return {"id": seller.id, "name": seller.name}


@app.post("/drops", status_code=201)
def create_drop(body: DropIn, session: Session = Depends(get_session)):
    if session.get(Seller, body.seller_id) is None:
        raise HTTPException(404, "No seller with that id.")
    drop = drops.create_drop(session, **body.model_dump())
    return drop_summary(session, drop)


@app.get("/drops/{drop_id}")
def get_drop(drop_id: int, session: Session = Depends(get_session)):
    drop = session.get(Drop, drop_id)
    if drop is None:
        raise drops.DropNotFound(f"No drop with id {drop_id}.")
    return drop_summary(session, drop)


@app.post("/drops/{drop_id}/cancel")
def cancel_drop(drop_id: int, session: Session = Depends(get_session), paypal=Depends(get_paypal)):
    status = drops.cancel_drop(session, paypal, drop_id)
    return {"id": drop_id, "status": status.value}


# ---- buyer routes ----------------------------------------------------------

@app.post("/drops/{drop_id}/orders", status_code=201)
def place_order(
    drop_id: int,
    body: OrderIn,
    session: Session = Depends(get_session),
    paypal=Depends(get_paypal),
    settings: Settings = Depends(get_settings),
):
    """Reserve stock, then start PayPal checkout. The buyer approves at approval_url."""
    order = drops.reserve_stock(
        session, drop_id, body.buyer_name, body.buyer_email, body.chat_session_id, body.quantity
    )
    try:
        approval_url = drops.start_checkout(
            session,
            paypal,
            order.id,
            return_url=f"{settings.public_api_url}/paypal/return",
            cancel_url=f"{settings.public_api_url}/paypal/cancel",
        )
    except PayPalError:
        # Do not leave the stock locked up for 15 minutes because PayPal failed.
        session.rollback()
        drops.release_reservation(session, order.id, reason="checkout_failed")
        raise HTTPException(502, "Could not start PayPal checkout. Please try again.")
    return {
        "order_id": order.id,
        "approval_url": approval_url,
        "amount": str(order.amount),
        "reserved_until": order.reserved_until.isoformat(),
    }


@app.get("/orders/{order_id}")
def get_order(order_id: int, session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if order is None:
        raise drops.OrderNotFound(f"No order with id {order_id}.")
    drop = session.get(Drop, order.drop_id)
    return {
        "id": order.id,
        "status": order.status.value,
        "quantity": order.quantity,
        "amount": str(order.amount),
        "drop_id": drop.id,
        "item_name": drop.item_name,
        "drop_status": drop.status.value,
    }


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
