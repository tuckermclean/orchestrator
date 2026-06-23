"""VAPID push subscription store — Protocol+Fake discipline.

Push subscriptions are per-device, per-operator.  Each subscription is
identified by its ``endpoint`` URL (unique per browser push service
registration).

Protocol: PushStorePort
Fake:     FakePushStore
Real:     SQLitePushStore
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import aiosqlite

from src.db import SharedDB, _make_shared_db, serialized_write

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id  TEXT    NOT NULL,
    endpoint     TEXT    NOT NULL UNIQUE,
    keys_json    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (operator_id, endpoint)
)
"""


@runtime_checkable
class PushStorePort(Protocol):
    """Contract for VAPID push subscription storage."""

    async def add_subscription(
        self,
        operator_id: str,
        endpoint: str,
        keys: dict[str, str],
        created_at: str,
    ) -> None:
        """Insert or replace a subscription."""
        ...

    async def remove_subscription(self, operator_id: str, endpoint: str) -> None:
        """Remove a subscription; no-op if not present."""
        ...

    async def list_subscriptions(self, operator_id: str) -> list[dict[str, object]]:
        """Return all subscriptions for the operator."""
        ...

    async def all_subscriptions(self) -> list[dict[str, object]]:
        """Return every subscription across all operators (for broadcasting)."""
        ...


# ---------------------------------------------------------------------------
# Fake — in-process, no I/O
# ---------------------------------------------------------------------------


class FakePushStore:
    """In-process push subscription store for tests and dev mode."""

    def __init__(self) -> None:
        # endpoint → subscription dict
        self._store: dict[str, dict[str, object]] = {}

    async def add_subscription(
        self,
        operator_id: str,
        endpoint: str,
        keys: dict[str, str],
        created_at: str,
    ) -> None:
        self._store[endpoint] = {
            "operator_id": operator_id,
            "endpoint": endpoint,
            "keys": keys,
            "created_at": created_at,
        }

    async def remove_subscription(self, operator_id: str, endpoint: str) -> None:
        self._store.pop(endpoint, None)

    async def list_subscriptions(self, operator_id: str) -> list[dict[str, object]]:
        return [
            sub for sub in self._store.values() if sub["operator_id"] == operator_id
        ]

    async def all_subscriptions(self) -> list[dict[str, object]]:
        return list(self._store.values())


# ---------------------------------------------------------------------------
# SQLite — DB-backed, async
# ---------------------------------------------------------------------------


class SQLitePushStore:
    """SQLite-backed VAPID push subscription store.

    Accepts either a ``SharedDB`` (production, shared connections) or a plain
    path string (tests / legacy callers).

    ``init()`` must be awaited before any other method.
    """

    def __init__(self, db_path: str | SharedDB = ":memory:") -> None:
        self._shared: SharedDB = _make_shared_db(db_path)
        self._owns_shared: bool = not isinstance(db_path, SharedDB)
        self._initialized = False

    async def init(self) -> None:
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
    def _conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("SQLitePushStore.init() must be called before use")
        return self._shared.write

    @property
    def _read_conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("SQLitePushStore.init() must be called before use")
        return self._shared.read

    async def close(self) -> None:
        if self._owns_shared:
            await self._shared.close()
        self._initialized = False

    @serialized_write
    async def add_subscription(
        self,
        operator_id: str,
        endpoint: str,
        keys: dict[str, str],
        created_at: str,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO push_subscriptions (operator_id, endpoint, keys_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (operator_id, endpoint) DO UPDATE SET
                keys_json  = excluded.keys_json,
                created_at = excluded.created_at
            """,
            (operator_id, endpoint, json.dumps(keys), created_at),
        )
        await self._conn.commit()

    @serialized_write
    async def remove_subscription(self, operator_id: str, endpoint: str) -> None:
        await self._conn.execute(
            "DELETE FROM push_subscriptions WHERE operator_id = ? AND endpoint = ?",
            (operator_id, endpoint),
        )
        await self._conn.commit()

    async def list_subscriptions(self, operator_id: str) -> list[dict[str, object]]:
        async with self._read_conn.execute(
            "SELECT operator_id, endpoint, keys_json, created_at "
            "FROM push_subscriptions WHERE operator_id = ? ORDER BY id ASC",
            (operator_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_sub(row) for row in rows]

    async def all_subscriptions(self) -> list[dict[str, object]]:
        async with self._read_conn.execute(
            "SELECT operator_id, endpoint, keys_json, created_at "
            "FROM push_subscriptions ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_sub(row) for row in rows]


def _row_to_sub(row: aiosqlite.Row) -> dict[str, object]:
    keys: dict[str, str] = json.loads(str(row["keys_json"]))
    return {
        "operator_id": str(row["operator_id"]),
        "endpoint": str(row["endpoint"]),
        "keys": keys,
        "created_at": str(row["created_at"]),
    }
