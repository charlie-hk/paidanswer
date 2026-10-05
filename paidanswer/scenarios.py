"""Failure scenarios. Each one runs the real engine against fakes and checks the guarantees.

Used by the unit tests, by `paidanswer resilience` (the scorecard) and by `paidanswer demo`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .engine import Engine
from .llm import LLMChain
from .models import Inbound
from .settings import Settings
from .store import Order, Store
from .testing import (ANSWER, FakeChannel, FakeClock, FakeLLM, FakeNotifier, FakePayments, FakeSTT, FakeTTS,
                      call_wsgi)
from .web import WebApp


@dataclass
class Check:
    label: str
    ok: bool


@dataclass
class ScenarioResult:
    key: str
    title: str
    group: str
    checks: List[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


class Harness:
    """Engine + fakes + a fake clock + the web app, wired together."""

    RZP, TG, WA_VERIFY, WA_APP = "rzp_secret", "tg_secret", "wa_verify", "wa_app_secret"

    def __init__(self, llm=None, channel=None, payments=None, settings: Optional[Settings] = None,
                 stt=None, tts=None) -> None:
        self.clock = FakeClock()
        self.settings = settings or Settings()
        self.store = Store(":memory:")
        self.channel = channel or FakeChannel()
        self.payments = payments or FakePayments()
        self.llm = llm or FakeLLM()
        self.notifier = FakeNotifier()
        channels = {"fake": self.channel, "telegram": self.channel, "whatsapp": self.channel}
        self.engine = Engine(self.settings, self.store, channels, self.payments, self.llm, tts=tts, stt=stt,
                             notifier=self.notifier, clock=self.clock)
        self.web = WebApp(self.engine, self.store, self.RZP, telegram_secret=self.TG,
                          whatsapp_verify_token=self.WA_VERIFY, whatsapp_app_secret=self.WA_APP,
                          admin_token="admin-token", clock=self.clock)
        self._pay_n = 0

    def ask(self, text: Optional[str] = "What will my week look like?", chat: str = "u1",
            media_ref: Optional[str] = None) -> Order:
        self.engine.on_message(Inbound("fake", chat, text, media_ref, None, None))
        order = self.store.find_open_order("fake", chat, self.clock(), 10 ** 9)
        assert order is not None, "no order was created"
        return order

    def next_payment_id(self) -> str:
        self._pay_n += 1
        return f"pay_{self._pay_n}"

    def pay(self, order: Order, payment_id: Optional[str] = None, amount: Optional[int] = None) -> str:
        return self.engine.on_payment(order.id, payment_id or self.next_payment_id(),
                                      order.amount if amount is None else amount, order.currency)

    def run(self, seconds: float, step: float = 5) -> None:
        self.engine.run_pending()
        elapsed = 0.0
        while elapsed < seconds:
            self.clock.advance(step)
            elapsed += step
            self.engine.run_pending()

    def order(self, order_id: str) -> Order:
        o = self.store.get(order_id)
        assert o is not None
        return o

    def razorpay_event(self, order: Order, payment_id: str, event_id: Optional[str] = None,
                       signature: Optional[str] = None, amount: Optional[int] = None):
        body = json.dumps({"event": "payment_link.paid", "payload": {
            "payment_link": {"entity": {"reference_id": order.id}},
            "payment": {"entity": {"id": payment_id, "amount": order.amount if amount is None else amount,
                                   "currency": order.currency}}}}).encode()
        sig = signature if signature is not None else hmac.new(self.RZP.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Razorpay-Signature": sig}
        if event_id:
            headers["X-Razorpay-Event-Id"] = event_id
        return call_wsgi(self.web, "POST", "/webhooks/razorpay", body, headers)


class S:
    def __init__(self, key: str, title: str, group: str) -> None:
        self.r = ScenarioResult(key, title, group)

    def check(self, label: str, ok: object) -> None:
        self.r.checks.append(Check(label, bool(ok)))


def invariants(h: Harness, s: S) -> ScenarioResult:
    orders = h.store.all_orders(1000)
    delivered = sum(1 for o in orders if o.status == "delivered")
    s.check("every paid order ends as delivered or refunded", not h.store.invariant_violations())
    s.check("the answer is delivered exactly once per delivered order", h.channel.count(ANSWER) == delivered)
    s.check("no order is both delivered and refunded",
            not any(o.status == "delivered" and o.payment_id in h.payments.refunded for o in orders))
    return s.r


G_CORE, G_AI, G_DELIVERY, G_MONEY, G_WEB = "Core flow", "AI failures", "Delivery", "Money safety", "Webhook security"


# --------------------------------------------------------------------------- core flow
def happy_path() -> ScenarioResult:
    h, s = Harness(), S("happy_path", "Pay, answer, delivered", G_CORE)
    o = h.ask()
    s.check("a payment link is sent to the user", any("https://pay.example/" in t for t in h.channel.texts_to("u1")))
    h.pay(o)
    h.run(10)
    s.check("the order is delivered", h.order(o.id).status == "delivered")
    s.check("the user got a payment receipt and the answer", "Payment received. Preparing your answer now." in
            h.channel.texts_to("u1") and h.channel.count(ANSWER) == 1)
    s.check("no refund and no admin alert", not h.payments.refunded and not h.notifier.alerts)
    return invariants(h, s)


def premium_price() -> ScenarioResult:
    h, s = Harness(), S("premium_price", "Premium questions get the premium price", G_CORE)
    normal = h.ask("What is my lucky colour?", chat="u1")
    premium = h.ask("Tell me about my health this year", chat="u2")
    s.check("a normal question costs the base price", normal.amount == 9900)
    s.check("a premium question costs the premium price", premium.amount == 25800)
    return invariants(h, s)


def one_open_order_per_user() -> ScenarioResult:
    h, s = Harness(), S("open_order", "A user cannot pile up unpaid payment links", G_CORE)
    h.ask("First question")
    h.engine.on_message(Inbound("fake", "u1", "Second question", None, None, None))
    s.check("only one payment link was created", len(h.payments.links) == 1)
    s.check("the user is reminded of the pending link",
            any("already have a question" in t for t in h.channel.texts_to("u1")))
    return invariants(h, s)


def voice_question() -> ScenarioResult:
    h, s = Harness(stt=FakeSTT("what will my week look like")), S("voice_question", "Voice questions are transcribed after payment", G_CORE)
    o = h.ask("", media_ref="voice-1")
    s.check("nothing is transcribed before payment", h.llm.calls == 0 and h.order(o.id).question is None)
    h.pay(o)
    h.run(10)
    s.check("the model received the transcript", h.llm.last_question == "what will my week look like")
    s.check("the order is delivered", h.order(o.id).status == "delivered")
    return invariants(h, s)


def voice_reply() -> ScenarioResult:
    settings = Settings(voice_replies=True)
    h, s = Harness(settings=settings, tts=FakeTTS()), S("voice_reply", "Voice note is sent after the text answer", G_CORE)
    o = h.ask()
    h.pay(o)
    h.run(10)
    s.check("the text answer is delivered", h.order(o.id).status == "delivered" and h.channel.count(ANSWER) == 1)
    s.check("a voice note follows", len(h.channel.voices) == 1)
    return invariants(h, s)


# --------------------------------------------------------------------------- AI failures
def retry_then_success() -> ScenarioResult:
    h, s = Harness(llm=FakeLLM(["fail", "fail", "ok"])), S("retry_then_success", "AI fails twice, then answers", G_AI)
    o = h.ask()
    h.pay(o)
    h.run(30)
    s.check("the order is delivered after retries", h.order(o.id).status == "delivered")
    s.check("the model was called three times", h.llm.calls == 3)
    s.check("the user got exactly one 'taking longer' notice",
            sum("taking longer" in t for t in h.channel.texts_to("u1")) == 1)
    s.check("the admin was alerted once", len(h.notifier.alerts) == 1)
    s.check("no refund was issued", not h.payments.refunded)
    return invariants(h, s)


def fallback_provider() -> ScenarioResult:
    primary, secondary = FakeLLM(default="fail", name="primary"), FakeLLM(name="secondary")
    h, s = Harness(llm=LLMChain([primary, secondary])), S("fallback_provider", "Primary model down, fallback answers", G_AI)
    o = h.ask()
    h.pay(o)
    h.run(10)
    s.check("the order is delivered", h.order(o.id).status == "delivered")
    s.check("the fallback provider answered", primary.calls == 1 and secondary.calls == 1)
    s.check("the user never noticed a problem", not any("taking longer" in t for t in h.channel.texts_to("u1")))
    return invariants(h, s)


def all_providers_down_refund() -> ScenarioResult:
    h, s = Harness(llm=FakeLLM(default="fail")), S("all_down_refund", "Every model is down: automatic refund", G_AI)
    o = h.ask()
    pid = h.next_payment_id()
    h.pay(o, pid)
    h.run(700)
    final = h.order(o.id)
    s.check("the order ends as refunded", final.status == "refunded")
    s.check("the payment was refunded", pid in h.payments.refunded)
    s.check("no answer was sent", h.channel.count(ANSWER) == 0)
    s.check("the user was told about the delay and the refund",
            any("taking longer" in t for t in h.channel.texts_to("u1")) and
            any("has been refunded" in t for t in h.channel.texts_to("u1")))
    s.check("the admin was alerted", len(h.notifier.alerts) >= 2)
    return invariants(h, s)


def crash_mid_answer() -> ScenarioResult:
    h, s = Harness(), S("crash_mid_answer", "A worker dies after claiming an order", G_AI)
    o = h.ask()
    h.pay(o)
    claimed = h.store.claim_due(h.clock(), h.settings.lease_seconds)  # the worker takes the order, then 'crashes'
    s.check("an order was claimed by the dead worker", claimed is not None and claimed.status == "paid")
    h.run(30)
    s.check("while the lease is valid nobody else touches it", h.llm.calls == 0)
    h.run(60)
    s.check("after the lease expires another worker finishes it", h.order(o.id).status == "delivered")
    s.check("the model was called once", h.llm.calls == 1)
    return invariants(h, s)


# --------------------------------------------------------------------------- delivery
def delivery_flaky() -> ScenarioResult:
    h, s = Harness(channel=FakeChannel(fail_texts={ANSWER: 2})), S("delivery_flaky", "Sending fails twice, then works", G_DELIVERY)
    o = h.ask()
    h.pay(o)
    h.run(30)
    s.check("the order is delivered after retries", h.order(o.id).status == "delivered")
    s.check("the answer arrived once", h.channel.count(ANSWER) == 1)
    s.check("the admin was alerted about the first failure", len(h.notifier.alerts) == 1)
    return invariants(h, s)


def delivery_dead_refund() -> ScenarioResult:
    h, s = Harness(channel=FakeChannel(fail_texts={ANSWER: 10 ** 9})), S("delivery_dead", "Cannot reach the user: refund", G_DELIVERY)
    o = h.ask()
    pid = h.next_payment_id()
    h.pay(o, pid)
    h.run(700)
    s.check("the order ends as refunded", h.order(o.id).status == "refunded")
    s.check("the payment was refunded", pid in h.payments.refunded)
    s.check("the answer was never delivered", h.channel.count(ANSWER) == 0)
    return invariants(h, s)


# --------------------------------------------------------------------------- money safety
def duplicate_payment_events() -> ScenarioResult:
    h, s = Harness(), S("duplicate_payment", "The same payment reported three times", G_MONEY)
    o = h.ask()
    results = [h.pay(o, "pay_dup") for _ in range(3)]
    h.run(10)
    s.check("only the first report is accepted", results == ["ok", "already", "already"])
    s.check("the model ran once", h.llm.calls == 1)
    s.check("the order is delivered once", h.order(o.id).status == "delivered" and h.channel.count(ANSWER) == 1)
    return invariants(h, s)


def refund_api_flaky() -> ScenarioResult:
    h, s = Harness(llm=FakeLLM(default="fail"), payments=FakePayments(fail_refunds=2)), S("refund_flaky", "Refund API fails twice, then works", G_MONEY)
    o = h.ask()
    pid = h.next_payment_id()
    h.pay(o, pid)
    h.run(700)
    s.check("the refund is retried until it succeeds", h.order(o.id).status == "refunded")
    s.check("the refund API was called three times", h.payments.refund_calls == 3)
    s.check("the admin saw the refund trouble", any("REFUND FAILED" in a for a in h.notifier.alerts))
    return invariants(h, s)


def amount_mismatch() -> ScenarioResult:
    h, s = Harness(), S("amount_mismatch", "Wrong amount paid: refunded, not fulfilled", G_MONEY)
    o = h.ask()
    pid = h.next_payment_id()
    result = h.pay(o, pid, amount=o.amount - 100)
    h.run(10)
    s.check("the payment is rejected", result == "amount_mismatch")
    s.check("the payment is refunded", pid in h.payments.refunded)
    s.check("no answer is produced", h.llm.calls == 0 and h.channel.count(ANSWER) == 0)
    s.check("the order is still waiting for a correct payment", h.order(o.id).status == "awaiting_payment")
    return invariants(h, s)


def extra_payment_refunded() -> ScenarioResult:
    h, s = Harness(), S("extra_payment", "A second payment for a paid order", G_MONEY)
    o = h.ask()
    h.pay(o, "pay_first")
    h.run(10)
    result = h.pay(o, "pay_second")
    s.check("the extra payment is detected", result == "extra_refunded")
    s.check("the extra payment is refunded", "pay_second" in h.payments.refunded)
    s.check("the original payment is not refunded", "pay_first" not in h.payments.refunded)
    s.check("the user got one answer", h.channel.count(ANSWER) == 1 and h.order(o.id).status == "delivered")
    return invariants(h, s)


def unknown_order() -> ScenarioResult:
    h, s = Harness(), S("unknown_order", "Payment for an order we do not know", G_MONEY)
    result = h.engine.on_payment("deadbeef", "pay_x", 9900, "INR")
    s.check("it is ignored without crashing", result == "unknown")
    s.check("the admin is told to check it by hand", any("unknown order" in a for a in h.notifier.alerts))
    s.check("nothing is refunded automatically", not h.payments.refunded)
    return invariants(h, s)


# --------------------------------------------------------------------------- webhook security
def razorpay_signature() -> ScenarioResult:
    h, s = Harness(), S("razorpay_signature", "Razorpay webhooks must be signed", G_WEB)
    o = h.ask()
    code_bad, _ = h.razorpay_event(o, "pay_w1", signature="0" * 64)
    s.check("a forged webhook is rejected (401)", code_bad == 401)
    s.check("the order stays unpaid", h.order(o.id).status == "awaiting_payment")
    code_ok, _ = h.razorpay_event(o, "pay_w1")
    h.run(10)
    s.check("a correctly signed webhook is accepted", code_ok == 200 and h.order(o.id).status == "delivered")
    return invariants(h, s)


def razorpay_replay() -> ScenarioResult:
    h, s = Harness(), S("razorpay_replay", "A replayed webhook does nothing", G_WEB)
    o = h.ask()
    first, _ = h.razorpay_event(o, "pay_r1", event_id="evt_1")
    second, body = h.razorpay_event(o, "pay_r1", event_id="evt_1")
    h.run(10)
    s.check("both calls answer 200", first == 200 and second == 200)
    s.check("the replay is recognised as a duplicate", b"duplicate" in body)
    s.check("the model ran once and the user got one answer", h.llm.calls == 1 and h.channel.count(ANSWER) == 1)
    return invariants(h, s)


def telegram_secret() -> ScenarioResult:
    h, s = Harness(), S("telegram_secret", "Telegram webhook needs the secret token", G_WEB)
    update = json.dumps({"update_id": 7, "message": {"chat": {"id": 555}, "text": "Hello there, one question"}}).encode()
    bad, _ = call_wsgi(h.web, "POST", "/webhooks/telegram", update, {"X-Telegram-Bot-Api-Secret-Token": "nope"})
    s.check("a wrong secret is rejected (403)", bad == 403 and not h.payments.links)
    good, _ = call_wsgi(h.web, "POST", "/webhooks/telegram", update, {"X-Telegram-Bot-Api-Secret-Token": h.TG})
    s.check("the right secret is accepted and a link is sent",
            good == 200 and any("https://pay.example/" in t for t in h.channel.texts_to("555")))
    again, body = call_wsgi(h.web, "POST", "/webhooks/telegram", update, {"X-Telegram-Bot-Api-Secret-Token": h.TG})
    s.check("a retried update is ignored", again == 200 and b"duplicate" in body and len(h.payments.links) == 1)
    return invariants(h, s)


def whatsapp_handshake_and_signature() -> ScenarioResult:
    h, s = Harness(), S("whatsapp_signature", "WhatsApp handshake and signed messages", G_WEB)
    ok, body = call_wsgi(h.web, "GET", "/webhooks/whatsapp", query=f"hub.mode=subscribe&hub.verify_token={h.WA_VERIFY}&hub.challenge=12345")
    s.check("the verification handshake returns the challenge", ok == 200 and body == b"12345")
    bad, _ = call_wsgi(h.web, "GET", "/webhooks/whatsapp", query="hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1")
    s.check("a wrong verify token is rejected", bad == 403)
    payload = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": "919800000000", "id": "wamid.1", "type": "text", "text": {"body": "A question for you"}}]}}]}]}).encode()
    unsigned, _ = call_wsgi(h.web, "POST", "/webhooks/whatsapp", payload)
    s.check("an unsigned message is rejected (401)", unsigned == 401 and not h.payments.links)
    sig = "sha256=" + hmac.new(h.WA_APP.encode(), payload, hashlib.sha256).hexdigest()
    signed, _ = call_wsgi(h.web, "POST", "/webhooks/whatsapp", payload, {"X-Hub-Signature-256": sig})
    s.check("a signed message creates a payment link",
            signed == 200 and any("https://pay.example/" in t for t in h.channel.texts_to("919800000000")))
    call_wsgi(h.web, "POST", "/webhooks/whatsapp", payload, {"X-Hub-Signature-256": sig})
    s.check("a retried message is processed once", len(h.payments.links) == 1)
    return invariants(h, s)


def admin_needs_token() -> ScenarioResult:
    h, s = Harness(), S("admin_token", "The admin endpoint is protected", G_WEB)
    open_code, _ = call_wsgi(h.web, "GET", "/admin/orders")
    s.check("no token: 401", open_code == 401)
    ok_code, _ = call_wsgi(h.web, "GET", "/admin/orders", headers={"Authorization": "Bearer admin-token"})
    s.check("with the token: 200", ok_code == 200)
    health, _ = call_wsgi(h.web, "GET", "/healthz")
    s.check("the health check is public", health == 200)
    return invariants(h, s)


SCENARIOS: List[Callable[[], ScenarioResult]] = [
    happy_path, premium_price, one_open_order_per_user, voice_question, voice_reply,
    retry_then_success, fallback_provider, all_providers_down_refund, crash_mid_answer,
    delivery_flaky, delivery_dead_refund,
    duplicate_payment_events, refund_api_flaky, amount_mismatch, extra_payment_refunded, unknown_order,
    razorpay_signature, razorpay_replay, telegram_secret, whatsapp_handshake_and_signature, admin_needs_token,
]


def run_all() -> List[ScenarioResult]:
    return [fn() for fn in SCENARIOS]
