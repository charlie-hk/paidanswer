from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Inbound:
    """A message received from a user, normalised across channels."""
    channel: str
    chat_id: str
    text: Optional[str] = None
    media_ref: Optional[str] = None      # voice note id (Telegram file_id / WhatsApp media id)
    media_mime: Optional[str] = None
    message_id: Optional[str] = None


@dataclass(frozen=True)
class PaymentLink:
    id: str
    url: str
