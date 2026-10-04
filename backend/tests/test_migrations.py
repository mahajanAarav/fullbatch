"""The migrations must build exactly the schema that models.py describes."""

import tempfile
from pathlib import Path

import pixeltable_pgserver
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
