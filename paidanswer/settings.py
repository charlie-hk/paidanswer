from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


def money(amount_minor: int, currency: str) -> str:
    """Format an amount in minor units (paise, cents) for messages."""
    return f"{currency} {amount_minor / 100:.2f}"


@dataclass
class Settings:
    base_price: int = 99                      # major units (rupees, dollars)
    premium_price: int = 258
    currency: str = "INR"
    premium_keywords: Tuple[str, ...] = ("health", "wealth", "relationship")
    deadline_seconds: int = 600               # a paid order must be delivered or refunded within this time
    backoff: Tuple[int, ...] = (5, 15, 45, 120)
    lease_seconds: int = 60                   # how long a worker owns an order before another may take over
    open_order_max_age: int = 3600
    max_answer_chars: int = 3500              # keeps every answer inside one message (limit is 4096)
    voice_replies: bool = False
    system_prompt: str = "You are a helpful assistant. Answer clearly and briefly."
    brand: str = "PaidAnswer"

    def __post_init__(self) -> None:
        if self.deadline_seconds < 30:
            raise ValueError("deadline_seconds must be at least 30")
        if not self.backoff or any(b <= 0 for b in self.backoff):
            raise ValueError("backoff must contain positive numbers")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

    def is_premium(self, question: Optional[str]) -> bool:
        q = (question or "").lower()
        return any(k and k in q for k in self.premium_keywords)

    def amount_for(self, question: Optional[str]) -> int:
        price = self.premium_price if self.is_premium(question) else self.base_price
        return int(price) * 100

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        kwargs = {}
        if env.get("PRICE_BASE"):
            kwargs["base_price"] = int(env["PRICE_BASE"])
        if env.get("PRICE_PREMIUM"):
            kwargs["premium_price"] = int(env["PRICE_PREMIUM"])
        if env.get("CURRENCY"):
            kwargs["currency"] = env["CURRENCY"].upper()
        if env.get("PREMIUM_KEYWORDS") is not None:
            kwargs["premium_keywords"] = tuple(k.strip().lower() for k in env["PREMIUM_KEYWORDS"].split(",") if k.strip())
        if env.get("ANSWER_DEADLINE_SECONDS"):
            kwargs["deadline_seconds"] = int(env["ANSWER_DEADLINE_SECONDS"])
        if env.get("VOICE_REPLIES"):
            kwargs["voice_replies"] = env["VOICE_REPLIES"] == "1"
        prompt_file = env.get("SYSTEM_PROMPT_FILE")
        if prompt_file:
            with open(prompt_file, encoding="utf-8") as f:
                kwargs["system_prompt"] = f.read().strip()
        return cls(**kwargs)
