"""GitHub webhook verification + event normalization (Week 1: push, pull_request)."""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Event, utcnow


def webhook_secret() -> str:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: a webhook endpoint with no secret configured must not
        # accept anything.
        raise HTTPException(status_code=500, detail="webhook secret not configured")
    return secret


def verify_signature(body: bytes, signature_header: str | None) -> None:
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing signature")
    expected = hmac.new(
        webhook_secret().encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest("sha256=" + expected, signature_header):
        raise HTTPException(status_code=403, detail="invalid signature")


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_github_event(event_name: str, payload: dict) -> dict | None:
    """Return a normalized event dict, or None for events we ignore."""
    repo = (payload.get("repository") or {}).get("full_name")
    sender = (payload.get("sender") or {}).get("login")

    if event_name == "push":
        commits = payload.get("commits") or []
        ref = payload.get("ref", "")
        branch = ref.split("/")[-1] if ref else ""
        head = payload.get("head_commit") or {}
        return {
            "source": "github",
            "type": "push",
            "occurred_at": _parse_dt(head.get("timestamp")),
            "actor": (payload.get("pusher") or {}).get("name") or sender,
            "repo": repo,
            "title": f"{len(commits)} commit(s) pushed to {branch or ref}",
            "url": payload.get("compare"),
            "payload": payload,
        }
    if event_name == "pull_request":
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        number = payload.get("number")
        return {
            "source": "github",
            "type": "pull_request",
            "occurred_at": _parse_dt(pr.get("updated_at")),
            "actor": sender,
            "repo": repo,
            "title": f"PR #{number} {action}: {pr.get('title')}",
            "url": pr.get("html_url"),
            "payload": payload,
        }
    return None


def store_event(db: Session, data: dict, delivery_id: str | None) -> Event | None:
    """Persist a normalized event. Returns None when the delivery was a duplicate."""
    event = Event(delivery_id=delivery_id, **data)
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # X-GitHub-Delivery seen before (unique constraint) — redelivery.
        db.rollback()
        return None
    db.refresh(event)
    return event
