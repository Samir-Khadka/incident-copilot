"""Pydantic schemas for the timeline API and generic ingest."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    """Generic ingest payload (used by week-2 CI/Docker collectors)."""

    source: str = Field(min_length=1, max_length=32)
    type: str = Field(min_length=1, max_length=64)
    occurred_at: datetime | None = None
    actor: str | None = Field(default=None, max_length=128)
    repo: str | None = Field(default=None, max_length=256)
    title: str | None = Field(default=None, max_length=512)
    url: str | None = Field(default=None, max_length=1024)
    payload: dict | None = None


class EventOut(BaseModel):
    id: uuid.UUID
    source: str
    type: str
    occurred_at: datetime
    actor: str | None
    repo: str | None
    title: str | None
    url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class EventList(BaseModel):
    events: list[EventOut]
    total: int
    limit: int
    offset: int
