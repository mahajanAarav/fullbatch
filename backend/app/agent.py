"""
The chat agent: one conversation turn at a time.

A turn sends the history and the role's tools to the model. If the model asks
for a tool we run it and feed the result back, repeating until the model
answers in plain text.

Safety is enforced here in code, not left to the prompt:
  - Each role only has its own tools. A tool not on the list cannot run,
    even if the model asks for it.
  - The server supplies the seller id and chat session id. The model never chooses them.
  - Sellers can only see and cancel their own drops.
  - Cancelling needs an explicit confirm=true.
  - No tool can capture, void or refund a payment. That stays in paypal.py
    and the deadline job.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import drops, geo, planner
from app.config import Settings
from app.llm import LLM
from app.models import ChatMessage, Drop, DropStatus, Order, OrderStatus, User, conversation_id
from app.paypal import PayPalError

log = logging.getLogger(__name__)

MAX_STEPS = 6        # model/tool round trips per turn, so a confused model cannot loop forever
HISTORY_LIMIT = 30   # most recent stored messages sent to the model
FALLBACK_REPLY = "Sorry, I got stuck on that. Could you rephrase or try again?"


@dataclass
class ToolContext:
    session: Session
    paypal: Any
    settings: Settings
    chat_session_id: str
    seller_id: int | None = None  # set for seller conversations only
    user: User | None = None  # the signed-in person, set by the server (never by the model)
    geocoder: Any = None  # looks up addresses; set by the server


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict]

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _clean_schema(self.args.model_json_schema()),
        }


def _clean_schema(node):
    """Drop the cosmetic 'title' and 'default' keys that model providers do not need."""
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k not in ("title", "default")}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


# ---- shared helpers --------------------------------------------------------

def _own_drop(ctx: ToolContext, drop_id: int) -> Drop:
    """A seller's drop. Someone else's drop looks exactly like a missing one."""
    drop = ctx.session.get(Drop, drop_id)
    if drop is None or drop.seller_id != ctx.seller_id:
        raise drops.DropNotFound(f"You have no drop with id {drop_id}.")
    return drop


def _parse_deadline(text: str, tz_name: str) -> datetime:
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise drops.InvalidDrop("The deadline must look like 2026-10-10T17:00 (date and time).")
    if when.tzinfo is None:  # a seller typing "5pm Saturday" means their own timezone
        when = when.replace(tzinfo=ZoneInfo(tz_name))
    return when


# ---- seller tools ----------------------------------------------------------

class CreateDropArgs(BaseModel):
    item_name: str = Field(description="What is being sold, e.g. 'Sourdough loaf'")
    unit_price: float = Field(gt=0, description="Price per unit in US dollars")
    quantity_total: int = Field(gt=0, description="Total units available")
    minimum_units: int = Field(gt=0, description="Units that must be ordered or the drop is cancelled and nobody is charged")
    deadline: str = Field(description="When ordering closes, ISO 8601 like 2026-10-10T17:00. Times without a timezone are in the seller's timezone.")
    max_per_buyer: int = Field(default=4, gt=0, description="Most units one buyer can order")
    pickup_address: str = Field(description="The street address buyers collect from (and deliveries start from), with city and ZIP")
    pickup_notes: str = Field(default="", description="Optional note for buyers after they approve, e.g. 'Ring the side bell'")
    offers_delivery: bool = Field(default=False, description="True if the seller also delivers")
    delivery_radius_miles: float = Field(default=0, ge=0, description="How far the seller delivers, in miles (needed if offers_delivery)")
    delivery_fee: float = Field(default=0, ge=0, description="Flat delivery fee in US dollars (0 for free delivery)")


def create_drop(ctx: ToolContext, a: CreateDropArgs) -> dict:
    place = drops.locate(ctx.geocoder, a.pickup_address)
    drop = drops.create_drop(
        ctx.session,
        seller_id=ctx.seller_id,
        item_name=a.item_name,
        unit_price=Decimal(str(a.unit_price)).quantize(Decimal("0.01")),
        quantity_total=a.quantity_total,
        minimum_units=a.minimum_units,
        deadline=_parse_deadline(a.deadline, ctx.settings.timezone),
        max_per_buyer=a.max_per_buyer,
        offers_pickup=True,
        offers_delivery=a.offers_delivery,
        pickup_address=a.pickup_address,
        pickup_area=place.area,
        pickup_notes=a.pickup_notes or None,
        pickup_lat=place.lat,
        pickup_lng=place.lng,
        delivery_radius_km=(a.delivery_radius_miles * geo.KM_PER_MILE) if a.offers_delivery else None,
        delivery_fee=Decimal(str(a.delivery_fee)).quantize(Decimal("0.01")),
    )
    return drops.drop_summary(ctx.session, drop)


class NoArgs(BaseModel):
    pass


def list_my_drops(ctx: ToolContext, a: NoArgs) -> dict:
    rows = ctx.session.scalars(
        select(Drop).where(Drop.seller_id == ctx.seller_id).order_by(Drop.id.desc()).limit(10)
    ).all()
    return {"drops": [drops.drop_summary(ctx.session, d) for d in rows]}


class DropIdArgs(BaseModel):
    drop_id: int


def get_drop_status(ctx: ToolContext, a: DropIdArgs) -> dict:
    return drops.drop_progress(ctx.session, _own_drop(ctx, a.drop_id))


class PlanArgs(BaseModel):
    drop_id: int = Field(default=0, description="A finished drop to focus on, or 0 for the most recent finished one")


def plan_next_drop(ctx: ToolContext, a: PlanArgs) -> dict:
    reports = planner.settled_reports(ctx.session, ctx.seller_id, ctx.settings.timezone)
    if a.drop_id:
        match = [r for r in reports if r["drop_id"] == a.drop_id]
        if not match:
            return {"error": "That drop isn't finished yet, or isn't one of yours."}
        reports = match + [r for r in reports if r["drop_id"] != a.drop_id]
    return {
        "recommendation": planner.recommend(reports, ctx.settings.timezone),
        "last_finished_drop": reports[0] if reports else None,
        "note": (
            "These numbers were computed from this seller's own finished drops. Explain them plainly and "
            "do not change them. Only create a drop if the seller confirms the details."
        ),
    }


class CancelDropArgs(BaseModel):
    drop_id: int
    confirm: bool = Field(description="Must be true. Only set it after the seller has clearly agreed to cancel.")


def cancel_drop(ctx: ToolContext, a: CancelDropArgs) -> dict:
    drop = _own_drop(ctx, a.drop_id)
    if not a.confirm:
        return {"error": "Not cancelled. Ask the seller to confirm, then call again with confirm=true."}
    status = drops.cancel_drop(ctx.session, ctx.paypal, drop.id)
    return {"drop_id": drop.id, "status": status.value, "note": "All payment holds were released. Nobody was charged."}


# ---- buyer tools -----------------------------------------------------------

def list_open_drops(ctx: ToolContext, a: NoArgs) -> dict:
    return {"drops": [drops.drop_summary(ctx.session, d) for d in drops.list_open_drops(ctx.session, limit=10)]}


def check_stock(ctx: ToolContext, a: DropIdArgs) -> dict:
    drop = ctx.session.get(Drop, a.drop_id)
    if drop is None:
        raise drops.DropNotFound(f"No drop with id {a.drop_id}.")
    return drops.drop_summary(ctx.session, drop)


class PlaceOrderArgs(BaseModel):
    drop_id: int
    quantity: int = Field(gt=0)
    fulfillment: str = Field(default="pickup", description="'pickup' or 'delivery'. Delivery only if the drop offers it.")
    delivery_address: str = Field(default="", description="The address to deliver to, with city and ZIP. Needed for delivery.")


def place_order(ctx: ToolContext, a: PlaceOrderArgs) -> dict:
    if not ctx.user.email_verified:
        return {"error": "The buyer needs to verify their email first. They can do that from the Reserve button or the banner on the site."}
    delivery = {}
    if a.fulfillment == "delivery":
        if not a.delivery_address.strip():
            return {"error": "Ask the buyer for the address to deliver to."}
        place = drops.locate(ctx.geocoder, a.delivery_address)
        delivery = {"delivery_address": a.delivery_address, "delivery_lat": place.lat, "delivery_lng": place.lng}
    # Who is buying comes from the signed-in account, never from anything the model supplies.
    order, link = drops.place_order(
        ctx.session,
        ctx.paypal,
        a.drop_id,
        ctx.user.name,
        ctx.user.email,
        ctx.chat_session_id,
        a.quantity,
        return_url=f"{ctx.settings.public_api_url}/paypal/return",
        cancel_url=f"{ctx.settings.public_api_url}/paypal/cancel",
        buyer_user_id=ctx.user.id,
        fulfillment=a.fulfillment,
        **delivery,
    )
    return {
        "order_id": order.id,
        "amount": str(order.amount),
        "approval_url": link,
        "reserved_until": order.reserved_until.isoformat(),
        "fulfillment": order.fulfillment,
        "note": "Stock is reserved. The buyer must open approval_url to approve. PayPal only holds the amount; they are charged only if the drop reaches its minimum.",
    }


def my_orders(ctx: ToolContext, a: NoArgs) -> dict:
    rows = ctx.session.scalars(
        select(Order)
        .where(Order.chat_session_id == ctx.chat_session_id)
        .order_by(Order.id.desc())
        .limit(10)
    ).all()
    return {
        "orders": [
            {
                "order_id": o.id,
                "drop_id": o.drop_id,
                "item_name": o.drop.item_name,
                "quantity": o.quantity,
                "amount": str(o.amount),
                "status": o.status.value,
            }
            for o in rows
        ]
    }


SELLER_TOOLS = [
    Tool("create_drop", "Create a new preorder drop for this seller.", CreateDropArgs, create_drop),
    Tool("list_my_drops", "List this seller's most recent drops with live stock numbers.", NoArgs, list_my_drops),
    Tool("get_drop_status", "Detailed progress of one of this seller's drops, including whether the minimum is met so far.", DropIdArgs, get_drop_status),
    Tool("plan_next_drop", "Review how the seller's finished drops went and recommend quantity, minimum, price and timing for the next one. Read-only: it never creates a drop.", PlanArgs, plan_next_drop),
    Tool("cancel_drop", "Cancel an open drop and release every payment hold. Needs the seller's explicit confirmation.", CancelDropArgs, cancel_drop),
]

BUYER_TOOLS = [
    Tool("list_open_drops", "List drops that are currently taking orders.", NoArgs, list_open_drops),
    Tool("check_stock", "Current price, stock and deadline of one drop.", DropIdArgs, check_stock),
    Tool("place_order", "Reserve units for the buyer and get their PayPal approval link.", PlaceOrderArgs, place_order),
    Tool("my_orders", "This buyer's own orders and their statuses.", NoArgs, my_orders),
]

TOOLS_BY_ROLE = {"seller": SELLER_TOOLS, "buyer": BUYER_TOOLS}


# ---- prompts ---------------------------------------------------------------

def system_prompt(role: str, settings: Settings) -> str:
    now = datetime.now(ZoneInfo(settings.timezone)).strftime("%A %Y-%m-%d %H:%M %Z")
    how_it_works = (
        "How fullbatch works: a seller opens a limited 'drop' with a price, quantity, a minimum "
        "number of units and a deadline. Buyers' payments are only HELD on PayPal, not charged. "
        "At the deadline, if the minimum was reached every hold is charged; if not, every hold is "
        "released and nobody pays anything. A buyer's stock reservation lasts only 15 minutes, which is the "
        "window to approve on PayPal; once approved, the hold lasts until the deadline."
    )
    rules = (
        "Rules: use tools for every fact about drops, stock, prices and orders; never guess or invent "
        "them. You cannot charge, refund or release payments yourself, and must never claim to. "
        "Only share links that a tool returned. Keep replies short and friendly, in plain language."
    )
    if role == "seller":
        return (
            f"You are the fullbatch assistant helping a small seller (home baker, market vendor) run preorder drops. "
            f"Now: {now}.\n{how_it_works}\n"
            "To create a drop you need: item, price, quantity, minimum units, deadline, and the pickup address (where buyers "
            "collect it). Also ask whether the seller delivers, and if so how far (in miles) and the fee. Ask for anything missing, "
            "then confirm the details back before creating it. When the seller asks how a drop went, or what to run "
            "next, call plan_next_drop, explain the result and the reasons in plain language, say how confident it is, and "
            "offer to create the drop. Never invent figures that the tool did not return, and never create the drop until "
            "the seller agrees. Each buyer may order at most 4 units unless the "
            "seller asks for a different limit. Before cancelling a drop, ask the seller to confirm, "
            "and only then call cancel_drop with confirm=true.\n" + rules
        )
    return (
        f"You are the fullbatch assistant helping a buyer order from preorder drops. Now: {now}.\n{how_it_works}\n"
        "Help the buyer pick a drop and quantity (they are already signed in, so never ask for their name or "
        "email), ask whether they want pickup or delivery when the drop offers both (for delivery, ask for their address "
        "and mention the fee), confirm the order and total, then call place_order and give them the approval link. Explain that approving only places a hold and "
        "that they pay only if the drop reaches its minimum. They have 15 minutes to approve before the "
        "reservation is released.\n" + rules
    )


# ---- history ---------------------------------------------------------------

def _to_message(row: ChatMessage) -> dict:
    if row.role == "assistant":
        return {"role": "assistant", "text": row.text, "tool_calls": row.tool_calls or []}
    if row.role == "tool":
        return {"role": "tool", "call_id": row.call_id, "name": row.tool_name, "result": row.text}
    return {"role": "user", "text": row.text}


def _repair(messages: list[dict]) -> list[dict]:
    """
    Skip any tool request that has no matching results (e.g. the server crashed
    mid-turn), along with the partial results it left behind. A model API rejects
    history containing an unanswered tool call. Everything else is kept.
    """
    fixed: list[dict] = []
    skip_results_for: set[str] = set()
    for i, m in enumerate(messages):
        if m["role"] == "tool" and m.get("call_id") in skip_results_for:
            continue
        if m["role"] == "assistant" and m["tool_calls"]:
            wanted = {c["id"] for c in m["tool_calls"]}
            following = messages[i + 1 : i + 1 + len(wanted)]
            if {f.get("call_id") for f in following if f["role"] == "tool"} != wanted:
                skip_results_for |= wanted
                continue
        fixed.append(m)
    return fixed


def load_history(session: Session, conv_id: str) -> list[dict]:
    rows = list(
        reversed(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_LIMIT)
            ).all()
        )
    )
    # The window may start mid-exchange. Begin at the first user message instead.
    while rows and rows[0].role != "user":
        rows.pop(0)
    return _repair([_to_message(r) for r in rows])


def _store(session: Session, conv_id: str, **fields) -> None:
    session.add(ChatMessage(conversation_id=conv_id, **fields))
    session.commit()


# ---- running tools ---------------------------------------------------------

def execute_tool(ctx: ToolContext, tools: dict[str, Tool], name: str, args: dict | None) -> dict:
    """Run one tool the model asked for. Every failure becomes a readable result, never a crash."""
    tool = tools.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'."}
    try:
        parsed = tool.args.model_validate(args or {})
    except ValidationError as err:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in err.errors())
        return {"error": f"Invalid arguments: {problems}"}
    try:
        return tool.handler(ctx, parsed)
    except drops.SoldOut as err:
        ctx.session.rollback()
        return {"error": str(err), "remaining": err.remaining}
    except drops.DropError as err:
        ctx.session.rollback()
        return {"error": str(err)}
    except PayPalError:
        ctx.session.rollback()
        log.exception("PayPal error inside tool %s", name)
        return {"error": "The payment system is unavailable right now. Please try again shortly."}
    except Exception:
        ctx.session.rollback()
        log.exception("Unexpected error inside tool %s", name)
        return {"error": "Something went wrong running that. Please try again."}


# ---- one conversation turn -------------------------------------------------

def run_turn(
    session: Session,
    llm: LLM,
    paypal,
    settings: Settings,
    role: str,
    session_id: str,
    user_text: str,
    seller_id: int | None = None,
    user: User | None = None,
    geocoder=None,
) -> str:
    """Handle one user message. Returns the assistant's reply text."""
    tools = {t.name: t for t in TOOLS_BY_ROLE[role]}
    specs = [t.spec() for t in tools.values()]
    ctx = ToolContext(session, paypal, settings, chat_session_id=session_id, seller_id=seller_id, user=user, geocoder=geocoder)
    conv_id = conversation_id(role, session_id)

    messages = load_history(session, conv_id)
    messages.append({"role": "user", "text": user_text})
    _store(session, conv_id, role="user", text=user_text)

    system = system_prompt(role, settings)
    for _ in range(MAX_STEPS):
        reply = llm.generate(system=system, messages=messages, tools=specs)  # may raise LLMError
        calls = reply.get("tool_calls") or []
        text = reply.get("text")

        # Save the assistant's message before running tools: tool code may roll back the session.
        _store(session, conv_id, role="assistant", text=text, tool_calls=calls or None)
        messages.append({"role": "assistant", "text": text, "tool_calls": calls})

        if not calls:
            if text:
                return text
            _store(session, conv_id, role="assistant", text=FALLBACK_REPLY)
            return FALLBACK_REPLY

        for call in calls:
            result = execute_tool(ctx, tools, call["name"], call.get("args"))
            result_text = json.dumps(result, default=str)
            _store(session, conv_id, role="tool", call_id=call["id"], tool_name=call["name"], text=result_text)
            messages.append({"role": "tool", "call_id": call["id"], "name": call["name"], "result": result_text})

    _store(session, conv_id, role="assistant", text=FALLBACK_REPLY)
    return FALLBACK_REPLY
