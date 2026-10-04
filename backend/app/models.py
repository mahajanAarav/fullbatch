"""
Database tables for fullbatch.

Three main tables:
  Seller - who runs drops
  Drop   - one preorder drop (item, price, stock, minimum, deadline)
  Order  - one buyer's order inside a drop, tied to a PayPal hold

Counts such as "units reserved" are never stored. They are computed from
the orders table, so they cannot drift out of sync with the real orders.
"""

import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DropStatus(str, enum.Enum):
    OPEN = "open"          # taking orders
    FILLED = "filled"      # minimum met at the deadline, holds captured
    FAILED = "failed"      # minimum not met at the deadline, holds voided
    CANCELLED = "cancelled"  # seller cancelled, holds voided


class OrderStatus(str, enum.Enum):
    RESERVED = "reserved"      # stock held, waiting for the buyer to approve
    AUTHORIZED = "authorized"  # PayPal hold placed, buyer not charged
    CAPTURED = "captured"      # hold charged
    VOIDED = "voided"          # hold released, buyer never charged
    EXPIRED = "expired"        # buyer never approved in time, stock released
    FAILED = "failed"          # PayPal rejected it, needs manual follow-up


# Orders in these states are using up stock. Everything else has let go of it.
STOCK_HOLDING_STATUSES = (
    OrderStatus.RESERVED,
    OrderStatus.AUTHORIZED,
    OrderStatus.CAPTURED,
)


def _enum(enum_class: type[enum.Enum]) -> Enum:
    # Stored as plain text (not a Postgres enum type) so adding a status
    # later does not need a schema change.
    return Enum(
        enum_class,
        native_enum=False,
        values_callable=lambda e: [member.value for member in e],
        length=20,
    )


class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    drops: Mapped[list["Drop"]] = relationship(back_populates="seller")


class Drop(Base):
    __tablename__ = "drops"
    __table_args__ = (
        CheckConstraint("unit_price > 0", name="drop_price_positive"),
        CheckConstraint("quantity_total > 0", name="drop_quantity_positive"),
        CheckConstraint("minimum_units > 0", name="drop_minimum_positive"),
        CheckConstraint("minimum_units <= quantity_total", name="drop_minimum_within_quantity"),
        CheckConstraint("max_per_buyer > 0", name="drop_max_per_buyer_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id"), index=True)
    item_name: Mapped[str] = mapped_column(String(200))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    quantity_total: Mapped[int] = mapped_column(Integer)
    # The minimum is counted in units, not buyers: the seller's costs depend on units.
    minimum_units: Mapped[int] = mapped_column(Integer)
    max_per_buyer: Mapped[int] = mapped_column(Integer, default=4)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[DropStatus] = mapped_column(_enum(DropStatus), default=DropStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set when the deadline job finishes (filled or failed), for the drop planner.
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    seller: Mapped[Seller] = relationship(back_populates="drops")
    orders: Mapped[list["Order"]] = relationship(back_populates="drop")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="order_quantity_positive"),
        CheckConstraint("amount > 0", name="order_amount_positive"),
        # The deadline job looks up "all orders in this drop with this status".
        Index("ix_orders_drop_status", "drop_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"))

    # Buyers have no accounts: just what they type into the chat.
    buyer_name: Mapped[str] = mapped_column(String(120))
    buyer_email: Mapped[str] = mapped_column(String(254))
    chat_session_id: Mapped[str] = mapped_column(String(64), index=True)

    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # quantity x unit price, fixed at order time

    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.RESERVED)
    # An unapproved reservation lets go of its stock after this time.
    reserved_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Unique, so a repeated webhook cannot attach the same PayPal order twice.
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paypal_authorization_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    authorization_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    drop: Mapped[Drop] = relationship(back_populates="orders")


class DropEvent(Base):
    """A simple log of what happened in a drop. Feeds the planner and the demo."""

    __tablename__ = "drop_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    kind: Mapped[str] = mapped_column(String(40))  # e.g. "order_reserved", "drop_filled"
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def conversation_id(role: str, session_id: str) -> str:
    """Key of one chat conversation. The role prefix keeps buyer and seller chats apart."""
    return f"{role}:{session_id}"


class ChatMessage(Base):
    """One message in an agent conversation, stored so chats survive restarts."""

    __tablename__ = "chat_messages"
    __table_args__ = (Index("ix_chat_messages_conversation", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # "<role>:<session id>", so a buyer can never read a seller conversation by guessing ids.
    conversation_id: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(12))  # "user", "assistant" or "tool"
    text: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list | None] = mapped_column(JSON)  # assistant: [{id, name, args}]
    call_id: Mapped[str | None] = mapped_column(String(80))  # tool: which call this answers
    tool_name: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
