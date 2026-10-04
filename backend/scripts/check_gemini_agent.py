"""
Live check: the real Gemini model driving the agent end to end.

Uses a throwaway Postgres and a FAKE PayPal, so no real orders or payments are
created. It does call the real Gemini API (free tier) and uses a few requests.

    python backend/scripts/check_gemini_agent.py
"""

import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import pixeltable_pgserver  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import agent  # noqa: E402
from app.config import Settings  # noqa: E402
from app.gemini import from_env  # noqa: E402
from app.models import Base, ChatMessage, Drop, Order, Seller  # noqa: E402
from tests.fakes import FakePayPal  # noqa: E402

SETTINGS = Settings(public_api_url="http://localhost:8000", frontend_url="http://localhost:5173")


def show_turn(session, conv, since_id, user_text, reply):
    print(f"\n  YOU: {user_text}")
    rows = session.scalars(
        select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.id > since_id).order_by(ChatMessage.id)
    ).all()
    for r in rows:
        if r.role == "assistant" and r.tool_calls:
            for c in r.tool_calls:
                print(f"       [tool call] {c['name']}({c['args']})")
        elif r.role == "tool":
            print(f"       [tool result] {r.text[:160]}")
    print(f"  BOT: {reply}")
    return rows[-1].id if rows else since_id


def converse(session, llm, paypal, role, session_id, lines, seller_id=None):
    conv = agent.conversation_id(role, session_id)
    last = session.scalar(select(ChatMessage.id).order_by(ChatMessage.id.desc()).limit(1)) or 0
    for line in lines:
        time.sleep(6)  # stay under the free tier's per-minute limit
        reply = agent.run_turn(session, llm, paypal, SETTINGS, role, session_id, line, seller_id=seller_id)
        last = show_turn(session, conv, last, line, reply)


def main() -> None:
    llm, paypal = from_env(), FakePayPal()
    server = pixeltable_pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
    engine = create_engine(server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1))
    Base.metadata.create_all(engine)
    checks: list[tuple[str, bool]] = []

    with sessionmaker(engine, expire_on_commit=False)() as session:
        seller = Seller(name="Test Bakery")
        session.add(seller)
        session.commit()

        print("=== SELLER CHAT ===")
        converse(session, llm, paypal, "seller", "seller-1", [
            "I'm a home baker. I want to sell 12 sourdough loaves at $9 each. I need at least 6 orders or I won't bake. Orders close in 3 days at 5pm.",
            "Yes, that's right, please create it.",
        ], seller_id=seller.id)
        drop = session.scalars(select(Drop)).first()
        checks.append(("seller's chat created a drop", drop is not None))
        if drop:
            checks.append(("drop details are right (12 units, $9, min 6)",
                           (drop.quantity_total, str(drop.unit_price), drop.minimum_units) == (12, "9.00", 6)))

        print("\n=== BUYER CHAT ===")
        converse(session, llm, paypal, "buyer", "buyer-1", [
            "What can I buy right now?",
            "I'd like 2 loaves please. My name is Sam Lee and my email is sam@example.com",
            "Yes, please place the order.",
            "Will I be charged right away?",
        ])
        order = session.scalars(select(Order)).first()
        checks.append(("buyer's chat placed an order", order is not None))
        if order:
            checks.append(("order is 2 units, tied to the buyer's session", (order.quantity, order.chat_session_id) == (2, "buyer-1")))

    print("\n=== RESULT ===")
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    engine.dispose()
    server.cleanup()
    if not all(ok for _, ok in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
