"""Daily summary table. One row per (channel, local day). Summaries are an index, not facts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bot.storage.db import Database, utc_now_iso
from bot.storage.messages import DayKey


@dataclass(frozen=True)
class DailySummary:
    channel_id: int
    day: str
    content: str


@dataclass(frozen=True)
class SummaryJob:
    channel_id: int
    day: str
    guild_id: int | None


class SummaryStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def for_channel(self, channel_id: int, since_day: str, until_day: str) -> list[DailySummary]:
        """Usable summaries in [since_day, until_day], oldest first.

        Rows flagged for rebuild are skipped: they may still mention deleted messages.
        """
        cursor = await self._db.conn.execute(
            """
            SELECT channel_id, day, content FROM daily_summaries
            WHERE channel_id = ? AND day >= ? AND day <= ?
              AND content IS NOT NULL AND content != '' AND needs_rebuild = 0
            ORDER BY day ASC
            """,
            (channel_id, since_day, until_day),
        )
        return [DailySummary(row["channel_id"], row["day"], row["content"]) for row in await cursor.fetchall()]

    async def mark_rebuild(self, keys: Iterable[DayKey]) -> None:
        """Flag days for rebuild. Creates a placeholder row if none exists yet."""
        rows = [(channel_id, day, utc_now_iso()) for channel_id, day in keys]
        if not rows:
            return
        async with self._db.transaction() as conn:
            await conn.executemany(
                """
                INSERT INTO daily_summaries (channel_id, day, needs_rebuild, revision, updated_at)
                VALUES (?, ?, 1, 1, ?)
                ON CONFLICT (channel_id, day) DO UPDATE SET
                    needs_rebuild = 1,
                    revision = revision + 1,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    async def get_revision(self, channel_id: int, day: str) -> int:
        cursor = await self._db.conn.execute(
            "SELECT revision FROM daily_summaries WHERE channel_id = ? AND day = ?",
            (channel_id, day),
        )
        row = await cursor.fetchone()
        return row["revision"] if row else 0

    async def save(
        self,
        job: SummaryJob,
        content: str,
        message_count: int,
        last_message_id: int,
        expected_revision: int,
    ) -> bool:
        """Store a finished summary.

        If a rebuild was requested while the summary was being generated (revision changed),
        the row keeps needs_rebuild = 1 so the next scan regenerates it. Returns True when clean.
        """
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO daily_summaries
                    (channel_id, day, guild_id, content, message_count, last_message_id,
                     needs_rebuild, revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
                ON CONFLICT (channel_id, day) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    content = excluded.content,
                    message_count = excluded.message_count,
                    last_message_id = excluded.last_message_id,
                    needs_rebuild = CASE WHEN daily_summaries.revision = ? THEN 0 ELSE 1 END,
                    updated_at = excluded.updated_at
                RETURNING needs_rebuild
                """,
                (
                    job.channel_id,
                    job.day,
                    job.guild_id,
                    content,
                    message_count,
                    last_message_id,
                    utc_now_iso(),
                    expected_revision,
                ),
            )
            row = await cursor.fetchone()
        return row is not None and row["needs_rebuild"] == 0

    async def delete(self, channel_id: int, day: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM daily_summaries WHERE channel_id = ? AND day = ?",
                (channel_id, day),
            )

    async def delete_before(self, cutoff_day: str) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM daily_summaries WHERE day < ?", (cutoff_day,))
            return cursor.rowcount

    async def find_due(self, since_day: str, idle_before: str, offpeak: bool) -> list[SummaryJob]:
        """Days whose summary is missing, stale, or flagged for rebuild.

        A day is only returned when the run is off-peak or its channel has been idle
        since `idle_before` (UTC ISO). Flagged days with no messages left are always
        returned so their summary can be dropped.
        """
        cursor = await self._db.conn.execute(
            """
            WITH day_stats AS (
                SELECT channel_id, day, MAX(guild_id) AS guild_id, MAX(message_id) AS last_id
                FROM messages
                WHERE day >= :since
                GROUP BY channel_id, day
            ),
            channel_activity AS (
                SELECT channel_id, MAX(created_at) AS last_at
                FROM messages
                GROUP BY channel_id
            )
            SELECT d.channel_id, d.day, d.guild_id
            FROM day_stats d
            JOIN channel_activity a ON a.channel_id = d.channel_id
            LEFT JOIN daily_summaries s ON s.channel_id = d.channel_id AND s.day = d.day
            WHERE (s.channel_id IS NULL
                   OR s.content IS NULL
                   OR s.needs_rebuild = 1
                   OR d.last_id > s.last_message_id)
              AND (:offpeak = 1 OR a.last_at <= :idle_before)

            UNION ALL

            SELECT s.channel_id, s.day, s.guild_id
            FROM daily_summaries s
            WHERE s.needs_rebuild = 1
              AND NOT EXISTS (
                  SELECT 1 FROM messages m WHERE m.channel_id = s.channel_id AND m.day = s.day
              )

            ORDER BY 2 ASC, 1 ASC
            """,
            {"since": since_day, "idle_before": idle_before, "offpeak": int(offpeak)},
        )
        return [SummaryJob(row["channel_id"], row["day"], row["guild_id"]) for row in await cursor.fetchall()]
