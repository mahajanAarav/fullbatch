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
    Boolean,
    CheckConstraint,
    Float,
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


class User(Base):
    """A signed-in person. Buyers and sellers are both Users; a seller also owns a shop."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # PayPal's stable id for this person. Unique, so signing in twice never makes two users.
    paypal_payer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(254))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(120))
    # PayPal's own verdict that this is a verified PayPal account. Sellers need it to open drops.
    paypal_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dev: Mapped[bool] = mapped_column(Boolean, default=False)  # created by the local-only dev sign-in
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    """A browser's sign-in. Only a hash of the cookie token is stored, so a database leak is not a session leak."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailCode(Base):
    """A one-time code emailed to confirm someone controls their address. Only a hash is kept."""

    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254))  # the address the code was sent to
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # The person who owns this shop. One shop per user.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User | None"] = relationship()
    drops: Mapped[list["Drop"]] = relationship(back_populates="seller")

    @property
    def verified(self) -> bool:
        """A shop is verified when its owner's PayPal account is."""
        return bool(self.user and self.user.paypal_verified)


class Drop(Base):
    __tablename__ = "drops"
    __table_args__ = (
        CheckConstraint("unit_price > 0", name="drop_price_positive"),
        CheckConstraint("quantity_total > 0", name="drop_quantity_positive"),
        CheckConstraint("minimum_units > 0", name="drop_minimum_positive"),
        CheckConstraint("minimum_units <= quantity_total", name="drop_minimum_within_quantity"),
        CheckConstraint("max_per_buyer > 0", name="drop_max_per_buyer_positive"),
        CheckConstraint("offers_pickup OR offers_delivery", name="drop_offers_a_way_to_receive"),
        CheckConstraint("delivery_fee >= 0", name="drop_delivery_fee_not_negative"),
        CheckConstraint("delivery_radius_km IS NULL OR delivery_radius_km > 0", name="drop_delivery_radius_positive"),
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
    # ---- where, and how buyers receive it ----
    offers_pickup: Mapped[bool] = mapped_column(Boolean, default=True)
    offers_delivery: Mapped[bool] = mapped_column(Boolean, default=False)
    pickup_area: Mapped[str | None] = mapped_column(String(120))     # public: "Park Slope, New York"
    pickup_address: Mapped[str | None] = mapped_column(String(300))  # PRIVATE until a buyer's hold is approved
    pickup_notes: Mapped[str | None] = mapped_column(String(300))    # PRIVATE until a buyer's hold is approved
    pickup_lat: Mapped[float | None] = mapped_column(Float)          # exact; only ever shown rounded to ~1 km
    pickup_lng: Mapped[float | None] = mapped_column(Float)
    delivery_radius_km: Mapped[float | None] = mapped_column(Float)  # measured from the pickup point
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
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
    buyer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    chat_session_id: Mapped[str] = mapped_column(String(64), index=True)

    quantity: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # quantity x unit price (+ delivery fee), fixed at order time
    fulfillment: Mapped[str] = mapped_column(String(10), default="pickup")  # "pickup" or "delivery"
    delivery_address: Mapped[str | None] = mapped_column(String(300))  # shown to the seller only once the hold is approved
    delivery_lat: Mapped[float | None] = mapped_column(Float)
    delivery_lng: Mapped[float | None] = mapped_column(Float)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))

    status: Mapped[OrderStatus] = mapped_column(_enum(OrderStatus), default=OrderStatus.RESERVED)
    # An unapproved reservation lets go of its stock after this time.
    reserved_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # Unique, so a repeated webhook cannot attach the same PayPal order twice.
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paypal_authorization_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    authorization_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the current hold began. PayPal only guarantees funds for 3 days after this; a longer wait needs a re-authorization.
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paypal_capture_id: Mapped[str | None] = mapped_column(String(64))  # the charge, once captured

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    drop: Mapped[Drop] = relationship(back_populates="orders")


class Payout(Base):
    """What a seller is paid when their drop fills: what buyers paid, minus the platform fee."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    drop_id: Mapped[int] = mapped_column(ForeignKey("drops.id"), unique=True)  # one payout per drop, ever
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id"), index=True)
    gross: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fee: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    net: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    receiver_email: Mapped[str | None] = mapped_column(String(254))
    batch_id: Mapped[str | None] = mapped_column(String(64), unique=True)  # PayPal's payout batch id
    # pending (sent, not finished) | success | unclaimed | denied | unavailable (will retry) | skipped
    status: Mapped[str] = mapped_column(String(20), default="pending")
    detail: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
