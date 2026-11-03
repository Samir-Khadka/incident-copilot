"""Week 2 tests: CI webhook events, docker collector normalization, timeline UI."""
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from collectors.docker_events import normalize_docker_event

SECRET = "test-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def post_webhook(client, payload, event, delivery, secret=SECRET):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
    return client.post("/webhooks/github", content=body, headers=headers)


WORKFLOW_RUN_PAYLOAD = {
    "action": "completed",
    "repository": {"full_name": "Samir-Khadka/incident-copilot"},
    "sender": {"login": "samir"},
    "workflow_run": {
        "name": "CI",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": "abc1234567890",
        "html_url": "https://github.com/Samir-Khadka/incident-copilot/actions/runs/42",
        "updated_at": "2026-11-03T12:00:00Z",
    },
}

DEPLOY_STATUS_PAYLOAD = {
    "repository": {"full_name": "Samir-Khadka/incident-copilot"},
    "deployment": {"ref": "main", "sha": "abc1234567890"},
    "deployment_status": {
        "state": "success",
        "environment": "production",
        "target_url": "https://example.com",
        "updated_at": "2026-11-03T12:05:00Z",
        "creator": {"login": "samir"},
    },
}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_workflow_run_completed_success(client):
    r = post_webhook(client, WORKFLOW_RUN_PAYLOAD, "workflow_run", "d-wr-1")
    assert r.status_code == 200
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?type=workflow_run").json()
    assert ev["total"] == 1
    e = ev["events"][0]
    assert "Workflow 'CI' success on main" in e["title"]
    assert "(abc1234)" in e["title"]
    assert e["url"].endswith("/actions/runs/42")
    assert e["actor"] == "samir"


def test_workflow_run_completed_failure(client):
    payload = json.loads(json.dumps(WORKFLOW_RUN_PAYLOAD))
    payload["workflow_run"]["conclusion"] = "failure"
    r = post_webhook(client, payload, "workflow_run", "d-wr-2")
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?type=workflow_run").json()
    assert "failure" in ev["events"][0]["title"]


def test_workflow_run_non_completed_ignored(client):
    payload = json.loads(json.dumps(WORKFLOW_RUN_PAYLOAD))
    payload["action"] = "requested"
    r = post_webhook(client, payload, "workflow_run", "d-wr-3")
    assert r.status_code == 200
    assert r.json()["msg"] == "ignored"
    assert client.get("/api/events?type=workflow_run").json()["total"] == 0


def test_deployment_status_ingested(client):
    r = post_webhook(client, DEPLOY_STATUS_PAYLOAD, "deployment_status", "d-ds-1")
    assert r.status_code == 200
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?type=deployment_status").json()
    assert ev["total"] == 1
    e = ev["events"][0]
    assert "Deploy to production: success" in e["title"]
    assert e["actor"] == "samir"
    assert e["url"] == "https://example.com"


def test_deployment_status_failure_ingested(client):
    payload = json.loads(json.dumps(DEPLOY_STATUS_PAYLOAD))
    payload["deployment_status"]["state"] = "failure"
    r = post_webhook(client, payload, "deployment_status", "d-ds-2")
    assert r.json()["msg"] == "stored"
    ev = client.get("/api/events?type=deployment_status").json()
    assert "failure" in ev["events"][0]["title"]


def test_signature_rejected_for_new_event_types(client):
    r = post_webhook(
        client, WORKFLOW_RUN_PAYLOAD, "workflow_run", "d-wr-bad", secret=None
    )
    assert r.status_code == 401
    r = post_webhook(
        client, DEPLOY_STATUS_PAYLOAD, "deployment_status", "d-ds-bad", secret="wrong"
    )
    assert r.status_code == 403


def test_workflow_run_redelivery_deduped(client):
    post_webhook(client, WORKFLOW_RUN_PAYLOAD, "workflow_run", "d-wr-dupe")
    r = post_webhook(client, WORKFLOW_RUN_PAYLOAD, "workflow_run", "d-wr-dupe")
    assert r.json()["msg"] == "duplicate"


# --- docker collector normalization (pure unit tests, no daemon) ---


def test_docker_container_die():
    evt = {
        "Type": "container",
        "Action": "die",
        "time": 1730000000,
        "Actor": {"ID": "abc123", "Attributes": {"name": "web", "exitCode": "1", "image": "nginx:latest"}},
    }
    n = normalize_docker_event(evt)
    assert n["source"] == "docker"
    assert n["type"] == "container_die"
    assert "web" in n["title"] and "code 1" in n["title"]
    assert n["occurred_at"].startswith("2024-10-27")


def test_docker_container_oom():
    evt = {
        "Type": "container",
        "Action": "die",
        "time": 1730000000,
        "Actor": {"ID": "abc123", "Attributes": {"name": "api", "exitCode": "137", "oomKilled": "true", "image": "myapp:1"}},
    }
    n = normalize_docker_event(evt)
    assert n["type"] == "container_oom"
    assert "OOM-killed" in n["title"]


def test_docker_container_destroy():
    evt = {
        "Type": "container",
        "Action": "destroy",
        "time": 1730000000,
        "Actor": {"ID": "abc123", "Attributes": {"name": "tmp"}},
    }
    n = normalize_docker_event(evt)
    assert n["type"] == "container_destroy"
    assert "destroyed" in n["title"]


def test_docker_image_pull():
    evt = {
        "Type": "image",
        "Action": "pull",
        "time": 1730000000,
        "Actor": {"ID": "sha256:xyz", "Attributes": {"name": "redis:7"}},
    }
    n = normalize_docker_event(evt)
    assert n["type"] == "image_pull"
    assert "redis:7" in n["title"]


def test_docker_unwatched_action_ignored():
    assert normalize_docker_event({"Type": "container", "Action": "start", "time": 1, "Actor": {}}) is None
    assert normalize_docker_event({"Type": "network", "Action": "connect", "time": 1, "Actor": {}}) is None


# --- timeline UI ---


def test_timeline_page_serves(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'type="datetime-local"' in html
    assert "Around alert time" in html
    assert "last 1h" in html and "last 6h" in html and "last 24h" in html
    assert "/api/events" in html
