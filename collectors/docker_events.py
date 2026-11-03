"""Docker events collector (Week 2).

Tails `docker events` and POSTs container/image lifecycle events to the
Incident Copilot API (`POST /api/events`, source="docker").

Run as a sidecar next to the API, e.g.::

    COPILOT_API_URL=http://localhost:8000 python -m collectors.docker_events

Degrades cleanly: if no Docker daemon is reachable it logs to stderr and
exits non-zero — it never touches the API process.

Stdlib only: no extra dependencies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

API_URL = os.environ.get("COPILOT_API_URL", "http://localhost:8000").rstrip("/")

# Only these docker event actions are normalized; everything else is ignored.
WATCHED = {
    ("container", "die"),
    ("container", "destroy"),
    ("image", "pull"),
}


def normalize_docker_event(evt: dict) -> dict | None:
    """Pure function: docker event JSON -> API ingest payload, or None to skip."""
    etype = evt.get("Type")
    action = evt.get("Action")
    if (etype, action) not in WATCHED:
        return None

    attrs = (evt.get("Actor") or {}).get("Attributes") or {}
    ts = evt.get("time")
    occurred = (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(ts, (int, float))
        else datetime.now(timezone.utc)
    )

    if etype == "container" and action == "die":
        name = attrs.get("name") or (evt.get("Actor") or {}).get("ID", "")[:12]
        if attrs.get("oomKilled") == "true":
            return {
                "source": "docker",
                "type": "container_oom",
                "occurred_at": occurred.isoformat(),
                "actor": name,
                "title": f"Container '{name}' OOM-killed ({attrs.get('image', 'unknown image')})",
                "payload": evt,
            }
        return {
            "source": "docker",
            "type": "container_die",
            "occurred_at": occurred.isoformat(),
            "actor": name,
            "title": f"Container '{name}' exited (code {attrs.get('exitCode', '?')})",
            "payload": evt,
        }

    if etype == "container" and action == "destroy":
        name = attrs.get("name") or (evt.get("Actor") or {}).get("ID", "")[:12]
        return {
            "source": "docker",
            "type": "container_destroy",
            "occurred_at": occurred.isoformat(),
            "actor": name,
            "title": f"Container '{name}' destroyed",
            "payload": evt,
        }

    if etype == "image" and action == "pull":
        name = attrs.get("name") or "unknown"
        return {
            "source": "docker",
            "type": "image_pull",
            "occurred_at": occurred.isoformat(),
            "actor": name,
            "title": f"Image '{name}' pulled",
            "payload": evt,
        }

    return None


def post_event(payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API_URL}/api/events",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"API returned {resp.status}")


def docker_available() -> bool:
    try:
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def main() -> int:
    if not docker_available():
        print(
            "docker_events: no Docker daemon reachable ('docker info' failed) — exiting.",
            file=sys.stderr,
        )
        return 1

    cmd = [
        "docker", "events",
        "--format", "{{json .}}",
        "--filter", "event=die",
        "--filter", "event=destroy",
        "--filter", "event=pull",
    ]
    print(f"docker_events: streaming to {API_URL}/api/events", file=sys.stderr)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"docker_events: failed to start docker events: {exc}", file=sys.stderr)
        return 1

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            normalized = normalize_docker_event(evt)
            if normalized is None:
                continue
            try:
                post_event(normalized)
            except Exception as exc:  # noqa: BLE001 — keep streaming on API errors
                print(f"docker_events: POST failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
