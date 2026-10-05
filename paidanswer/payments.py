"""Razorpay payment links, refunds and webhook signature verification."""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import requests

from .models import PaymentLink


def verify_hmac_sha256(body: bytes, signature: Optional[str], secret: str, prefix: str = "") -> bool:
    """Constant-time check of an HMAC-SHA256 hex signature (Razorpay and Meta both use this scheme)."""
    if not signature or not secret:
        return False
    if prefix:
        if not signature.startswith(prefix):
            return False
        signature = signature[len(prefix):]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class RazorpayPayments:
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, timeout: float = 20, http=None) -> None:
        self.auth = (key_id, key_secret)
        self.timeout = timeout
        self.http = http or requests.Session()

    def create_link(self, order_id: str, amount: int, currency: str, description: str) -> PaymentLink:
        r = self.http.post(f"{self.BASE}/payment_links", auth=self.auth, timeout=self.timeout, json={
            "amount": amount, "currency": currency, "description": description,
            "reference_id": order_id, "reminder_enable": False, "notes": {"order_id": order_id},
        })
        r.raise_for_status()
        data = r.json()
        return PaymentLink(id=data["id"], url=data["short_url"])

    def refund(self, payment_id: str, amount: int, order_id: str) -> str:
        """Refund a payment in full. Safe to call again: an already refunded payment is not refunded twice."""
        p = self.http.get(f"{self.BASE}/payments/{payment_id}", auth=self.auth, timeout=self.timeout)
        p.raise_for_status()
        info = p.json()
        if int(info.get("amount_refunded") or 0) >= int(info.get("amount") or 0) > 0:
            return "already_refunded"
        r = self.http.post(f"{self.BASE}/payments/{payment_id}/refund", auth=self.auth, timeout=self.timeout,
                           json={"amount": amount, "notes": {"order_id": order_id, "reason": "answer not delivered"}})
        r.raise_for_status()
        return r.json().get("id", "refunded")
