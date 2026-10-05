"""
Fill a LOCAL database with a believable demo bakery: filled, missed and still-open drops, with
orders spread over the last two weeks. Useful for screenshots, the demo video and trying the
dashboard. It creates a dev user (demo@example.com) you can sign in as with the dev sign-in.

Re-running resets the demo shop. It never touches PayPal. Use it only on a development database.

    DATABASE_URL=... python backend/scripts/seed_demo.py     (./dev.sh sets DATABASE_URL for you)
"""

import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select  # noqa: E402

from app.db import get_session_factory  # noqa: E402
from app.models import Drop, DropEvent, DropStatus, Order, OrderStatus, Seller, User  # noqa: E402

DEMO_EMAIL = "demo@example.com"
NOW = datetime.now(timezone.utc)
rng = random.Random(7)  # fixed seed: the same demo data every time

# (item, price, total, minimum, status, created_days_ago, deadline_days_from_now, [(order status, how many orders)], delivers?)
DROPS = [
    ("Sourdough loaves", "9.00", 24, 10, DropStatus.FILLED, 13, -9, [(OrderStatus.CAPTURED, 14)], True),
    ("Cinnamon rolls (box of 4)", "5.50", 30, 20, DropStatus.FAILED, 9, -5, [(OrderStatus.VOIDED, 8)], False),
    ("Blueberry scones", "4.50", 16, 8, DropStatus.FILLED, 7, -3, [(OrderStatus.CAPTURED, 11)], False),
    ("Rye & caraway loaves", "10.50", 20, 12, DropStatus.OPEN, 4, 2, [(OrderStatus.AUTHORIZED, 9), (OrderStatus.RESERVED, 2)], True),
    ("Lemon tarts", "6.00", 12, 6, DropStatus.OPEN, 1, 5, [(OrderStatus.AUTHORIZED, 3), (OrderStatus.RESERVED, 1)], False),
]
FIRST = ["Alex", "Sam", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Quinn", "Avery", "Jamie"]


def main() -> None:
    with get_session_factory()() as s:
        user = s.scalar(select(User).where(User.email == DEMO_EMAIL, User.is_dev.is_(True)))
        if user is None:
            user = User(name="Dana Demo", email=DEMO_EMAIL, is_dev=True, paypal_verified=True, email_verified=True)
            s.add(user)
            s.commit()
        shop = s.scalar(select(Seller).where(Seller.user_id == user.id))
        if shop is not None:  # start fresh
            ids = s.scalars(select(Drop.id).where(Drop.seller_id == shop.id)).all()
            s.execute(delete(DropEvent).where(DropEvent.drop_id.in_(ids)))
            s.execute(delete(Order).where(Order.drop_id.in_(ids)))
            s.execute(delete(Drop).where(Drop.seller_id == shop.id))
        else:
            shop = Seller(name="Dana's Bakehouse", user_id=user.id)
            s.add(shop)
            s.commit()

        for item, price, total, minimum, status, made, closes, orders, delivery in DROPS:
            created = NOW - timedelta(days=made)
            deadline = NOW + timedelta(days=closes)
            drop = Drop(
                seller_id=shop.id, item_name=item, unit_price=Decimal(price), quantity_total=total,
                minimum_units=minimum, max_per_buyer=4, deadline=deadline, status=status,
                created_at=created, settled_at=deadline if status != DropStatus.OPEN else None,
                offers_pickup=True, offers_delivery=delivery, delivery_radius_km=6.0 if delivery else None,
                delivery_fee=Decimal("3.00") if delivery else Decimal("0"),
                pickup_address="281 7th Ave, Brooklyn, NY 11215", pickup_area="Park Slope, New York",
                pickup_notes="Ring the bell marked Bakehouse. Pickup is on the porch.",
                pickup_lat=40.6717, pickup_lng=-73.9806,
            )
            s.add(drop)
            s.flush()
            window = (min(deadline, NOW) - created).total_seconds()
            stock_left = total  # the real engine can never oversell, so neither can the demo data
            for order_status, count in orders:
                for _ in range(count):
                    qty = min(rng.choice([1, 1, 2, 2, 3]), stock_left)
                    if qty < 1:
                        break
                    stock_left -= qty
                    # Orders cluster early and just before the deadline, like real drops.
                    frac = rng.choice([rng.random() ** 2, 1 - rng.random() ** 3])
                    placed = created + timedelta(seconds=window * frac)
                    name = rng.choice(FIRST)
                    s.add(Order(
                        drop_id=drop.id, buyer_name=f"{name} (demo)", buyer_email="demo-buyer@example.com",
                        chat_session_id="demo", quantity=qty, amount=Decimal(price) * qty, status=order_status,
                        reserved_until=placed + timedelta(minutes=15), created_at=placed,
                    ))
        s.commit()
        print(f"Seeded the demo shop for {DEMO_EMAIL}: {len(DROPS)} drops.")
        print("Sign in locally with the dev sign-in using that email (tick verified).")


if __name__ == "__main__":
    main()
