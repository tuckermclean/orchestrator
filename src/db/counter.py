"""Real SQLite-backed CounterStore (SPEC §8.2a).

Atomic ``UPDATE … SET v = v + 1`` increment. Used in the production path only;
``FakeCounterStore`` remains the default for all tests (non-regression).
"""

from __future__ import annotations

import aiosqlite

from src.db import SharedDB, _make_shared_db, serialized_write
from src.domain.types import IssueRef, PRRef

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS entity_counters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key   TEXT    NOT NULL,
    channel      TEXT    NOT NULL,
    v            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (entity_key, channel)
)
"""


def _entity_key(entity_ref: IssueRef | PRRef) -> str:
    """Stable string key for an entity reference."""
    if isinstance(entity_ref, IssueRef):
        return (
            f"issue:{entity_ref.repo.owner}/{entity_ref.repo.name}#{entity_ref.number}"
        )
    return (
        f"pr:{entity_ref.repo.owner}/{entity_ref.repo.name}!{entity_ref.number}"
    )


class SQLiteCounterStore:
    """SQLite-backed atomic per-entity, per-channel counter store.

    Accepts either a ``SharedDB`` (production, shared connections) or a plain
    path string (tests / legacy callers).

    The ``increment`` method uses ``INSERT … ON CONFLICT DO UPDATE SET v = v + 1``
    so the read-modify-write is handled by SQLite's serialised write lock — no
    application-level locking is needed for single-process use.
    """

    def __init__(self, db_path: str | SharedDB = ":memory:") -> None:
        self._shared: SharedDB = _make_shared_db(db_path)
        self._owns_shared: bool = not isinstance(db_path, SharedDB)
        self._initialized = False

    async def init(self) -> None:
        """Open the connection and create the table if absent."""
        if self._initialized:
            return
        await self._shared.init()
        wc = self._shared.write
        wc.row_factory = aiosqlite.Row
        await wc.execute(_CREATE_TABLE)
        await wc.commit()
        self._shared.read.row_factory = aiosqlite.Row
        self._initialized = True

    @property
    def _db(self) -> aiosqlite.Connection | None:
        """Legacy compat: expose the write connection as ``_db``."""
        return self._shared._write

    @property
    def _conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("SQLiteCounterStore.init() must be called before use")
        return self._shared.write

    @property
    def _read_conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("SQLiteCounterStore.init() must be called before use")
        return self._shared.read

    async def get_count(self, entity_ref: IssueRef | PRRef, channel: str) -> int:
        """Return the current counter value (0 if never incremented)."""
        key = _entity_key(entity_ref)
        async with self._read_conn.execute(
            "SELECT v FROM entity_counters WHERE entity_key = ? AND channel = ?",
            (key, channel),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["v"]) if row is not None else 0

    @serialized_write
    async def increment(self, entity_ref: IssueRef | PRRef, channel: str) -> int:
        """Atomically increment the counter; return the new value.

        Uses ``INSERT … ON CONFLICT DO UPDATE`` so the entire read-modify-write
        is a single SQL statement executed under SQLite's serialised write lock.
        The shared write connection ensures no concurrent writers; @serialized_write
        adds belt-and-suspenders retry for out-of-process lockers.
        """
        key = _entity_key(entity_ref)
        async with self._conn.execute(
            """
            INSERT INTO entity_counters (entity_key, channel, v)
            VALUES (?, ?, 1)
            ON CONFLICT (entity_key, channel)
            DO UPDATE SET v = v + 1
            RETURNING v
            """,
            (key, channel),
        ) as cursor:
            row = await cursor.fetchone()
        await self._conn.commit()
        assert row is not None, "RETURNING returned no row"
        return int(row["v"])

    @serialized_write
    async def reset(self, entity_ref: IssueRef | PRRef, channel: str) -> None:
        """Reset the counter to 0."""
        key = _entity_key(entity_ref)
        await self._conn.execute(
            """
            INSERT INTO entity_counters (entity_key, channel, v)
            VALUES (?, ?, 0)
            ON CONFLICT (entity_key, channel)
            DO UPDATE SET v = 0
            """,
            (key, channel),
        )
        await self._conn.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._owns_shared:
            await self._shared.close()
        self._initialized = False
