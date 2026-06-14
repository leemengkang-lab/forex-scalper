"""
notifier.py
===========
Heartbeats, fills, and errors. TelegramNotifier uses your bot token (you already
have the integration code); ConsoleNotifier is the no-creds fallback so nothing
breaks in demo. Lazy import of requests keeps the package importable bare.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("notifier")


class Notifier:
    def send(self, text: str) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, text: str) -> None:
        logger.info("NOTIFY | %s", text)


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        try:
            import requests  # lazy
            requests.post(self.url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
        except Exception as e:                      # never let alerts crash the bot
            logger.warning("Telegram send failed: %s", e)
