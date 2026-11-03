"""Week 1 tests — run against in-memory SQLite (prod is Postgres)."""
import hashlib
import hmac
import json
import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Event  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    """Isolate tests: the in-memory DB is shared via StaticPool."""
    Base.metadata.create_all(engine)
    db = SessionLocal()
    db.query(Event).delete()
    db.commit()
    db.close()
