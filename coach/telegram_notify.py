"""Send the daily update to Telegram."""

from __future__ import annotations

import requests

API = "https://api.telegram.org/bot{token}/{method}"


def get_chat_id(token: str) -> str | None:
    """Discover the chat id from recent updates (message the bot once first)."""
    try:
        resp = requests.get(API.format(token=token, method="getUpdates"), timeout=20)
        resp.raise_for_status()
        results = resp.json().get("result", [])
    except (requests.RequestException, ValueError):
        return None
    for update in reversed(results):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    return None


def send_message(token: str, chat_id: str, text: str) -> None:
    """Send a plain-text message (handles Telegram's 4096-char limit)."""
    for chunk_start in range(0, len(text), 4000):
        chunk = text[chunk_start : chunk_start + 4000]
        resp = requests.post(
            API.format(token=token, method="sendMessage"),
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=20,
        )
        resp.raise_for_status()
