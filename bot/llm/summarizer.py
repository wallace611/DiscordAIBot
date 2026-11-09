"""Daily summary generation.

Two interchangeable backends:
- "ollama": native Ollama API, so thinking can be turned off and num_ctx set per call.
- "openai" / "azure": reuses ChatProvider (development fallback when Ollama is not available).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import ollama

from bot.config import ProviderConfig
from bot.llm.provider import ChatMessage, LLMError, create_chat_provider
from bot.prompts import build_merge_messages, build_summary_messages, format_message_line
from bot.storage import StoredMessage

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Some models leak a reasoning block even with thinking disabled; drop it defensively.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class SummaryBackend(Protocol):
    name: str

    async def complete(self, messages: list[ChatMessage]) -> str: ...

    async def close(self) -> None: ...


class OllamaBackend:
    name = "ollama"

    def __init__(self, config: ProviderConfig, num_ctx: int) -> None:
        self._model = config.model
        self._options: dict[str, float | int] = {"num_ctx": num_ctx}
        if config.temperature is not None:
            self._options["temperature"] = config.temperature
        if config.max_tokens is not None:
            self._options["num_predict"] = config.max_tokens
        self._client = ollama.AsyncClient(host=config.base_url or DEFAULT_OLLAMA_HOST, timeout=config.timeout)

    async def complete(self, messages: list[ChatMessage]) -> str:
        try:
            # think=False disables Qwen3 thinking mode.
            response = await self._client.chat(
                model=self._model,
                messages=messages,
                think=False,
                options=self._options,
            )
        except ollama.ResponseError as exc:
            raise LLMError(
                f"ollama request failed (HTTP {exc.status_code}: {exc.error})",
                provider=self.name,
                retryable=exc.status_code >= 500 or exc.status_code == 429,
            ) from exc
        except (ConnectionError, httpx.HTTPError) as exc:
            # The ollama client turns connect failures into the builtin ConnectionError.
            raise LLMError(f"ollama connection failed ({exc})", provider=self.name, retryable=True) from exc
        return response.message.content or ""

    async def close(self) -> None:
        await self._client.close()


class ProviderBackend:
    """Summary backend that goes through the OpenAI-compatible provider layer."""

    def __init__(self, config: ProviderConfig) -> None:
        self._provider = create_chat_provider(config)
        self.name = config.provider

    async def complete(self, messages: list[ChatMessage]) -> str:
        return await self._provider.chat(messages)

    async def close(self) -> None:
        await self._provider.close()


def create_summary_backend(config: ProviderConfig, num_ctx: int) -> SummaryBackend:
    if config.provider == "ollama":
        return OllamaBackend(config, num_ctx)
    return ProviderBackend(config)


class Summarizer:
    def __init__(
        self,
        backend: SummaryBackend,
        *,
        language: str,
        tz: ZoneInfo,
        chunk_messages: int,
        chunk_chars: int,
    ) -> None:
        self._backend = backend
        self._language = language
        self._tz = tz
        self._chunk_messages = chunk_messages
        self._chunk_chars = chunk_chars

    async def summarize_day(self, channel_label: str, day: str, messages: Sequence[StoredMessage]) -> str:
        """Summarize one channel-day from raw messages only (never from older summaries)."""
        lines = [format_message_line(m, self._tz, with_date=False) for m in messages]
        chunks = self._split(lines)
        log.info("Summarizing %s %s: %d messages in %d chunk(s)", channel_label, day, len(lines), len(chunks))

        partials = []
        for chunk in chunks:
            partials.append(await self._run(build_summary_messages(channel_label, day, chunk, self._language)))
        if len(partials) == 1:
            return partials[0]
        return await self._merge(channel_label, day, partials)

    def _split(self, lines: list[str]) -> list[list[str]]:
        """Split by message count, and also by size so one chunk fits the model context."""
        chunks: list[list[str]] = []
        current: list[str] = []
        size = 0
        for line in lines:
            if current and (len(current) >= self._chunk_messages or size + len(line) > self._chunk_chars):
                chunks.append(current)
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append(current)
        return chunks

    async def _merge(self, channel_label: str, day: str, partials: list[str]) -> str:
        """Merge partial summaries of the same day, in rounds if they do not fit at once."""
        while len(partials) > 1:
            groups: list[list[str]] = [[]]
            size = 0
            for text in partials:
                if len(groups[-1]) >= 2 and size + len(text) > self._chunk_chars:
                    groups.append([])
                    size = 0
                groups[-1].append(text)
                size += len(text)
            merged = []
            for group in groups:
                if len(group) == 1:
                    merged.append(group[0])
                else:
                    merged.append(await self._run(build_merge_messages(channel_label, day, group, self._language)))
            partials = merged
        return partials[0]

    async def _run(self, messages: list[ChatMessage]) -> str:
        text = await self._backend.complete(messages)
        text = _THINK_BLOCK.sub("", text).strip()
        if not text:
            raise LLMError("Summary model returned empty text", provider=self._backend.name, retryable=True)
        return text

    async def close(self) -> None:
        await self._backend.close()
