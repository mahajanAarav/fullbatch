"""
Shared test fixtures.

Tests run against a real, throwaway Postgres (bundled in the pip package
pixeltable-pgserver), because the row locking we rely on does not exist in SQLite.
"""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pixeltable_pgserver
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Make `import app...` work no matter where pytest is started from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Base, Seller, User  # noqa: E402
from app import drops  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    """Start one Postgres for the whole test run and create the tables."""
    data_dir = tempfile.mkdtemp()
    server = pixeltable_pgserver.get_server(data_dir, cleanup_mode="delete")
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, pool_size=30, max_overflow=0)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    server.cleanup()


@pytest.fixture
def session_factory(engine):
    """Gives each test empty tables. Tests that need many sessions use this directly."""
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE chat_messages, drop_events, orders, drops, sellers, auth_sessions, users RESTART IDENTITY CASCADE")
        )
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory) -> Session:
    with session_factory() as s:
        yield s


@pytest.fixture
def seller_user(session) -> User:
    """A seller's PayPal-verified account."""
    u = User(name="Baker Bea", email="bea@example.com", paypal_payer_id="PAYER-SELLER", paypal_verified=True, email_verified=True)
    session.add(u)
    session.commit()
    return u


@pytest.fixture
def buyer(session) -> User:
    u = User(name="Sam Lee", email="sam@example.com", paypal_payer_id="PAYER-BUYER", paypal_verified=True, email_verified=True)
    session.add(u)
    session.commit()
    return u


@pytest.fixture
def seller(session, seller_user) -> Seller:
    s = Seller(name="Test Bakery", user_id=seller_user.id)
    session.add(s)
    session.commit()
    return s


@pytest.fixture
def make_drop(session, seller):
    """Build a drop with sensible defaults. Override only what a test cares about."""

    def _make(**overrides):
        args = dict(
            seller_id=seller.id,
            item_name="Sourdough loaf",
            unit_price=Decimal("9.00"),
            quantity_total=10,
            minimum_units=5,
            deadline=datetime.now(timezone.utc) + timedelta(days=3),
            pickup_address="350 5th Ave, New York, NY",
            pickup_area="Koreatown, New York",
            pickup_lat=40.7484,
            pickup_lng=-73.9857,
        )
        args.update(overrides)
        return drops.create_drop(session, **args)

    return _make
