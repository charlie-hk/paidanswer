"""A small WSGI app: the three webhooks (Telegram, WhatsApp, Razorpay) plus health and admin endpoints.

Stdlib only, so it runs under `python -m paidanswer serve` or any WSGI server (gunicorn, waitress).
"""
from __future__ import annotations

import hmac
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from .channels import parse_telegram_update, parse_whatsapp_payload
from .engine import Engine
from .payments import verify_hmac_sha256
from .store import Store

log = logging.getLogger("paidanswer")
MAX_BODY = 1_000_000

Response = Tuple[int, str, bytes]


def _json(status: int, data: object) -> Response:
    return status, "application/json", json.dumps(data).encode("utf-8")


def _text(status: int, text: str) -> Response:
    return status, "text/plain; charset=utf-8", text.encode("utf-8")


class WebApp:
    def __init__(self, engine: Engine, store: Store, razorpay_webhook_secret: str,
                 telegram_secret: Optional[str] = None, whatsapp_verify_token: Optional[str] = None,
                 whatsapp_app_secret: Optional[str] = None, admin_token: Optional[str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.engine, self.store, self.clock = engine, store, clock
        self.razorpay_secret = razorpay_webhook_secret
        self.telegram_secret = telegram_secret
        self.wa_verify_token = whatsapp_verify_token
        self.wa_app_secret = whatsapp_app_secret
        self.admin_token = admin_token

    # ------------------------------------------------------------------ WSGI
    def __call__(self, environ, start_response):
        try:
            status, ctype, body = self._route(environ)
        except Exception:
            log.exception("unhandled error in %s", environ.get("PATH_INFO"))
            status, ctype, body = _json(500, {"error": "internal error"})
        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                   405: "Method Not Allowed", 413: "Payload Too Large", 500: "Internal Server Error"}
        start_response(f"{status} {reasons.get(status, 'OK')}", [("Content-Type", ctype),
                                                                   ("Content-Length", str(len(body)))])
        return [body]

    @staticmethod
    def _body(environ) -> Optional[bytes]:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        return environ["wsgi.input"].read(length) if length else b""

    @staticmethod
    def _header(environ, name: str) -> str:
        return environ.get("HTTP_" + name.upper().replace("-", "_"), "")

    def _route(self, environ) -> Response:
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "/")
        if path == "/healthz" and method == "GET":
            return _json(200, {"ok": True, "orders": self.store.stats()})
        if path == "/admin/orders" and method == "GET":
            return self._admin(environ)
        if path == "/webhooks/telegram" and method == "POST":
            return self._telegram(environ)
        if path == "/webhooks/whatsapp":
            if method == "GET":
                return self._whatsapp_verify(environ)
            if method == "POST":
                return self._whatsapp(environ)
        if path == "/webhooks/razorpay" and method == "POST":
            return self._razorpay(environ)
        if path in ("/healthz", "/admin/orders", "/webhooks/telegram", "/webhooks/whatsapp", "/webhooks/razorpay"):
            return _json(405, {"error": "method not allowed"})
        return _json(404, {"error": "not found"})

    # -------------------------------------------------------------- Telegram
    def _telegram(self, environ) -> Response:
        if not self.telegram_secret or not hmac.compare_digest(
                self._header(environ, "X-Telegram-Bot-Api-Secret-Token"), self.telegram_secret):
            return _json(403, {"error": "bad secret token"})
        raw = self._body(environ)
        if raw is None:
            return _json(413, {"error": "body too large"})
        try:
            update = json.loads(raw)
        except ValueError:
            return _json(400, {"error": "invalid json"})
        inbound = parse_telegram_update(update) if isinstance(update, dict) else None
        if inbound is None:
            return _json(200, {"ok": True, "ignored": True})
        # Inbound chat messages are at-most-once: losing one only means the user asks again.
        if not self.store.mark_event_seen(f"tg:{update.get('update_id')}", self.clock()):
            return _json(200, {"ok": True, "duplicate": True})
        try:
            self.engine.on_message(inbound)
        except Exception:
            log.exception("failed to handle a Telegram message")
        return _json(200, {"ok": True})

    # -------------------------------------------------------------- WhatsApp
    def _whatsapp_verify(self, environ) -> Response:
        q = parse_qs(environ.get("QUERY_STRING", ""))
        mode = (q.get("hub.mode") or [""])[0]
        token = (q.get("hub.verify_token") or [""])[0]
        challenge = (q.get("hub.challenge") or [""])[0]
        if (mode == "subscribe" and self.wa_verify_token and hmac.compare_digest(token, self.wa_verify_token)):
            return _text(200, challenge)
        return _text(403, "forbidden")

    def _whatsapp(self, environ) -> Response:
        raw = self._body(environ)
        if raw is None:
            return _json(413, {"error": "body too large"})
        if not self.wa_app_secret or not verify_hmac_sha256(raw, self._header(environ, "X-Hub-Signature-256"),
                                                            self.wa_app_secret, prefix="sha256="):
            return _json(401, {"error": "bad signature"})
        try:
            payload = json.loads(raw)
        except ValueError:
            return _json(400, {"error": "invalid json"})
        for inbound in parse_whatsapp_payload(payload if isinstance(payload, dict) else {}):
            if inbound.message_id and not self.store.mark_event_seen(f"wa:{inbound.message_id}", self.clock()):
                continue
            try:
                self.engine.on_message(inbound)
            except Exception:
                log.exception("failed to handle a WhatsApp message")
        return _json(200, {"ok": True})

    # -------------------------------------------------------------- Razorpay
    def _razorpay(self, environ) -> Response:
        raw = self._body(environ)
        if raw is None:
            return _json(413, {"error": "body too large"})
        if not verify_hmac_sha256(raw, self._header(environ, "X-Razorpay-Signature"), self.razorpay_secret):
            return _json(401, {"error": "bad signature"})
        try:
            payload = json.loads(raw)
            event = payload.get("event")
            if event != "payment_link.paid":
                return _json(200, {"ok": True, "ignored": event})
            body = payload["payload"]
            order_id = body["payment_link"]["entity"]["reference_id"]
            pay = body["payment"]["entity"]
            payment_id, amount, currency = pay["id"], int(pay["amount"]), pay["currency"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return _json(400, {"error": "unexpected payload"})
        event_key = "rzp:" + (self._header(environ, "X-Razorpay-Event-Id") or f"{event}:{payment_id}")
        if self.store.event_seen(event_key):
            return _json(200, {"ok": True, "duplicate": True})
        # Payments are at-least-once: handle first (idempotent), record the event only after success.
        # If this raises, the app answers 500 and Razorpay retries the webhook.
        result = self.engine.on_payment(order_id, payment_id, amount, currency)
        self.store.mark_event_seen(event_key, self.clock())
        return _json(200, {"ok": True, "result": result})

    # ----------------------------------------------------------------- admin
    def _admin(self, environ) -> Response:
        auth = self._header(environ, "Authorization")
        if not self.admin_token or not hmac.compare_digest(auth, f"Bearer {self.admin_token}"):
            return _json(401, {"error": "unauthorized"})
        orders = [{"id": o.id[:8], "channel": o.channel, "status": o.status, "amount": o.amount,
                   "currency": o.currency, "attempts": o.attempts, "last_error": o.last_error,
                   "paid_at": o.paid_at, "deadline_at": o.deadline_at} for o in self.store.all_orders(50)]
        return _json(200, {"stats": self.store.stats(), "orders": orders})
