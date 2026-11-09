"""Background daily-summary worker.

A scanner finds channel-days whose summary is missing, stale, or flagged for rebuild,
and puts them on a queue. One consumer drains the queue in order, so only one summary
call runs at a time (this keeps the local model from competing for VRAM).

Summaries are never triggered per message. A channel-day is picked up only during the
off-peak window, or after its channel has been idle for a while.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from bot.config import Settings
from bot.llm.provider import LLMError
from bot.llm.summarizer import Summarizer
from bot.storage import MessageStore, SummaryJob, SummaryStore
from bot.storage.db import to_utc_iso
from bot.timeutil import in_hour_window, window_start

log = logging.getLogger(__name__)

JobKey = tuple[int, str]

# Backoff after a failed job: 5 min, 10 min, 20 min ... capped at 6 hours.
_BACKOFF_BASE_SECONDS = 300
_BACKOFF_MAX_SECONDS = 6 * 3600


class SummaryWorker:
    def __init__(
        self,
        settings: Settings,
        messages: MessageStore,
        summaries: SummaryStore,
        summarizer: Summarizer,
        *,
        channel_label: Callable[[int], str],
        self_user_id: Callable[[], int | None],
    ) -> None:
        self._settings = settings
        self._messages = messages
        self._summaries = summaries
        self._summarizer = summarizer
        self._channel_label = channel_label
        self._self_user_id = self_user_id

        self._queue: asyncio.Queue[SummaryJob] = asyncio.Queue()
        self._queued: set[JobKey] = set()
        # key -> (failure count, monotonic time when retry is allowed)
        self._failures: dict[JobKey, tuple[int, float]] = {}
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._scan_loop(), name="summary-scan"),
            asyncio.create_task(self._consume_loop(), name="summary-consume"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # -- scanning ------------------------------------------------------------

    def _is_offpeak(self) -> bool:
        hour = datetime.now(self._settings.timezone).hour
        return in_hour_window(hour, self._settings.summary_offpeak_start_hour, self._settings.summary_offpeak_end_hour)

    async def scan_once(self) -> int:
        """Queue every due job. Returns how many new jobs were queued."""
        since_day = window_start(self._settings.timezone, self._settings.retention_days)
        idle_before = to_utc_iso(datetime.now(timezone.utc) - timedelta(minutes=self._settings.summary_idle_minutes))
        jobs = await self._summaries.find_due(since_day, idle_before, self._is_offpeak())

        now = time.monotonic()
        added = 0
        for job in jobs:
            key = (job.channel_id, job.day)
            if key in self._queued:
                continue
            failure = self._failures.get(key)
            if failure is not None and now < failure[1]:
                continue
            self._queued.add(key)
            self._queue.put_nowait(job)
            added += 1
        if added:
            log.info("Queued %d summary job(s)", added)
        return added

    async def _scan_loop(self) -> None:
        interval = self._settings.summary_scan_minutes * 60
        while True:
            try:
                await self.scan_once()
            except Exception:
                log.exception("Summary scan failed")
            await asyncio.sleep(interval)

    # -- consuming -----------------------------------------------------------

    async def _consume_loop(self) -> None:
        while True:
            job = await self._queue.get()
            key = (job.channel_id, job.day)
            try:
                await self.process(job)
                self._failures.pop(key, None)
            except LLMError as exc:
                self._record_failure(key)
                log.warning("Summary for %s failed (retryable=%s): %s", key, exc.retryable, exc)
            except Exception:
                self._record_failure(key)
                log.exception("Summary for %s failed", key)
            finally:
                self._queued.discard(key)
                self._queue.task_done()

    def _record_failure(self, key: JobKey) -> None:
        count = self._failures.get(key, (0, 0.0))[0] + 1
        delay = min(_BACKOFF_BASE_SECONDS * 2 ** (count - 1), _BACKOFF_MAX_SECONDS)
        self._failures[key] = (count, time.monotonic() + delay)

    async def process(self, job: SummaryJob) -> None:
        """Build one channel-day summary from that day's raw messages."""
        # Read the revision first: a rebuild request that lands during generation bumps it.
        revision = await self._summaries.get_revision(job.channel_id, job.day)
        day_messages = await self._messages.for_day(job.channel_id, job.day)

        if not day_messages:
            # Every message of the day was deleted, so the summary must go too.
            await self._summaries.delete(job.channel_id, job.day)
            log.info("Dropped summary for %s %s (no messages left)", job.channel_id, job.day)
            return

        last_message_id = day_messages[-1].message_id
        # Leave out the bot's own replies so its answers are not fed back in as memory.
        self_id = self._self_user_id()
        source = [m for m in day_messages if m.author_id != self_id]

        if source:
            content = await self._summarizer.summarize_day(self._channel_label(job.channel_id), job.day, source)
        else:
            content = ""

        clean = await self._summaries.save(
            job,
            content=content,
            message_count=len(source),
            last_message_id=last_message_id,
            expected_revision=revision,
        )
        if clean:
            log.info("Saved summary for %s %s (%d messages)", job.channel_id, job.day, len(source))
        else:
            log.info("Summary for %s %s changed during generation; will rebuild", job.channel_id, job.day)
