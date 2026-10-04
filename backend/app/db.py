"""Database engine and per-request sessions."""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def normalize_url(url: str) -> str:
    """Render gives postgres:// or postgresql:// URLs; our driver wants postgresql+psycopg://."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


@lru_cache
def get_session_factory() -> sessionmaker:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    return sessionmaker(create_engine(normalize_url(url), pool_pre_ping=True), expire_on_commit=False)


def get_session():
    """FastAPI dependency: one session per request, always closed."""
    with get_session_factory()() as session:
        yield session
