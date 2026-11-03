"""Event model — the single table every collector writes to."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(32), index=True)  # github, ci, docker, ...
    type: Mapped[str] = mapped_column(String(64), index=True)  # push, pull_request, deploy, ...
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=utcnow
    )
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repo: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # GitHub delivery id — unique so redeliveries are ignored, not duplicated.
    delivery_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    payload: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
