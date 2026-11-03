"""Database engine/session. Reads DATABASE_URL; SQLite gets a StaticPool so
in-memory databases survive across sessions (used by tests)."""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


def default_database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://copilot:copilot@localhost:5432/copilot",
    )


def make_engine(url: str | None = None):
    url = url or default_database_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        kwargs = {
            "poolclass": StaticPool,
            "connect_args": {"check_same_thread": False},
        }
    return create_engine(url, **kwargs)


class Base(DeclarativeBase):
    pass


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
