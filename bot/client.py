"""Minimal Telegram Bot API client — stdlib only (urllib)."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger("copilot.bot")


class TelegramError(RuntimeError):
    """The Bot API answered ok=false or the request failed."""


class TelegramClient:
    def __init__(self, token: str, timeout: int = 40) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    def _call(self, method: str, params: dict | None = None) -> dict:
        body = json.dumps(params or {}).encode()
        req = urllib.request.Request(
            f"{self._base}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 - surfaced to the polling loop
            raise TelegramError(f"bot api {method} failed: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"bot api {method} not ok: {data}")
        return data

    def get_updates(self, offset: int | None = None, long_poll_s: int = 25) -> list[dict]:
        params: dict = {"timeout": long_poll_s, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params).get("result", [])

    def send_message(self, chat_id: int | str, text: str) -> bool:
        """Never raises — returns False on failure (logged)."""
        text = text[:4000]  # Telegram hard limit is 4096 chars
        try:
            self._call("sendMessage", {"chat_id": chat_id, "text": text})
            return True
        except Exception as e:  # noqa: BLE001 - send failures are warnings
            log.warning("send_message failed: %s", e)
            return False
