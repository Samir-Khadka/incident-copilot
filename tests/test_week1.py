import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

SECRET = "test-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, payload, event="push", delivery="del-1", secret=SECRET):
    body = json.dumps(payload).encode()
    sig = (
        "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if secret
        else None
    )
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if sig:
        headers["X-Hub-Signature-256"] = sig
    return client.post("/webhooks/github", content=body, headers=headers)


PUSH_PAYLOAD = {
    "ref": "refs/heads/main",
    "pusher": {"name": "samir"},
    "repository": {"full_name": "Samir-Khadka/incident-copilot"},
    "compare": "https://github.com/Samir-Khadka/incident-copilot/compare/a...b",
    "commits": [{"id": "a"}, {"id": "b"}],
    "head_commit": {"timestamp": "2026-11-03T10:00:00Z"},
}

PR_PAYLOAD = {
    "action": "opened",
    "number": 7,
    "repository": {"full_name": "Samir-Khadka/incident-copilot"},
    "sender": {"login": "samir"},
    "pull_request": {
        "title": "Add restore checker",
        "html_url": "https://github.com/Samir-Khadka/incident-copilot/pull/7",
        "updated_at": "2026-11-03T11:00:00Z",
    },
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_ping_returns_pong(client):
    body = json.dumps({"zen": "hello"}).encode()
    r = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "ping",
            "X-Hub-Signature-256": sign(body),
            "X-GitHub-Delivery": "ping-1",
        },
    )
    assert r.status_code == 200
    assert r.json() == {"msg": "pong"}


def test_missing_signature_rejected(client):
    r = client.post(
        "/webhooks/github",
        content=b"{}",
        headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "d-missing"},
    )
    assert r.status_code == 401


def test_invalid_signature_rejected(client):
    r = post_webhook(client, PUSH_PAYLOAD, delivery="d-bad", secret="wrong-secret")
    assert r.status_code == 403


def test_push_ingested(client):
    r = post_webhook(client, PUSH_PAYLOAD, event="push", delivery="d-push-1")
    assert r.status_code == 200
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?source=github&type=push").json()
    assert ev["total"] == 1
    e = ev["events"][0]
    assert e["repo"] == "Samir-Khadka/incident-copilot"
    assert e["actor"] == "samir"
    assert "2 commit(s) pushed to main" in e["title"]


def test_pull_request_ingested(client):
    r = post_webhook(client, PR_PAYLOAD, event="pull_request", delivery="d-pr-1")
    assert r.status_code == 200
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?type=pull_request").json()
    assert ev["total"] >= 1
    e = ev["events"][0]
    assert "PR #7 opened" in e["title"]
    assert e["url"].endswith("/pull/7")


def test_redelivery_deduped(client):
    r1 = post_webhook(client, PUSH_PAYLOAD, delivery="d-dupe-1")
    r2 = post_webhook(client, PUSH_PAYLOAD, delivery="d-dupe-1")
    assert r1.json()["msg"] == "stored"
    assert r2.json()["msg"] == "duplicate"
    ev = client.get("/api/events?type=push&repo=Samir-Khadka/incident-copilot").json()
    assert ev["total"] == 1


def test_ignored_event_type(client):
    r = post_webhook(client, {}, event="issues", delivery="d-issues-1")
    assert r.status_code == 200
    assert r.json()["msg"] == "ignored"


def test_timeline_filters_and_pagination(client):
    now = datetime.now(timezone.utc)
    # seed via generic ingest: 5 events across sources/repos/times
    for i in range(5):
        r = client.post(
            "/api/events",
            json={
                "source": "ci" if i % 2 else "github",
                "type": "deploy" if i % 2 else "push",
                "occurred_at": (now - timedelta(hours=i)).isoformat(),
                "repo": "org/repo-a" if i < 3 else "org/repo-b",
                "title": f"event {i}",
            },
        )
        assert r.status_code == 201

    # source filter
    assert client.get("/api/events?source=ci").json()["total"] == 2
    # repo filter
    assert client.get("/api/events?repo=org/repo-b").json()["total"] == 2
    # since/until window (hours 1..3) — params= so '+' in the offset is encoded
    q = client.get(
        "/api/events",
        params={
            "since": (now - timedelta(hours=3, minutes=1)).isoformat(),
            "until": (now - timedelta(minutes=30)).isoformat(),
        },
    ).json()
    assert q["total"] == 3
    # pagination: newest first
    p1 = client.get("/api/events?limit=2&offset=0").json()
    p2 = client.get("/api/events?limit=2&offset=2").json()
    assert len(p1["events"]) == 2 and len(p2["events"]) == 2
    assert p1["events"][0]["title"] == "event 0"
    assert p2["events"][0]["title"] == "event 2"
    assert p1["total"] == 5


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


def test_generic_ingest_requires_source_and_type(client):
    r = client.post("/api/events", json={"title": "no source"})
    assert r.status_code == 422
