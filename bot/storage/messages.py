"""Raw message table. This is the single source of truth for memory."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from bot.storage.db import Database, to_utc_iso

# (channel_id, day) pairs whose daily summary may now be out of date.
DayKey = tuple[int, str]


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    guild_id: int | None
    channel_id: int
    author_id: int
    author_name: str
    is_bot: bool
    content: str
    created_at: datetime
    day: str


def _row_to_message(row) -> StoredMessage:
    return StoredMessage(
        message_id=row["message_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        is_bot=bool(row["is_bot"]),
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
        day=row["day"],
    )


class MessageStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, message: StoredMessage) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO messages
                    (message_id, guild_id, channel_id, author_id, author_name, is_bot, content, created_at, day)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.guild_id,
                    message.channel_id,
                    message.author_id,
                    message.author_name,
                    int(message.is_bot),
                    message.content,
                    to_utc_iso(message.created_at),
                    message.day,
                ),
            )

    async def update_content(self, message_id: int, content: str) -> DayKey | None:
        """Apply an edit. Returns the affected day, or None if the message is not stored."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE messages SET content = ? WHERE message_id = ? RETURNING channel_id, day",
                (content, message_id),
            )
            row = await cursor.fetchone()
        return (row["channel_id"], row["day"]) if row else None

    async def delete_many(self, message_ids: Iterable[int]) -> set[DayKey]:
        """Delete messages by id. Returns the days that lost at least one message."""
        ids = list(message_ids)
        if not ids:
            return set()
        affected: set[DayKey] = set()
        async with self._db.transaction() as conn:
            # Chunk to stay well under SQLite's bound-parameter limit.
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                cursor = await conn.execute(
                    f"DELETE FROM messages WHERE message_id IN ({placeholders}) RETURNING channel_id, day",
                    chunk,
                )
                affected.update((row["channel_id"], row["day"]) for row in await cursor.fetchall())
        return affected

    async def delete_by_author(self, author_id: int) -> set[DayKey]:
        """Remove everything a user has said (used on opt-out)."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM messages WHERE author_id = ? RETURNING channel_id, day",
                (author_id,),
            )
            rows = await cursor.fetchall()
        return {(row["channel_id"], row["day"]) for row in rows}

    async def delete_before(self, cutoff_day: str) -> int:
        """Retention purge: delete messages older than cutoff_day (exclusive)."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute("DELETE FROM messages WHERE day < ?", (cutoff_day,))
            return cursor.rowcount

    async def recent(self, channel_id: int, limit: int, before_id: int | None = None) -> list[StoredMessage]:
        """Newest `limit` messages in a channel, returned oldest first."""
        if before_id is None:
            sql = "SELECT * FROM messages WHERE channel_id = ? ORDER BY message_id DESC LIMIT ?"
            params: tuple = (channel_id, limit)
        else:
            sql = (
                "SELECT * FROM messages WHERE channel_id = ? AND message_id < ? "
                "ORDER BY message_id DESC LIMIT ?"
            )
            params = (channel_id, before_id, limit)
        cursor = await self._db.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [_row_to_message(row) for row in reversed(rows)]

    async def for_day(self, channel_id: int, day: str) -> list[StoredMessage]:
        """All messages of one channel on one local day, oldest first."""
        cursor = await self._db.conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND day = ? ORDER BY message_id ASC",
            (channel_id, day),
        )
        return [_row_to_message(row) for row in await cursor.fetchall()]
