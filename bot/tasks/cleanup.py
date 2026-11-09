"""Retention purge: drop raw messages and daily summaries older than RETENTION_DAYS."""

from __future__ import annotations

import asyncio
import logging

from bot.config import Settings
from bot.storage import MessageStore, SummaryStore
from bot.timeutil import window_start

log = logging.getLogger(__name__)


class RetentionCleaner:
    def __init__(self, settings: Settings, messages: MessageStore, summaries: SummaryStore) -> None:
        self._settings = settings
        self._messages = messages
        self._summaries = summaries
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="retention-cleanup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run_once(self) -> tuple[int, int]:
        # Keep today plus the previous RETENTION_DAYS - 1 days.
        cutoff = window_start(self._settings.timezone, self._settings.retention_days)
        removed_messages = await self._messages.delete_before(cutoff)
        removed_summaries = await self._summaries.delete_before(cutoff)
        if removed_messages or removed_summaries:
            log.info(
                "Retention purge before %s: %d messages, %d summaries",
                cutoff,
                removed_messages,
                removed_summaries,
            )
        return removed_messages, removed_summaries

    async def _loop(self) -> None:
        interval = self._settings.cleanup_interval_minutes * 60
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("Retention purge failed")
            await asyncio.sleep(interval)
