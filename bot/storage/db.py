"""SQLite connection and schema. All access goes through aiosqlite so the event loop never blocks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def to_utc_iso(value: datetime) -> str:
    """Fixed-width UTC timestamp so string comparison in SQL matches time order."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def utc_now_iso() -> str:
    return to_utc_iso(datetime.now(timezone.utc))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id   INTEGER PRIMARY KEY,
    guild_id     INTEGER,
    channel_id   INTEGER NOT NULL,
    author_id    INTEGER NOT NULL,
    author_name  TEXT    NOT NULL,
    is_bot       INTEGER NOT NULL DEFAULT 0,
    content      TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,   -- ISO 8601, UTC
    day          TEXT    NOT NULL    -- YYYY-MM-DD in the configured local time zone
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_created ON messages (channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel_day     ON messages (channel_id, day);
CREATE INDEX IF NOT EXISTS idx_messages_day             ON messages (day);
CREATE INDEX IF NOT EXISTS idx_messages_author          ON messages (author_id);

CREATE TABLE IF NOT EXISTS daily_summaries (
    channel_id       INTEGER NOT NULL,
    day              TEXT    NOT NULL,
    guild_id         INTEGER,
    content          TEXT,               -- NULL until the first summary is written
    message_count    INTEGER NOT NULL DEFAULT 0,
    last_message_id  INTEGER NOT NULL DEFAULT 0,  -- newest message covered by content
    needs_rebuild    INTEGER NOT NULL DEFAULT 0,
    revision         INTEGER NOT NULL DEFAULT 0,  -- bumped on every rebuild request
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (channel_id, day)
);
CREATE INDEX IF NOT EXISTS idx_summaries_day ON daily_summaries (day);

CREATE TABLE IF NOT EXISTS opt_outs (
    user_id     INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL
);
"""


class Database:
    """Owns one aiosqlite connection shared by all stores."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Serializes multi-statement transactions on the shared connection.
        self._tx_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit by default, explicit BEGIN for transactions.
        self._conn = await aiosqlite.connect(self._path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run several statements atomically."""
        async with self._tx_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")
