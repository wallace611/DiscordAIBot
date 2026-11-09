"""Entry point: wire up storage, models, background tasks, and the Discord client."""

from __future__ import annotations

import asyncio
import logging
import sys

from bot.client import MemoryBot
from bot.config import ConfigError, load_settings
from bot.llm import Summarizer, create_chat_provider, create_summary_backend
from bot.storage import Database, MessageStore, OptOutStore, SummaryStore
from bot.tasks import RetentionCleaner, SummaryWorker

log = logging.getLogger("bot")


async def run() -> None:
    settings = load_settings()

    # Storage
    db = Database(settings.db_path)
    await db.connect()
    messages = MessageStore(db)
    summaries = SummaryStore(db)
    optouts = OptOutStore(db)
    await optouts.load()

    # Models
    provider = create_chat_provider(settings.main)
    summarizer = Summarizer(
        create_summary_backend(settings.summary, settings.summary_num_ctx),
        language=settings.language,
        tz=settings.timezone,
        chunk_messages=settings.summary_chunk_messages,
        chunk_chars=settings.summary_chunk_chars,
    )
    if settings.summary.is_cloud:
        log.warning(
            "Summary provider is %s: full daily logs are sent off this machine. Use Ollama in production.",
            settings.summary.provider,
        )

    # The worker needs the client for channel names and the bot's own user id,
    # so it is created with late-bound callbacks.
    bot: MemoryBot | None = None
    worker = SummaryWorker(
        settings,
        messages,
        summaries,
        summarizer,
        channel_label=lambda channel_id: bot.channel_label(channel_id) if bot else str(channel_id),
        self_user_id=lambda: bot.user.id if bot and bot.user else None,
    )
    cleaner = RetentionCleaner(settings, messages, summaries)

    bot = MemoryBot(
        settings,
        messages=messages,
        summaries=summaries,
        optouts=optouts,
        provider=provider,
        worker=worker,
        cleaner=cleaner,
    )

    try:
        async with bot:
            await bot.start(settings.discord_token)
    finally:
        await provider.close()
        await summarizer.close()
        await db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
