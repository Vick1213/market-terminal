"""One LLM client behind the brief + strategist narratives (Phase 9 §5b).

Providers (MARKET_LLM_PROVIDER):
  * ollama    — local, default, $0 tokens (the original Phase-7 path)
  * openai    — chat-completions API (api.openai.com)
  * deepseek  — chat-completions API (api.deepseek.com, OpenAI-compatible)
  * anthropic — Messages API (api.anthropic.com)

``generate`` raises on any failure — callers own the deterministic-template
fallback, so a narrative can degrade but never silently fail or block a
digest. <think>…</think> blocks (Qwen3, deepseek-reasoner) are stripped here
so callers always get clean markdown.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("market.edge.llm")

_DEFAULT_MODELS = {
    "ollama": "qwen3:8b",
    "openai": "gpt-4o-mini",
    "deepseek": "deepseek-chat",
    "anthropic": "claude-opus-4-8",
}
_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "anthropic": "https://api.anthropic.com",
}
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LlmClient:
    def __init__(
        self,
        *,
        provider: str = "ollama",
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_model: str = "qwen3:8b",
        timeout: float = 180.0,
    ) -> None:
        self.provider = (provider or "ollama").strip().lower()
        if self.provider not in ("ollama", "openai", "deepseek", "anthropic"):
            log.warning("unknown MARKET_LLM_PROVIDER %r — falling back to ollama", provider)
            self.provider = "ollama"
        if self.provider == "ollama":
            self.model = model or ollama_model or _DEFAULT_MODELS["ollama"]
            self._base = (base_url or ollama_url).rstrip("/")
        else:
            self.model = model or _DEFAULT_MODELS[self.provider]
            self._base = (base_url or _DEFAULT_BASE_URLS[self.provider]).rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @property
    def label(self) -> str:
        """Stored as the brief/strategist 'model' chip, e.g. 'deepseek:deepseek-chat'."""
        return f"{self.provider}:{self.model}"

    async def generate(self, prompt: str, *, temperature: float = 0.3) -> str:
        """One-shot completion. Raises on any transport/shape/auth failure."""
        if self.provider == "ollama":
            text = await self._ollama(prompt, temperature)
        elif self.provider == "anthropic":
            text = await self._anthropic(prompt)
        else:
            text = await self._openai_compat(prompt, temperature)
        text = _THINK_RE.sub("", text or "").strip()
        if len(text) < 40:
            raise ValueError(f"{self.label} returned an empty/too-short response")
        return text

    async def _ollama(self, prompt: str, temperature: float) -> str:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/api/chat",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": temperature},
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]

    async def _openai_compat(self, prompt: str, temperature: float) -> str:
        if not self._api_key:
            raise ValueError(f"{self.provider} selected but MARKET_LLM_API_KEY is empty")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "stream": False,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def _anthropic(self, prompt: str) -> str:
        # Sampling params are omitted: recent Anthropic models reject them.
        if not self._api_key:
            raise ValueError("anthropic selected but MARKET_LLM_API_KEY is empty")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/v1/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": self.model,
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("stop_reason") == "refusal":
                raise ValueError("anthropic refused the request")
            return "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text"
            )
