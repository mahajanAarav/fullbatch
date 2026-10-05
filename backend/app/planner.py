"""
The drop planner: how did a drop go, and what should the next one look like?

The numbers come from code, not from a language model. A model is good at explaining a
recommendation, and bad at being trusted with arithmetic, so the assistant gets these results
from a tool and only puts them into words.

Rules, in short:
  - Sold out (90%+ of stock) and hit the minimum: raise quantity ~25%, and nudge the price up
    if the minimum was reached early.
  - Filled but stock left: keep or trim quantity toward real demand.
  - Missed the minimum: lower the minimum toward real demand, and run longer.
  - Timing: window length from how fast demand arrived; closing time from when orders came in.
It says so plainly when it has little history, and never claims more confidence than it has.
"""

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Drop, DropStatus, Order, OrderStatus

# An order that had a PayPal hold at some point. Reserved/expired orders never did: that is not demand.
COMMITTED = (OrderStatus.AUTHORIZED, OrderStatus.CAPTURED, OrderStatus.VOIDED, OrderStatus.FAILED)
SETTLED = (DropStatus.FILLED, DropStatus.FAILED)

STARTER = {"quantity_total": 12, "minimum_units": 6, "duration_days": 3, "max_per_buyer": 4}


def _round_price(value: float) -> Decimal:
    """Prices move in quarter-dollar steps, because $9.37 looks like a mistake."""
    return (Decimal(str(round(value * 4))) / 4).quantize(Decimal("0.01"))


def _round_half_up(value: float) -> int:
    """4.5 days is 5 days. (Python's round() sends halves to the nearest even number, which gives 4.)"""
    return math.floor(value + 0.5)


def _local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def drop_report(session: Session, drop: Drop, tz_name: str = "America/New_York") -> dict:
    """What happened in one drop, as plain numbers."""
    tz = ZoneInfo(tz_name)
    orders = session.scalars(
        select(Order).where(Order.drop_id == drop.id, Order.status.in_(COMMITTED)).order_by(Order.created_at, Order.id)
    ).all()

    committed_units = sum(o.quantity for o in orders)
    window_hours = max((drop.deadline - drop.created_at).total_seconds() / 3600, 1.0)

    # When did cumulative demand first reach the minimum?
    hours_to_minimum = None
    running = 0
    for o in orders:
        running += o.quantity
        if running >= drop.minimum_units:
            hours_to_minimum = max((o.created_at - drop.created_at).total_seconds() / 3600, 0.0)
            break

    last_day_units = sum(o.quantity for o in orders if o.created_at >= drop.deadline - timedelta(hours=24))
    collected = sum((o.amount for o in orders if o.status == OrderStatus.CAPTURED), Decimal("0"))
    missed = sum((o.amount for o in orders if o.status == OrderStatus.VOIDED), Decimal("0"))
    by_weekday = Counter(_local(o.created_at, tz).strftime("%A") for o in orders for _ in range(o.quantity))
    by_hour = Counter(_local(o.created_at, tz).hour for o in orders for _ in range(o.quantity))

    return {
        "drop_id": drop.id,
        "item_name": drop.item_name,
        "status": drop.status.value,
        "unit_price": float(drop.unit_price),
        "quantity_total": drop.quantity_total,
        "minimum_units": drop.minimum_units,
        "max_per_buyer": drop.max_per_buyer,
        "committed_units": committed_units,
        "committed_orders": len(orders),
        "average_order_size": round(committed_units / len(orders), 1) if orders else 0,
        "sell_through": round(committed_units / drop.quantity_total, 2),
        "minimum_ratio": round(committed_units / drop.minimum_units, 2),
        "window_hours": round(window_hours, 1),
        "hours_to_minimum": None if hours_to_minimum is None else round(hours_to_minimum, 1),
        "share_in_last_day": round(last_day_units / committed_units, 2) if committed_units else 0,
        "revenue_collected": float(collected),
        "revenue_not_collected": float(missed),
        "busiest_weekday": by_weekday.most_common(1)[0][0] if by_weekday else None,
        "busiest_hour": by_hour.most_common(1)[0][0] if by_hour else None,
        "closed_at": drop.deadline.isoformat(),
    }


def settled_reports(session: Session, seller_id: int, tz_name: str, limit: int = 5, drop_id: int | None = None) -> list[dict]:
    """The seller's most recent finished drops, newest first (or just the one asked about)."""
    query = select(Drop).where(Drop.seller_id == seller_id, Drop.status.in_(SETTLED))
    if drop_id is not None:
        query = query.where(Drop.id == drop_id)
    drops = session.scalars(query.order_by(Drop.deadline.desc()).limit(limit)).all()
    return [drop_report(session, d, tz_name) for d in drops]


def _hour_label(hour: int) -> str:
    return f"{hour % 12 or 12} {'am' if hour < 12 else 'pm'}"


