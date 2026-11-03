"""Week 4 tests — Telegram bot commands, formatting, polling. No network, no daemon."""
import os

import pytest

from bot import commands
from bot import main as bot_main


def fake_fetch(events, total=None):
    def fetch(params):
        return {"events": events, "total": total if total is not None else len(events)}
    return fetch


EV1 = {
    "source": "github", "type": "push", "occurred_at": "2026-11-03T10:00:00Z",
    "actor": "samir", "repo": "Samir-Khadka/incident-copilot",
    "title": "3 commit(s) pushed to main", "url": None,
}
EV_FAIL = {
    "source": "github", "type": "workflow_run", "occurred_at": "2026-11-03T11:00:00Z",
    "actor": None, "repo": "Samir-Khadka/incident-copilot",
    "title": "Workflow 'CI' failure on main (abc1234)", "url": None,
}
EV_RESTORE = {
    "source": "restore", "type": "restore_check", "occurred_at": "2026-11-03T03:00:00Z",
    "actor": None, "repo": None,
    "title": "Restore check (nightly): PASS (3/3 checks, 42s)", "url": None,
}


# ---- command parsing --------------------------------------------------------


def test_parse_command_basic():
    assert commands.parse_command("/changes") == ("changes", [])
    assert commands.parse_command("/changes 5") == ("changes", ["5"])
    assert commands.parse_command("/changes@mybot 3") == ("changes", ["3"])
    assert commands.parse_command("/START") == ("start", [])


def test_parse_command_rejects_plain_text():
    assert commands.parse_command("what changed?") is None
    assert commands.parse_command("") is None


def test_match_natural():
    assert commands.match_natural("what changed in the last 2 hours?") == ("changes", ["2"])
    assert commands.match_natural("What changed in the last 5 hours") == ("changes", ["5"])
    assert commands.match_natural("did last night's backup restore ok?") == ("restore", [])
    assert commands.match_natural("restore status?") == ("restore", [])
    assert commands.match_natural("any alerts?") == ("alerts", [])
    assert commands.match_natural("hello there") is None


def test_hours_arg_clamped():
    assert commands._hours_arg([], 2) == 2
    assert commands._hours_arg(["5"], 2) == 5
    assert commands._hours_arg(["0"], 2) == 1
    assert commands._hours_arg(["9999"], 2) == 168
    assert commands._hours_arg(["abc"], 2) == 2


# ---- answer formatting ------------------------------------------------------


def test_changes_formats_events():
    reply = commands.handle_changes(fake_fetch([EV1, EV_FAIL], total=2), ["2"])
    assert "last 2h" in reply
    assert "3 commit(s) pushed to main" in reply
    assert "❌" in reply  # failure icon inferred from title


def test_changes_empty():
    reply = commands.handle_changes(fake_fetch([]), ["2"])
    assert "Nothing recorded" in reply


def test_changes_caps_at_ten():
    many = [dict(EV1, title=f"event {i}") for i in range(15)]
    reply = commands.handle_changes(fake_fetch(many, total=15), ["2"])
    assert "…and 5 more" in reply
    assert "timeline" in reply


def test_restore_latest():
    reply = commands.handle_restore(fake_fetch([EV_RESTORE]))
    assert "PASS (3/3 checks, 42s)" in reply


def test_restore_none_recorded():
    reply = commands.handle_restore(fake_fetch([]))
    assert "No restore checks recorded yet" in reply


def test_alerts_filters_types():
    reply = commands.handle_alerts(fake_fetch([EV1, EV_FAIL, EV_RESTORE]))
    assert "failure" in reply
    assert "3 commit(s) pushed" not in reply  # push is not an alert type
    assert "PASS" in reply  # restore_check is an alert type


def test_dispatch_unknown_command():
    assert commands.dispatch("/frobnicate", fake_fetch([])).startswith("Unknown command")


def test_dispatch_ignores_chatter():
    assert commands.dispatch("hello there", fake_fetch([])) is None


def test_dispatch_start_help():
    assert "Incident Copilot" in commands.dispatch("/start", fake_fetch([]))
    assert "/changes" in commands.dispatch("/help", fake_fetch([]))


# ---- polling loop -----------------------------------------------------------


class FakeClient:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []

    def get_updates(self, offset=None, long_poll_s=25):
        return self.updates

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


def test_poll_once_advances_offset_and_replies():
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 1}, "text": "/restore"}},
        {"update_id": 11, "message": {"chat": {"id": 1}, "text": "just chatting"}},
        {"update_id": 12, "message": {"chat": {"id": 2}}},  # no text -> ignored
    ]
    client = FakeClient(updates)
    nxt = bot_main.poll_once(client, fake_fetch([EV_RESTORE]), offset=None)
    assert nxt == 13
    assert len(client.sent) == 1  # only /restore got a reply
    chat_id, text = client.sent[0]
    assert chat_id == 1
    assert "PASS" in text


def test_poll_once_continues_from_offset():
    client = FakeClient([{"update_id": 20, "message": {"chat": {"id": 1}, "text": "/help"}}])
    assert bot_main.poll_once(client, fake_fetch([]), offset=20) == 21
    assert any("/changes" in t for _, t in client.sent)


def test_main_exits_without_token(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        bot_main.main()
    assert exc.value.code == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_main_requires_token_even_empty(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(SystemExit) as exc:
        bot_main.main()
    assert exc.value.code == 2
