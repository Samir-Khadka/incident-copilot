"""Incident Copilot — Week 3 API: GitHub webhooks (incl. CI events), events table, timeline UI, restore-check event sink."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .db import Base, engine, get_db
from .schemas import EventIn, EventList, EventOut
from .webhooks import (
    normalize_github_event,
    store_event,
    utcnow,
    verify_signature,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Incident Copilot", version="0.3.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def timeline_ui():
    """Web timeline (Week 2): static page over the JSON API."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "db": "ok"}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    db: Session = Depends(get_db),
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    event_name: str | None = Header(default=None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
):
    body = await request.body()
    verify_signature(body, signature)

    if event_name == "ping":
        return {"msg": "pong"}

    payload = await request.json()
    normalized = normalize_github_event(event_name or "", payload)
    if normalized is None:
        return {"msg": "ignored", "event": event_name}

    event = store_event(db, normalized, delivery_id)
    if event is None:
        return {"msg": "duplicate", "delivery": delivery_id}
    return JSONResponse(
        status_code=200,
        content={"msg": "stored", "id": str(event.id)},
    )


@app.post("/api/events", response_model=EventOut, status_code=201)
def ingest_event(data: EventIn, db: Session = Depends(get_db)):
    """Generic ingest — Week 2's CI/Docker collectors post here."""
    event = models.Event(
        source=data.source,
        type=data.type,
        occurred_at=data.occurred_at or utcnow(),
        actor=data.actor,
        repo=data.repo,
        title=data.title,
        url=data.url,
        payload=data.payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@app.get("/api/events", response_model=EventList)
def list_events(
    db: Session = Depends(get_db),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    source: str | None = Query(default=None),
    type: str | None = Query(default=None, alias="type"),
    repo: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Timeline API: newest first, filterable — the 'what changed?' query surface."""
    stmt = select(models.Event)
    count_stmt = select(func.count()).select_from(models.Event)
    if since is not None:
        stmt = stmt.where(models.Event.occurred_at >= since)
        count_stmt = count_stmt.where(models.Event.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(models.Event.occurred_at <= until)
        count_stmt = count_stmt.where(models.Event.occurred_at <= until)
    if source:
        stmt = stmt.where(models.Event.source == source)
        count_stmt = count_stmt.where(models.Event.source == source)
    if type:
        stmt = stmt.where(models.Event.type == type)
        count_stmt = count_stmt.where(models.Event.type == type)
    if repo:
        stmt = stmt.where(models.Event.repo == repo)
        count_stmt = count_stmt.where(models.Event.repo == repo)

    total = db.execute(count_stmt).scalar_one()
    events = (
        db.execute(
            stmt.order_by(models.Event.occurred_at.desc()).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    return {"events": events, "total": total, "limit": limit, "offset": offset}
