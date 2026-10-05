"""Optional voice features: text to speech (replies) and speech to text (voice questions)."""
from __future__ import annotations

import base64
from typing import Optional, Tuple

import requests


class GoogleTTS:
    """Google Cloud Text-to-Speech, OGG/Opus output (accepted as a voice note by Telegram and WhatsApp)."""

    def __init__(self, api_key: str, language: str = "en-US", voice: Optional[str] = None,
                 timeout: float = 25, http=None) -> None:
        self.api_key, self.language, self.voice, self.timeout = api_key, language, voice, timeout
        self.http = http or requests.Session()

    def synthesize(self, text: str) -> Tuple[bytes, str]:
        voice = {"languageCode": self.language}
        if self.voice:
            voice["name"] = self.voice
        r = self.http.post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            params={"key": self.api_key},
            json={"input": {"text": text[:4000]}, "voice": voice, "audioConfig": {"audioEncoding": "OGG_OPUS"}},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return base64.b64decode(r.json()["audioContent"]), "audio/ogg"


class GeminiSTT:
    """Transcribe a voice note by sending the audio to a Gemini model."""

    def __init__(self, api_key: str, model: str, timeout: float = 40, http=None) -> None:
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.http = http or requests.Session()

    def transcribe(self, audio: bytes, mime: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {"contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode("ascii")}},
            {"text": "Transcribe this audio. Reply with the transcript only."},
        ]}]}
        r = self.http.post(url, params={"key": self.api_key}, json=body, timeout=self.timeout)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
