"""Language-model providers and the fallback chain."""
from __future__ import annotations

from typing import List, Optional, Sequence

import requests


class AllProvidersFailed(Exception):
    pass


class LLMChain:
    """Try providers in order; the first one that answers wins."""

    def __init__(self, providers: Sequence) -> None:
        if not providers:
            raise ValueError("at least one LLM provider is required")
        self.providers = list(providers)

    def answer(self, question: str, system_prompt: str) -> str:
        errors: List[str] = []
        for provider in self.providers:
            name = getattr(provider, "name", type(provider).__name__)
            try:
                return provider.answer(question, system_prompt)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise AllProvidersFailed("; ".join(errors))


class GeminiLLM:
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout: float = 25, http=None) -> None:
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.http = http or requests.Session()

    def answer(self, question: str, system_prompt: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
        }
        r = self.http.post(url, params={"key": self.api_key}, json=body, timeout=self.timeout)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise RuntimeError("gemini returned an empty answer")
        return text


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float = 25, max_tokens: int = 1024, http=None) -> None:
        self.api_key, self.model, self.timeout, self.max_tokens = api_key, model, timeout, max_tokens
        self.http = http or requests.Session()

    def answer(self, question: str, system_prompt: str) -> str:
        r = self.http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": self.max_tokens, "system": system_prompt,
                  "messages": [{"role": "user", "content": question}]},
            timeout=self.timeout,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
        if not text:
            raise RuntimeError("anthropic returned an empty answer")
        return text
