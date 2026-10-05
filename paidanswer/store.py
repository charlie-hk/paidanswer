"""SQLite state machine for orders.

Every transition is a guarded UPDATE (WHERE status = expected) inside one transaction,
so a transition happens at most once even with duplicate webhooks or several workers.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

ST_AWAITING = "awaiting_payment"
ST_PAID = "paid"
ST_ANSWERED = "answered"
ST_DELIVERED = "delivered"
ST_REFUND_PENDING = "refund_pending"
ST_REFUNDED = "refunded"
TERMINAL_OK = (ST_DELIVERED, ST_REFUNDED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    question TEXT,
    media_ref TEXT,
    media_mime TEXT,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    link_id TEXT,
    link_url TEXT,
    payment_id TEXT UNIQUE,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    paid_at REAL,
    deadline_at REAL,
    answer TEXT,
    last_error TEXT,
    delay_notified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_work ON orders(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_key TEXT PRIMARY KEY,
    received_at REAL NOT NULL
);
"""


@dataclass
class Order:
    id: str
    channel: str
    chat_id: str
    question: Optional[str]
    media_ref: Optional[str]
    media_mime: Optional[str]
    amount: int
    currency: str
    link_id: Optional[str]
    link_url: Optional[str]
    payment_id: Optional[str]
    status: str
    attempts: int
    next_attempt_at: float
    lease_until: float
    paid_at: Optional[float]
    deadline_at: Optional[float]
    answer: Optional[str]
    last_error: Optional[str]
    delay_notified: int
    created_at: float
    updated_at: float


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    @staticmethod
    def _order(row: Optional[sqlite3.Row]) -> Optional[Order]:
        return None if row is None else Order(**{k: row[k] for k in row.keys()})

    # ------------------------------------------------------------------ reads
    def get(self, order_id: str) -> Optional[Order]:
        with self._lock:
            return self._order(self._db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    def all_orders(self, limit: int = 100) -> List[Order]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._order(r) for r in rows]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def find_open_order(self, channel: str, chat_id: str, now: float, max_age: float) -> Optional[Order]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM orders WHERE channel = ? AND chat_id = ? AND status = ? AND link_url IS NOT NULL "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
                (channel, chat_id, ST_AWAITING, now - max_age),
            ).fetchone()
        return self._order(row)

    def invariant_violations(self) -> List[str]:
        """A paid order must end delivered or refunded. Returns violations for orders that should be finished."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, status FROM orders WHERE paid_at IS NOT NULL AND status NOT IN (?, ?)",
                TERMINAL_OK,
            ).fetchall()
        return [f"{r['id']}: {r['status']}" for r in rows]

    # ----------------------------------------------------------------- writes
    def create_order(self, channel: str, chat_id: str, question: Optional[str], media_ref: Optional[str],
                     media_mime: Optional[str], amount: int, currency: str, now: float) -> Order:
        oid = uuid.uuid4().hex
        with self._tx() as db:
            db.execute(
                "INSERT INTO orders (id, channel, chat_id, question, media_ref, media_mime, amount, currency, status, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (oid, channel, str(chat_id), question, media_ref, media_mime, amount, currency, ST_AWAITING, now, now),
            )
        return self.get(oid)  # type: ignore[return-value]

    def set_link(self, order_id: str, link_id: str, link_url: str, now: float) -> None:
        with self._tx() as db:
            db.execute("UPDATE orders SET link_id = ?, link_url = ?, updated_at = ? WHERE id = ?",
                       (link_id, link_url, now, order_id))

    def delete_unpaid(self, order_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM orders WHERE id = ? AND status = ? AND link_id IS NULL", (order_id, ST_AWAITING))

    def set_question(self, order_id: str, question: str) -> None:
        with self._tx() as db:
            db.execute("UPDATE orders SET question = ? WHERE id = ?", (question, order_id))

    def mark_event_seen(self, key: str, now: float) -> bool:
        """True if the key is new, False if it was already recorded."""
        with self._tx() as db:
            cur = db.execute("INSERT OR IGNORE INTO webhook_events (event_key, received_at) VALUES (?, ?)", (key, now))
            return cur.rowcount == 1

    def event_seen(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM webhook_events WHERE event_key = ?", (key,)).fetchone() is not None

    def mark_paid(self, order_id: str, payment_id: str, now: float, deadline_seconds: float) -> str:
        """Returns 'ok', 'already' (same payment seen before), 'conflict' (a different payment for an order that is
        already paid), 'reused' (this payment already belongs to another order) or 'unknown'."""
        with self._tx() as db:
            row = db.execute("SELECT status, payment_id FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                return "unknown"
            if row["status"] == ST_AWAITING:
                if db.execute("SELECT 1 FROM orders WHERE payment_id = ?", (payment_id,)).fetchone():
                    return "reused"
                db.execute(
                    "UPDATE orders SET status = ?, payment_id = ?, paid_at = ?, deadline_at = ?, next_attempt_at = ?, "
                    "lease_until = 0, updated_at = ? WHERE id = ? AND status = ?",
                    (ST_PAID, payment_id, now, now + deadline_seconds, now, now, order_id, ST_AWAITING),
                )
                return "ok"
            return "already" if row["payment_id"] == payment_id else "conflict"

    def claim_due(self, now: float, lease_seconds: float) -> Optional[Order]:
        """Take ownership of one order that needs work. A crashed worker's lease simply expires."""
        with self._tx() as db:
            row = db.execute(
                "SELECT id FROM orders WHERE status IN (?, ?, ?) AND next_attempt_at <= ? AND lease_until <= ? "
                "ORDER BY next_attempt_at LIMIT 1",
                (ST_PAID, ST_ANSWERED, ST_REFUND_PENDING, now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE orders SET lease_until = ?, updated_at = ? WHERE id = ?",
                       (now + lease_seconds, now, row["id"]))
        return self.get(row["id"])

    def record_answer(self, order_id: str, answer: str, now: float) -> bool:
        with self._tx() as db:
            cur = db.execute(
                "UPDATE orders SET status = ?, answer = ?, attempts = 0, next_attempt_at = ?, lease_until = 0, "
                "last_error = NULL, updated_at = ? WHERE id = ? AND status = ?",
                (ST_ANSWERED, answer, now, now, order_id, ST_PAID),
            )
            return cur.rowcount == 1

    def fail_attempt(self, order_id: str, error: str, next_at: float, now: float) -> int:
        with self._tx() as db:
            db.execute(
                "UPDATE orders SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?, lease_until = 0, "
                "updated_at = ? WHERE id = ?",
                (error, next_at, now, order_id),
            )
            return db.execute("SELECT attempts FROM orders WHERE id = ?", (order_id,)).fetchone()["attempts"]

    def set_delay_notified(self, order_id: str) -> bool:
        with self._tx() as db:
            return db.execute("UPDATE orders SET delay_notified = 1 WHERE id = ? AND delay_notified = 0",
                              (order_id,)).rowcount == 1

    def mark_delivered(self, order_id: str, now: float) -> bool:
        with self._tx() as db:
            return db.execute(
                "UPDATE orders SET status = ?, lease_until = 0, updated_at = ? WHERE id = ? AND status = ?",
                (ST_DELIVERED, now, order_id, ST_ANSWERED),
            ).rowcount == 1

    def to_refund(self, order_id: str, reason: str, now: float) -> bool:
        with self._tx() as db:
            return db.execute(
                "UPDATE orders SET status = ?, attempts = 0, next_attempt_at = ?, lease_until = 0, last_error = ?, "
                "updated_at = ? WHERE id = ? AND status IN (?, ?)",
                (ST_REFUND_PENDING, now, reason, now, order_id, ST_PAID, ST_ANSWERED),
            ).rowcount == 1

    def mark_refunded(self, order_id: str, now: float) -> bool:
        with self._tx() as db:
            return db.execute(
                "UPDATE orders SET status = ?, lease_until = 0, updated_at = ? WHERE id = ? AND status = ?",
                (ST_REFUNDED, now, order_id, ST_REFUND_PENDING),
            ).rowcount == 1
