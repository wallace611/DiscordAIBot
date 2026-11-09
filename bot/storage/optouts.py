"""Users who asked the bot not to record their messages."""

from __future__ import annotations

from bot.storage.db import Database, utc_now_iso


class OptOutStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        # In-memory copy so every incoming message does not need a DB round trip.
        self._cache: set[int] = set()

    async def load(self) -> None:
        cursor = await self._db.conn.execute("SELECT user_id FROM opt_outs")
        self._cache = {row["user_id"] for row in await cursor.fetchall()}

    def is_opted_out(self, user_id: int) -> bool:
        return user_id in self._cache

    async def opt_out(self, user_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO opt_outs (user_id, created_at) VALUES (?, ?)",
                (user_id, utc_now_iso()),
            )
        self._cache.add(user_id)

    async def opt_in(self, user_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM opt_outs WHERE user_id = ?", (user_id,))
        self._cache.discard(user_id)