def recommend(
    reports: list[dict], tz_name: str = "America/New_York", now: datetime | None = None
) -> dict:
    """Turn the seller's finished drops (newest first) into a suggestion for the next one."""
    now = now or datetime.now(timezone.utc)
    if not reports:
        return {
            "enough_data": False,
            "confidence": "none",
            "message": "No finished drops yet, so there is nothing to learn from. Here is a gentle starting point.",
            "recommended": {**STARTER, "unit_price": None, "item_name": None},
            "reasons": ["Start small: a low minimum means the drop is likely to run, and you learn what your buyers want."],
            "based_on": [],
        }

    last = reports[0]
    reasons: list[str] = []
    qty, minimum, price = last["quantity_total"], last["minimum_units"], last["unit_price"]
    window_days = last["window_hours"] / 24
    committed = last["committed_units"]

    if last["status"] == "filled":
        if last["sell_through"] >= 0.9:
            qty = math.ceil(last["quantity_total"] * 1.25)
            minimum = max(2, round(last["minimum_units"] * qty / last["quantity_total"]))
            reasons.append(
                f"“{last['item_name']}” sold {committed} of {last['quantity_total']} units, so demand outran supply. "
                f"A bigger batch of {qty} should capture the buyers you turned away."
            )
            fast = last["hours_to_minimum"] is not None and last["hours_to_minimum"] <= 0.4 * last["window_hours"]
            if fast:
                price = float(_round_price(price * 1.07))
                reasons.append(
                    f"It reached its minimum in about {last['hours_to_minimum']:.0f}h of a {last['window_hours']:.0f}h window, "
                    f"which is quick. A small price increase to ${price:.2f} is worth testing."
                )
        elif last["sell_through"] >= 0.6:
            reasons.append(
                f"It filled with {committed} of {last['quantity_total']} units committed ({round(100 * last['sell_through'])}%). "
                "The size looks about right, so keep it the same."
            )
        else:
            qty = max(minimum, math.ceil(committed * 1.2))
            reasons.append(
                f"It ran, but only {committed} of {last['quantity_total']} units were committed ({round(100 * last['sell_through'])}%). "
                f"A smaller batch of {qty} avoids leftovers."
            )
        if last["hours_to_minimum"] is not None:
            window_days = min(7.0, max(2.0, last["hours_to_minimum"] * 1.5 / 24))
    else:  # the minimum was not reached
        demand = committed
        if demand >= 0.5 * last["minimum_units"]:
            minimum = max(2, math.ceil(demand * 1.1))
            qty = max(minimum * 2, last["quantity_total"] if demand >= 0.8 * last["minimum_units"] else math.ceil(demand * 2))
            reasons.append(
                f"“{last['item_name']}” fell short: {demand} of the {last['minimum_units']} needed. "
                f"That is real interest, so lower the minimum to about {minimum} and keep the price."
            )
        else:
            minimum = max(2, math.ceil(max(demand, 1) * 1.5))
            qty = max(minimum * 2, 6)
            reasons.append(
                f"“{last['item_name']}” drew only {demand} of the {last['minimum_units']} needed. "
                f"Try a smaller test drop (minimum {minimum}) and promote it harder before changing the price."
            )
        window_days = min(14.0, max(3.0, window_days * 1.5))
        reasons.append(f"Run it longer, about {_round_half_up(window_days)} days, to give buyers more time to find it.")

    # Timing: when this shop's orders actually come in, across all the history we have.
    weekdays = Counter()
    hours = Counter()
    for r in reports:
        if r["busiest_weekday"]:
            weekdays[r["busiest_weekday"]] += r["committed_units"]
            hours[r["busiest_hour"]] += r["committed_units"]
    if last["share_in_last_day"] >= 0.4:
        reasons.append(
            f"{round(100 * last['share_in_last_day'])}% of orders arrived in the final 24 hours, so a reminder "
            "post on the last day is likely to help."
        )
    days = max(2, _round_half_up(window_days))
    close = _suggest_close(now, days, weekdays, hours, tz_name)
    if weekdays:
        day, _ = weekdays.most_common(1)[0]
        hour, _ = hours.most_common(1)[0]
        reasons.append(f"Most orders come in on {day}s around {_hour_label(hour)}. The suggested closing time follows that rhythm.")

    confidence = "high" if len(reports) >= 3 else "medium" if len(reports) == 2 else "low"
    caveat = {
        "low": "This is based on a single drop, so treat it as a starting point and not a forecast.",
        "medium": "Based on two drops. A third will make these suggestions sturdier.",
        "high": None,
    }[confidence]

    return {
        "enough_data": True,
        "confidence": confidence,
        "caveat": caveat,
        "recommended": {
            "item_name": last["item_name"],
            "unit_price": float(_round_price(price)),
            "quantity_total": qty,
            "minimum_units": min(minimum, qty),
            "max_per_buyer": last["max_per_buyer"],
            "duration_days": days,
            "deadline": close.isoformat(),
        },
        "reasons": reasons,
        "based_on": [r["drop_id"] for r in reports],
    }


def _suggest_close(now: datetime, days: int, weekdays: Counter, hours: Counter, tz_name: str) -> datetime:
    """About `days` from now, snapped to the evening of the weekday this shop's buyers order on."""
    tz = ZoneInfo(tz_name)
    target = _local(now, tz) + timedelta(days=days)
    target = target.replace(hour=18, minute=0, second=0, microsecond=0)
    if weekdays:
        names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        peak = names.index(weekdays.most_common(1)[0][0])
        # Move to the peak weekday within a day either side of the target, so the length stays about right.
        for shift in (0, -1, 1):
            candidate = target + timedelta(days=shift)
            if candidate.weekday() == peak and candidate > _local(now, tz) + timedelta(hours=24):
                return candidate
    return target
