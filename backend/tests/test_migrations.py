"""The migrations must build exactly the schema that models.py describes."""

import tempfile
from pathlib import Path

import pixeltable_pgserver
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]


def test_migrations_match_models():
    server = pixeltable_pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        engine.dispose()
        assert diff == [], f"models.py has changes with no migration: {diff}"

        # And it must be reversible.
        command.downgrade(cfg, "base")
    finally:
        server.cleanup()


def test_the_pickup_and_delivery_migration_works_on_a_database_that_already_has_rows():
    """Production already has drops and orders. The new columns must not break them."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    server = pixeltable_pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="delete")
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "ff187c2efbc2")  # the schema before pickup and delivery existed

        engine = create_engine(url)
        with engine.begin() as c:
            c.execute(text("INSERT INTO sellers (id, name, created_at) VALUES (1, 'Old shop', now())"))
            c.execute(text("""INSERT INTO drops (id, seller_id, item_name, unit_price, currency, quantity_total, minimum_units,
                              max_per_buyer, deadline, status, created_at)
                              VALUES (1, 1, 'Old drop', 9, 'USD', 10, 5, 4, now(), 'open', now())"""))
            c.execute(text("""INSERT INTO orders (id, drop_id, buyer_name, buyer_email, chat_session_id, quantity, amount, status,
                              reserved_until, created_at) VALUES (1, 1, 'B', 'b@x.co', 's', 1, 9, 'reserved', now(), now())"""))

        command.upgrade(cfg, "head")  # must not fail on those rows
        with engine.connect() as c:
            drop = c.execute(text("SELECT offers_pickup, offers_delivery, delivery_fee FROM drops")).one()
            order = c.execute(text("SELECT fulfillment, delivery_fee FROM orders")).one()
        assert (drop.offers_pickup, drop.offers_delivery, float(drop.delivery_fee)) == (True, False, 0.0)
        assert (order.fulfillment, float(order.delivery_fee)) == ("pickup", 0.0)

        # The check constraints were created too (Alembic would not have detected them).
        for bad in ("offers_pickup = false, offers_delivery = false", "delivery_fee = -1", "delivery_radius_km = 0"):
            with pytest.raises(IntegrityError):
                with engine.begin() as c:
                    c.execute(text(f"UPDATE drops SET {bad}"))

        command.downgrade(cfg, "ff187c2efbc2")  # and it can be undone
        engine.dispose()
    finally:
        server.cleanup()
