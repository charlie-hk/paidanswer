"""Channel adapters (Telegram, WhatsApp Cloud API) and parsers for their webhook payloads."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import requests

from .models import Inbound


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, timeout: float = 20, http=None) -> None:
        self.token, self.timeout = token, timeout
        self.http = http or requests.Session()

    def _call(self, method: str, **kwargs: Any) -> Dict[str, Any]:
        r = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", timeout=self.timeout, **kwargs)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: HTTP {r.status_code} {str(data)[:200]}")
        return data

    def send_text(self, chat_id: str, text: str) -> None:
        self._call("sendMessage", json={"chat_id": chat_id, "text": text[:4096]})

    def send_voice(self, chat_id: str, audio: bytes, mime: str = "audio/ogg") -> None:
        self._call("sendVoice", data={"chat_id": chat_id}, files={"voice": ("reply.ogg", audio, mime)})

    def download_media(self, file_id: str, mime: Optional[str] = None) -> Tuple[bytes, str]:
        info = self._call("getFile", json={"file_id": file_id})
        path = info["result"]["file_path"]
        r = self.http.get(f"https://api.telegram.org/file/bot{self.token}/{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.content, mime or "audio/ogg"


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, api_version: str, timeout: float = 20, http=None) -> None:
        self.token, self.phone_number_id, self.timeout = token, phone_number_id, timeout
        self.base = f"https://graph.facebook.com/{api_version}"
        self.http = http or requests.Session()

    @property
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _post_message(self, payload: Dict[str, Any]) -> None:
        r = self.http.post(f"{self.base}/{self.phone_number_id}/messages", headers=self._auth, json=payload,
                           timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"whatsapp send failed: HTTP {r.status_code} {r.text[:200]}")

    def send_text(self, chat_id: str, text: str) -> None:
        self._post_message({"messaging_product": "whatsapp", "recipient_type": "individual", "to": chat_id,
                            "type": "text", "text": {"body": text[:4096]}})

    def send_voice(self, chat_id: str, audio: bytes, mime: str = "audio/ogg") -> None:
        r = self.http.post(f"{self.base}/{self.phone_number_id}/media", headers=self._auth,
                           data={"messaging_product": "whatsapp", "type": mime},
                           files={"file": ("reply.ogg", audio, mime)}, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"whatsapp media upload failed: HTTP {r.status_code} {r.text[:200]}")
        self._post_message({"messaging_product": "whatsapp", "recipient_type": "individual", "to": chat_id,
                            "type": "audio", "audio": {"id": r.json()["id"]}})

    def download_media(self, media_id: str, mime: Optional[str] = None) -> Tuple[bytes, str]:
        meta = self.http.get(f"{self.base}/{media_id}", headers=self._auth, timeout=self.timeout)
        meta.raise_for_status()
        info = meta.json()
        r = self.http.get(info["url"], headers=self._auth, timeout=self.timeout)
        r.raise_for_status()
        return r.content, info.get("mime_type") or mime or "audio/ogg"


def parse_telegram_update(update: Dict[str, Any]) -> Optional[Inbound]:
    msg = update.get("message")
    if not isinstance(msg, dict) or "chat" not in msg:
        return None
    voice = msg.get("voice") or msg.get("audio")
    return Inbound(
        channel="telegram",
        chat_id=str(msg["chat"]["id"]),
        text=msg.get("text"),
        media_ref=voice.get("file_id") if isinstance(voice, dict) else None,
        media_mime=voice.get("mime_type") if isinstance(voice, dict) else None,
        message_id=str(update.get("update_id")),
    )


def parse_whatsapp_payload(payload: Dict[str, Any]) -> List[Inbound]:
    out: List[Inbound] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for m in (change.get("value", {}) or {}).get("messages", []) or []:
                kind = m.get("type")
                text = media = mime = None
                if kind == "text":
                    text = (m.get("text") or {}).get("body")
                elif kind == "audio":
                    media = (m.get("audio") or {}).get("id")
                    mime = (m.get("audio") or {}).get("mime_type")
                else:
                    continue
                out.append(Inbound(channel="whatsapp", chat_id=str(m.get("from")), text=text, media_ref=media,
                                   media_mime=mime, message_id=m.get("id")))
    return out
