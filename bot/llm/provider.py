"""Thin provider layer over the OpenAI SDK.

Every provider difference (endpoint, auth, model vs deployment name, token parameter,
error types) lives in this file. The rest of the bot only sees ChatProvider.chat().
"""

from __future__ import annotations

import logging
from typing import Any

import openai
from openai import AsyncAzureOpenAI, AsyncOpenAI

from bot.config import ProviderConfig

log = logging.getLogger(__name__)

DEFAULT_OLLAMA_OPENAI_URL = "http://localhost:11434/v1"

ChatMessage = dict[str, str]


class LLMError(RuntimeError):
    """Provider-neutral error. `retryable` tells callers whether trying later may help."""

    def __init__(self, message: str, *, provider: str, retryable: bool) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ChatProvider:
    def __init__(self, config: ProviderConfig, client: AsyncOpenAI) -> None:
        self._config = config
        self._client = client

    @property
    def name(self) -> str:
        return self._config.provider

    @property
    def model(self) -> str:
        return self._config.model

    async def chat(self, messages: list[ChatMessage]) -> str:
        """Send a non-streaming chat completion and return the reply text."""
        kwargs: dict[str, Any] = {"model": self._config.model, "messages": messages}
        if self._config.temperature is not None:
            kwargs["temperature"] = self._config.temperature
        if self._config.max_tokens is not None:
            # OpenAI and Azure moved to max_completion_tokens; Ollama's compatible API reads max_tokens.
            key = "max_tokens" if self._config.provider == "ollama" else "max_completion_tokens"
            kwargs[key] = self._config.max_tokens

        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise _normalize_error(exc, self._config.provider) from exc

        if not response.choices:
            raise LLMError("Provider returned no choices", provider=self.name, retryable=True)
        choice = response.choices[0]
        content = choice.message.content or ""
        if choice.finish_reason == "content_filter":
            # Azure reports blocked output this way instead of raising.
            raise LLMError("Response blocked by content filter", provider=self.name, retryable=False)
        if choice.finish_reason == "length":
            log.warning("%s reply hit the token limit and was cut off", self.name)
        return content.strip()

    async def close(self) -> None:
        await self._client.close()


def _normalize_error(exc: openai.APIError, provider: str) -> LLMError:
    retryable = isinstance(
        exc,
        (openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.InternalServerError),
    )
    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}: {exc.message}" if status else exc.message
    return LLMError(f"{provider} request failed ({detail})", provider=provider, retryable=retryable)


def create_chat_provider(config: ProviderConfig) -> ChatProvider:
    """Build the right SDK client for the configured provider."""
    # The SDK retries connection errors, 429 and 5xx with backoff by itself.
    common = {"timeout": config.timeout, "max_retries": 3}

    if config.provider == "ollama":
        client = AsyncOpenAI(
            base_url=config.base_url or DEFAULT_OLLAMA_OPENAI_URL,
            api_key=config.api_key or "ollama",  # Ollama ignores the key, but the SDK requires one.
            **common,
        )
    elif config.provider == "openai":
        client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url, **common)
    elif config.provider == "azure":
        # Azure routes by deployment name, which is passed as `model` on each request.
        client = AsyncAzureOpenAI(
            azure_endpoint=config.azure_endpoint,
            api_key=config.api_key,
            api_version=config.azure_api_version,
            **common,
        )
    else:
        raise ValueError(f"Unsupported provider: {config.provider}")

    return ChatProvider(config, client)
