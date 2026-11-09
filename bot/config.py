"""Load runtime settings from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

SUPPORTED_PROVIDERS = ("ollama", "openai", "azure")


class ConfigError(ValueError):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class ProviderConfig:
    """Connection settings for one LLM endpoint."""

    provider: str
    # For Azure this is the deployment name, not the model name.
    model: str
    api_key: str | None
    base_url: str | None
    azure_endpoint: str | None
    azure_api_version: str | None
    temperature: float | None
    max_tokens: int | None
    timeout: float

    @property
    def is_cloud(self) -> bool:
        return self.provider != "ollama"


@dataclass(frozen=True)
class Settings:
    discord_token: str
    dev_guild_id: int | None
    timezone: ZoneInfo
    db_path: Path
    # Natural language the bot writes summaries and replies in.
    language: str

    # Memory window
    retention_days: int
    summary_lookback_days: int
    recent_message_limit: int

    # Models
    main: ProviderConfig
    summary: ProviderConfig
    summary_allow_cloud: bool
    summary_num_ctx: int
    summary_chunk_messages: int
    summary_chunk_chars: int

    # Background scheduling
    summary_idle_minutes: int
    summary_offpeak_start_hour: int
    summary_offpeak_end_hour: int
    summary_scan_minutes: int
    cleanup_interval_minutes: int


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _require(name: str) -> str:
    value = _get(name)
    if value is None:
        raise ConfigError(f"Missing required setting: {name}")
    return value


def _get_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def _get_optional_int(name: str, minimum: int | None = None) -> int | None:
    if _get(name) is None:
        return None
    return _get_int(name, 0, minimum=minimum)


def _get_float(name: str) -> float | None:
    raw = _get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _load_provider(prefix: str) -> ProviderConfig:
    """Read one provider block, e.g. MAIN_* or SUMMARY_*."""
    provider = _require(f"{prefix}_PROVIDER").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"{prefix}_PROVIDER must be one of {SUPPORTED_PROVIDERS}, got {provider!r}")

    api_key = _get(f"{prefix}_API_KEY")
    azure_endpoint = _get(f"{prefix}_AZURE_ENDPOINT")
    azure_api_version = _get(f"{prefix}_AZURE_API_VERSION")

    # Validate the fields each provider needs, so errors show up at startup.
    if provider in ("openai", "azure") and api_key is None:
        raise ConfigError(f"{prefix}_API_KEY is required for provider {provider!r}")
    if provider == "azure" and (azure_endpoint is None or azure_api_version is None):
        raise ConfigError(f"{prefix}_AZURE_ENDPOINT and {prefix}_AZURE_API_VERSION are required for Azure")

    return ProviderConfig(
        provider=provider,
        model=_require(f"{prefix}_MODEL"),
        api_key=api_key,
        base_url=_get(f"{prefix}_BASE_URL"),
        azure_endpoint=azure_endpoint,
        azure_api_version=azure_api_version,
        temperature=_get_float(f"{prefix}_TEMPERATURE"),
        max_tokens=_get_optional_int(f"{prefix}_MAX_TOKENS", minimum=1),
        timeout=float(_get_int(f"{prefix}_TIMEOUT_SECONDS", 120, minimum=1)),
    )


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Build Settings from the environment. Values in the real environment win over .env."""
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    tz_name = _get("TIMEZONE", "Asia/Taipei")
    try:
        timezone = ZoneInfo(tz_name)
    except Exception as exc:
        raise ConfigError(f"Unknown TIMEZONE: {tz_name!r}") from exc

    retention_days = _get_int("RETENTION_DAYS", 10, minimum=1)
    dev_guild_raw = _get("DEV_GUILD_ID")

    settings = Settings(
        discord_token=_require("DISCORD_TOKEN"),
        dev_guild_id=int(dev_guild_raw) if dev_guild_raw else None,
        timezone=timezone,
        db_path=Path(_get("DB_PATH", "data/bot.db")),
        language=_get("BOT_LANGUAGE", "Traditional Chinese (Taiwan)"),
        retention_days=retention_days,
        summary_lookback_days=_get_int("SUMMARY_LOOKBACK_DAYS", min(7, retention_days), minimum=1),
        recent_message_limit=_get_int("RECENT_MESSAGE_LIMIT", 40, minimum=1, maximum=200),
        main=_load_provider("MAIN"),
        summary=_load_provider("SUMMARY"),
        summary_allow_cloud=_get_bool("SUMMARY_ALLOW_CLOUD", False),
        summary_num_ctx=_get_int("SUMMARY_NUM_CTX", 8192, minimum=512),
        summary_chunk_messages=_get_int("SUMMARY_CHUNK_MESSAGES", 100, minimum=10),
        summary_chunk_chars=_get_int("SUMMARY_CHUNK_CHARS", 6000, minimum=1000),
        summary_idle_minutes=_get_int("SUMMARY_IDLE_MINUTES", 30, minimum=1),
        summary_offpeak_start_hour=_get_int("SUMMARY_OFFPEAK_START_HOUR", 3, minimum=0, maximum=23),
        summary_offpeak_end_hour=_get_int("SUMMARY_OFFPEAK_END_HOUR", 6, minimum=0, maximum=23),
        summary_scan_minutes=_get_int("SUMMARY_SCAN_MINUTES", 5, minimum=1),
        cleanup_interval_minutes=_get_int("CLEANUP_INTERVAL_MINUTES", 60, minimum=1),
    )

    # Full raw logs must stay local unless the operator explicitly accepts sending them out.
    if settings.summary.is_cloud and not settings.summary_allow_cloud:
        raise ConfigError(
            "SUMMARY_PROVIDER is a cloud provider, which sends full raw logs off this machine. "
            "Set SUMMARY_ALLOW_CLOUD=true to allow this (for development only)."
        )
    if settings.summary_lookback_days > settings.retention_days:
        raise ConfigError("SUMMARY_LOOKBACK_DAYS cannot be larger than RETENTION_DAYS")

    return settings
