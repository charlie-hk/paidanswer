"""Fakes used by the tests, the demo and the resilience scorecard. No network is ever used."""
from __future__ import annotations

import io
from typing import Callable, Dict, List, Optional, Tuple
from wsgiref.util import setup_testing_defaults

from .models import PaymentLink

ANSWER = "The answer is 42."


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeChannel:
    """Records what was sent. `fail_texts` maps an exact text to the number of times sending it should fail."""

    def __init__(self, name: str = "fake", fail_texts: Optional[Dict[str, int]] = None) -> None:
        self.name = name
        self.sent: List[Tuple[str, str]] = []
        self.voices: List[Tuple[str, bytes]] = []
        self.fail_texts = dict(fail_texts or {})

    def send_text(self, chat_id: str, text: str) -> None:
        left = self.fail_texts.get(text, 0)
        if left > 0:
            self.fail_texts[text] = left - 1
            raise RuntimeError("network error while sending")
        self.sent.append((chat_id, text))

    def send_voice(self, chat_id: str, audio: bytes, mime: str = "audio/ogg") -> None:
        self.voices.append((chat_id, audio))

    def download_media(self, media_ref: str, mime: Optional[str] = None) -> Tuple[bytes, str]:
        return b"fake-audio", mime or "audio/ogg"

    def texts_to(self, chat_id: str) -> List[str]:
        return [t for c, t in self.sent if c == chat_id]

    def count(self, text: str) -> int:
        return sum(1 for _, t in self.sent if t == text)


class FakePayments:
    def __init__(self, fail_refunds: int = 0, fail_links: int = 0) -> None:
        self.links: List[str] = []
        self.refunded: Dict[str, int] = {}
        self.refund_calls = 0
        self.fail_refunds, self.fail_links = fail_refunds, fail_links

    def create_link(self, order_id: str, amount: int, currency: str, description: str) -> PaymentLink:
        if self.fail_links > 0:
            self.fail_links -= 1
            raise RuntimeError("payment provider down")
        self.links.append(order_id)
        return PaymentLink(id=f"plink_{len(self.links)}", url=f"https://pay.example/{order_id}")

    def refund(self, payment_id: str, amount: int, order_id: str) -> str:
        self.refund_calls += 1
        if self.fail_refunds > 0:
            self.fail_refunds -= 1
            raise RuntimeError("refund API unavailable")
        self.refunded.setdefault(payment_id, amount)  # idempotent per payment, like the real adapter
        return "rfnd_fake"


class FakeLLM:
    """`script` items: 'ok', 'fail', or an exception/str to return. After the script ends, `default` applies."""

    def __init__(self, script: Optional[List[str]] = None, default: str = "ok", name: str = "fake-llm") -> None:
        self.script, self.default, self.name = list(script or []), default, name
        self.calls = 0
        self.last_question: Optional[str] = None

    def answer(self, question: str, system_prompt: str) -> str:
        self.calls += 1
        self.last_question = question
        step = self.script.pop(0) if self.script else self.default
        if step == "fail":
            raise RuntimeError("model unavailable")
        return ANSWER if step == "ok" else step


class FakeSTT:
    def __init__(self, text: str = "what will my week look like") -> None:
        self.text = text

    def transcribe(self, audio: bytes, mime: str) -> str:
        return self.text


class FakeTTS:
    def synthesize(self, text: str) -> Tuple[bytes, str]:
        return b"voice:" + text.encode("utf-8"), "audio/ogg"


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: List[str] = []

    def alert(self, text: str) -> None:
        self.alerts.append(text)


def call_wsgi(app, method: str, path: str, body: bytes = b"", headers: Optional[Dict[str, str]] = None,
              query: str = "") -> Tuple[int, bytes]:
    """Call a WSGI app without a network or a server."""
    environ: Dict[str, object] = {}
    setup_testing_defaults(environ)
    environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                    "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body))})
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v
    seen: Dict[str, str] = {}

    def start_response(status, response_headers, exc_info=None):
        seen["status"] = status

    chunks = app(environ, start_response)
    return int(seen["status"].split()[0]), b"".join(chunks)


class FakeResponse:
    def __init__(self, status: int = 200, data: Optional[dict] = None, content: bytes = b"") -> None:
        self.status_code, self._data, self.content = status, data if data is not None else {}, content
        self.text = str(self._data)

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Records requests and returns queued responses (a `requests.Session` stand-in)."""

    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.calls: List[Tuple[str, str, dict]] = []

    def _next(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else FakeResponse(200, {"ok": True})

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._next("POST", url, kwargs)

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._next("GET", url, kwargs)
