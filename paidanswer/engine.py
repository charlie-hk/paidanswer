"""The engine: question -> payment link -> verified payment -> answer -> delivery, or refund.

Guarantee: once a payment is recorded, the order ends as `delivered` or `refunded`.
Work is driven by the database (not by memory), so a crash or restart never loses a paid order.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

from .models import Inbound
from .notify import LogNotifier
from .settings import Settings, money
from .store import Order, Store, ST_ANSWERED, ST_PAID, ST_REFUND_PENDING

log = logging.getLogger("paidanswer")
GREETINGS = {"/start", "start", "hi", "hello", "hey"}


class Engine:
    def __init__(self, settings: Settings, store: Store, channels: Dict[str, object], payments, llm,
                 tts=None, stt=None, notifier=None, clock: Callable[[], float] = time.time) -> None:
        self.s, self.store, self.channels = settings, store, channels
        self.payments, self.llm, self.tts, self.stt = payments, llm, tts, stt
        self.notifier = notifier or LogNotifier()
        self.clock = clock

    # ------------------------------------------------------------- helpers
    def _say(self, channel, chat_id: str, text: str) -> bool:
        """Best-effort message to the user (never raises)."""
        try:
            channel.send_text(chat_id, text)
            return True
        except Exception as exc:
            log.warning("could not message %s: %r", chat_id, exc)
            return False

    def _alert(self, text: str) -> None:
        try:
            self.notifier.alert(text)
        except Exception as exc:
            log.error("alert failed: %r", exc)

    def _backoff(self, attempt_number: int) -> float:
        b = self.s.backoff
        return float(b[min(max(attempt_number, 1) - 1, len(b) - 1)])

    # ------------------------------------------------------------- inbound
    def on_message(self, msg: Inbound) -> None:
        ch = self.channels.get(msg.channel)
        if ch is None:
            log.warning("message for an unconfigured channel: %s", msg.channel)
            return
        now = self.clock()
        text = (msg.text or "").strip()
        if text.lower() in GREETINGS:
            self._say(ch, msg.chat_id,
                      f"Welcome to {self.s.brand}. Send your question as text or a voice note. "
                      f"Each answer costs {money(self.s.base_price * 100, self.s.currency)}; you pay before the "
                      f"answer is prepared, and you are refunded automatically if we cannot deliver it.")
            return
        if not text and not msg.media_ref:
            self._say(ch, msg.chat_id, "Please send your question as a text or voice message.")
            return
        open_order = self.store.find_open_order(msg.channel, msg.chat_id, now, self.s.open_order_max_age)
        if open_order is not None:
            self._say(ch, msg.chat_id,
                      f"You already have a question waiting for payment. Pay here to get your answer: {open_order.link_url}")
            return
        amount = self.s.amount_for(text)
        order = self.store.create_order(msg.channel, msg.chat_id, text or None, msg.media_ref, msg.media_mime,
                                        amount, self.s.currency, now)
        try:
            link = self.payments.create_link(order.id, amount, self.s.currency, f"{self.s.brand} answer")
        except Exception as exc:
            self.store.delete_unpaid(order.id)
            self._alert(f"Could not create a payment link: {exc!r}")
            self._say(ch, msg.chat_id, "Sorry, something went wrong on our side. Please try again in a minute. "
                                       "You have not been charged.")
            return
        self.store.set_link(order.id, link.id, link.url, now)
        self._say(ch, msg.chat_id,
                  f"Your question is saved. Pay {money(amount, self.s.currency)} to get your answer: {link.url}")

    def on_payment(self, order_id: str, payment_id: str, amount: int, currency: str) -> str:
        """Handle a verified 'paid' event. Idempotent: safe to call any number of times for the same payment."""
        now = self.clock()
        order = self.store.get(order_id)
        if order is None:
            self._alert(f"Payment {payment_id} refers to an unknown order {order_id}. Check it by hand.")
            return "unknown"
        if int(amount) != order.amount or currency.upper() != order.currency.upper():
            self._refund_stray(payment_id, int(amount), order_id,
                               f"amount mismatch (expected {order.amount} {order.currency}, got {amount} {currency})")
            return "amount_mismatch"
        result = self.store.mark_paid(order_id, payment_id, now, self.s.deadline_seconds)
        if result == "ok":
            ch = self.channels.get(order.channel)
            if ch is not None:
                self._say(ch, order.chat_id, "Payment received. Preparing your answer now.")
            return "ok"
        if result == "already":
            return "already"
        if result == "reused":
            self._alert(f"Payment {payment_id} is already attached to another order; ignored for order {order_id}.")
            return "reused"
        self._refund_stray(payment_id, int(amount), order_id, "second payment for an order that is already paid")
        return "extra_refunded"

    def _refund_stray(self, payment_id: str, amount: int, order_id: str, why: str) -> None:
        """A payment that must not be fulfilled (wrong amount, duplicate). Refund it and tell the admin."""
        try:
            self.payments.refund(payment_id, amount, order_id)
            self._alert(f"Payment {payment_id} refunded automatically: {why}.")
        except Exception as exc:
            self._alert(f"Payment {payment_id} needs a MANUAL refund ({why}); automatic refund failed: {exc!r}")

    # -------------------------------------------------------------- worker
    def tick(self) -> bool:
        """Do one unit of due work. Returns False when nothing is due."""
        now = self.clock()
        order = self.store.claim_due(now, self.s.lease_seconds)
        if order is None:
            return False
        try:
            if order.status == ST_PAID:
                self._do_answer(order, now)
            elif order.status == ST_ANSWERED:
                self._do_deliver(order, now)
            elif order.status == ST_REFUND_PENDING:
                self._do_refund(order, now)
        except Exception as exc:  # last line of defence: back off and try again later
            log.exception("unexpected error on order %s", order.id)
            self.store.fail_attempt(order.id, f"unexpected: {exc!r}"[:300], now + self._backoff(order.attempts + 1), now)
        return True

    def run_pending(self, max_items: int = 200) -> int:
        done = 0
        while done < max_items and self.tick():
            done += 1
        return done

    def _question_text(self, order: Order) -> str:
        if order.question:
            return order.question
        if order.media_ref:
            if self.stt is None:
                raise RuntimeError("voice questions are not enabled")
            audio, mime = self.channels[order.channel].download_media(order.media_ref, order.media_mime)
            text = self.stt.transcribe(audio, mime).strip()
            if not text:
                raise RuntimeError("could not transcribe the voice message")
            self.store.set_question(order.id, text)
            return text
        raise RuntimeError("order has no question")

    def _clip(self, answer: str) -> str:
        answer = answer.strip()
        limit = self.s.max_answer_chars
        if len(answer) <= limit:
            return answer
        cut = answer[:limit]
        end = max(cut.rfind(". "), cut.rfind("\n"))
        return (cut[:end + 1] if end > limit * 0.6 else cut.rstrip()) + " ..."

    def _do_answer(self, order: Order, now: float) -> None:
        if now >= (order.deadline_at or 0):
            self._start_refund(order, "deadline reached before an answer was ready", now)
            return
        try:
            question = self._question_text(order)
            answer = self.llm.answer(question, self.s.system_prompt)
            if not answer or not answer.strip():
                raise RuntimeError("empty answer")
        except Exception as exc:
            log.warning("answer failed for order %s: %r", order.id, exc)
            self._answer_failed(order, now, exc)
            return
        self.store.record_answer(order.id, self._clip(answer), now)

    def _answer_failed(self, order: Order, now: float, exc: Exception) -> None:
        deadline = order.deadline_at or now
        next_at = min(now + self._backoff(order.attempts + 1), deadline)
        attempts = self.store.fail_attempt(order.id, str(exc)[:300], next_at, now)
        if self.store.set_delay_notified(order.id):
            ch = self.channels.get(order.channel)
            if ch is not None:
                self._say(ch, order.chat_id,
                          "Your answer is taking longer than usual. We are still working on it. If we cannot deliver "
                          "it in time, your payment is refunded automatically.")
            self._alert(f"Order {order.id[:8]}: the AI did not answer (attempt {attempts}): {exc}")

    def _do_deliver(self, order: Order, now: float) -> None:
        ch = self.channels.get(order.channel)
        try:
            if ch is None:
                raise RuntimeError(f"channel {order.channel} is not configured")
            ch.send_text(order.chat_id, order.answer or "")
        except Exception as exc:
            log.warning("delivery failed for order %s: %r", order.id, exc)
            if now >= (order.deadline_at or 0):
                self._start_refund(order, f"could not deliver the answer: {exc}", now)
                return
            deadline = order.deadline_at or now
            next_at = min(now + self._backoff(order.attempts + 1), deadline)
            attempts = self.store.fail_attempt(order.id, str(exc)[:300], next_at, now)
            if attempts == 1:
                self._alert(f"Order {order.id[:8]}: delivery to the user failed: {exc}")
            return
        if not self.store.mark_delivered(order.id, now):
            return
        if self.s.voice_replies and self.tts is not None:
            try:
                audio, mime = self.tts.synthesize(order.answer or "")
                ch.send_voice(order.chat_id, audio, mime)
            except Exception as exc:  # the text answer is already delivered; voice is a bonus
                log.warning("voice reply failed for order %s: %r", order.id, exc)

    def _start_refund(self, order: Order, reason: str, now: float) -> None:
        if self.store.to_refund(order.id, reason[:300], now):
            self._alert(f"Order {order.id[:8]} moved to refund: {reason}")

    def _do_refund(self, order: Order, now: float) -> None:
        try:
            self.payments.refund(order.payment_id or "", order.amount, order.id)
        except Exception as exc:
            attempts = self.store.fail_attempt(order.id, f"refund failed: {exc}"[:300],
                                               now + self._backoff(order.attempts + 1), now)
            if attempts == 1 or attempts % 5 == 0:
                self._alert(f"Order {order.id[:8]}: REFUND FAILED (attempt {attempts}), still retrying: {exc}")
            return
        if self.store.mark_refunded(order.id, now):
            ch = self.channels.get(order.channel)
            if ch is not None:
                self._say(ch, order.chat_id,
                          f"Sorry, we could not deliver your answer. Your payment of "
                          f"{money(order.amount, order.currency)} has been refunded. It can take a few business days "
                          f"to appear in your account.")
            self._alert(f"Order {order.id[:8]} refunded.")


class Worker(threading.Thread):
    """Background loop that keeps calling Engine.run_pending()."""

    def __init__(self, engine: Engine, interval: float = 1.0) -> None:
        super().__init__(name="paidanswer-worker", daemon=True)
        self.engine, self.interval = engine, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                busy = self.engine.run_pending()
            except Exception:
                log.exception("worker loop error")
                busy = 0
            if not busy:
                self._stop_event.wait(self.interval)

    def stop(self) -> None:
        self._stop_event.set()
