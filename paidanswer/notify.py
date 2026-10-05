from __future__ import annotations

import logging

log = logging.getLogger("paidanswer")


class LogNotifier:
    """Default admin alerts: write to the log."""

    def alert(self, text: str) -> None:
        log.warning("ALERT: %s", text)


class TelegramNotifier:
    """Send admin alerts to a Telegram chat; falls back to the log if sending fails."""

    def __init__(self, channel, chat_id: str) -> None:
        self.channel = channel
        self.chat_id = chat_id

    def alert(self, text: str) -> None:
        log.warning("ALERT: %s", text)
        try:
            self.channel.send_text(self.chat_id, "ALERT: " + text[:3500])
        except Exception as exc:  # alerts must never break order processing
            log.error("could not send the alert to Telegram: %r", exc)
