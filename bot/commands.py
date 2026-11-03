"""Command parsing, natural-language matching and answer formatting.

All handlers are pure functions over a ``fetch`` callable so they are trivially
testable without a Telegram connection or a running API:

    fetch(params: dict) -> dict   # GET /api/events?{params} -> {"events": [...], "total": N}
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

MAX_EVENTS_PER_REPLY = 10
TIMELINE_HINT = "Full history: open the web timeline (GET / on the API host)."

# Event types worth surfacing under /alerts (failures, deaths, deploys, restores).
ALERT_TYPES = {
    "workflow_run",
    "deployment_status",
    "container_die",
    "container_oom",
    "restore_check",
}

_FAIL_WORDS = ("fail", "error", "oom", "die", "destroy", "critical", "exception")
_OK_WORDS = ("success", "pass", "ok", "healthy")


def _icon_for(title: str | None) -> str:
    t = (title or "").lower()
    if any(w in t for w in _FAIL_WORDS):
        return "❌"
    if any(w in t for w in _OK_WORDS):
        return "✅"
    return "•"


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M %d %b") + " UTC"
    except ValueError:
        return iso[:16]


def _fmt_event(e: dict) -> str:
    bits = [f"{_icon_for(e.get('title'))} {_fmt_time(e.get('occurred_at'))}"]
    src_type = f"{e.get('source')}/{e.get('type')}"
    bits.append(f"[{src_type}]")
    if e.get("title"):
        bits.append(str(e["title"])[:120])
    if e.get("repo"):
        bits.append(f"— {e['repo']}")
    return " ".join(bits)


def _cap(events: list[dict]) -> tuple[list[str], int]:
    lines = [_fmt_event(e) for e in events[:MAX_EVENTS_PER_REPLY]]
    return lines, max(0, len(events) - len(lines))


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """'/changes 5' -> ('changes', ['5']). Handles '@BotName' suffixes."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split()
    cmd = parts[0].split("@")[0].lower()
    return cmd, parts[1:]


_HOURS_RE = re.compile(r"(\d+)\s*hour", re.IGNORECASE)


def match_natural(text: str) -> tuple[str, list[str]] | None:
    """Plain-language matching. AI summarization is a documented future step."""
    t = (text or "").lower()
    if "what changed" in t:
        m = _HOURS_RE.search(t)
        return ("changes", [m.group(1)] if m else [])
    if "backup" in t or "restore" in t:
        return ("restore", [])
    if "alert" in t:
        return ("alerts", [])
    return None


def _hours_arg(args: list[str], default: int = 2) -> int:
    try:
        h = int(args[0])
        return min(max(h, 1), 168)  # clamp to 1h..7d
    except (IndexError, ValueError):
        return default


def handle_start() -> str:
    return (
        "👋 Incident Copilot bot.\n"
        "I answer two questions: *what changed?* and *can we trust our backups?*\n"
        "Try /changes, /restore, /alerts — or just ask in plain words. /help for details."
    )


def handle_help() -> str:
    return (
        "Commands:\n"
        "/changes [hours] — what changed recently (default: last 2h)\n"
        "/restore — did the last backup restore OK?\n"
        "/alerts — latest failures, deploys and container events\n"
        "/help — this message\n\n"
        "You can also write naturally: “what changed in the last 5 hours?”\n"
        "Times are UTC. AI summarization is a future step — for now you get the raw facts."
    )


def handle_changes(fetch, args: list[str]) -> str:
    hours = _hours_arg(args)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    data = fetch({"since": since, "limit": MAX_EVENTS_PER_REPLY + 1})
    events = data.get("events", [])
    total = data.get("total", len(events))
    if not events:
        return f"🔄 Nothing recorded in the last {hours}h. Quiet — or the collectors aren't running."
    lines, more = _cap(events)
    head = f"🔄 Changes in the last {hours}h ({total} event{'s' if total != 1 else ''}):"
    tail = f"\n…and {more} more. {TIMELINE_HINT}" if more else ""
    return head + "\n" + "\n".join(lines) + tail


def handle_restore(fetch) -> str:
    data = fetch({"source": "restore", "type": "restore_check", "limit": 1})
    events = data.get("events", [])
    if not events:
        return "💾 No restore checks recorded yet. Schedule one (see restore/cron.example)."
    e = events[0]
    return f"💾 Last restore check:\n{_fmt_event(e)}"


def handle_alerts(fetch) -> str:
    data = fetch({"limit": 50})
    events = [e for e in data.get("events", []) if e.get("type") in ALERT_TYPES]
    if not events:
        return "🚨 No failures, deploys or container events on record."
    lines, more = _cap(events)
    head = f"🚨 Latest alerts ({len(events)}):"
    tail = f"\n…and {more} more. {TIMELINE_HINT}" if more else ""
    return head + "\n" + "\n".join(lines) + tail


def dispatch(text: str, fetch) -> str | None:
    """Route a message to a handler. Returns the reply text, or None to ignore."""
    parsed = parse_command(text)
    if parsed is None:
        matched = match_natural(text)
        if matched is None:
            return None
        cmd, args = matched
    else:
        cmd, args = parsed
    if cmd == "start":
        return handle_start()
    if cmd == "help":
        return handle_help()
    if cmd == "changes":
        return handle_changes(fetch, args)
    if cmd == "restore":
        return handle_restore(fetch)
    if cmd == "alerts":
        return handle_alerts(fetch)
    return f"Unknown command /{cmd}. Try /help."
