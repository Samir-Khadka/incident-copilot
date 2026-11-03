"""Telegram bot entrypoint — long-polling loop. Run: python -m bot.main (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request

from .client import TelegramClient
from .commands import dispatch

log = logging.getLogger("copilot.bot")


def make_fetcher(api_url: str):
    """Build the fetch(params) callable the command handlers query with."""

    def fetch(params: dict) -> dict:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(f"{api_url.rstrip('/')}/api/events?{qs}")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    return fetch


def poll_once(client: TelegramClient, fetch, offset: int | None) -> int | None:
    """Fetch one batch of updates, reply to each, return the next offset."""
    updates = client.get_updates(offset=offset)
    for update in updates:
        offset = update.get("update_id", offset)
        if offset is not None:
            offset += 1
        msg = update.get("message") or {}
        text = msg.get("text")
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not text or chat_id is None:
            continue
        try:
            reply = dispatch(text, fetch)
        except Exception as e:  # noqa: BLE001 - one bad message must not kill the loop
            log.warning("dispatch failed: %s", e)
            reply = "Something went wrong answering that. Try /help."
        if reply:
            client.send_message(chat_id, reply)
    return offset


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN is not set — create a bot with @BotFather and export it.",
            file=sys.stderr,
        )
        sys.exit(2)
    api_url = os.environ.get("COPILOT_API_URL", "http://localhost:8000")
    client = TelegramClient(token)
    fetch = make_fetcher(api_url)
    log.info("bot polling (api=%s)", api_url)
    offset: int | None = None
    while True:
        try:
            offset = poll_once(client, fetch, offset)
        except KeyboardInterrupt:
            log.info("stopping")
            break
        except Exception as e:  # noqa: BLE001 - transient network/API errors: back off
            log.warning("poll error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
