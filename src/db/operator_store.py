"""Operator account store — SQLite-backed with Protocol+Fake discipline.

Operators are the human administrators who authenticate via the PWA.
Password hashes are stored; plaintext is never persisted.

Protocol: OperatorStorePort
Fake:     FakeOperatorStore
Real:     SQLiteOperatorStore
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import aiosqlite

from src.db import serialized_write

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS operators (
    id           TEXT    PRIMARY KEY,
    password_hash TEXT   NOT NULL,
    created_at   TEXT    NOT NULL,
    last_login   TEXT
)
"""


@runtime_checkable
class OperatorStorePort(Protocol):
    """Minimal operator account store contract."""

    async def get_operator(self, operator_id: str) -> dict[str, object] | None:
        """Return operator record or None if not found.

        Returned dict has: id, password_hash, created_at, last_login.
        """
        ...

    async def list_operators(self) -> list[dict[str, object]]:
        """Return all operators (omit password_hash)."""
        ...

    async def create_operator(self, operator_id: str, password_hash: str) -> None:
        """Insert a new operator; raises ValueError if operator_id already exists."""
        ...

    async def delete_operator(self, operator_id: str) -> None:
        """Delete an operator; raises ValueError if it is the last one."""
        ...

    async def update_password(self, operator_id: str, password_hash: str) -> None:
        """Replace the password hash for an operator."""
        ...

    async def record_login(self, operator_id: str) -> None:
        """Update last_login timestamp."""
        ...


# ---------------------------------------------------------------------------
# Fake — in-process, ordered dict, no I/O
# ---------------------------------------------------------------------------


class FakeOperatorStore:
    """In-process operator store for tests and dev mode."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, object]] = {}

    async def get_operator(self, operator_id: str) -> dict[str, object] | None:
        return self._store.get(operator_id)

    async def list_operators(self) -> list[dict[str, object]]:
        return [
            {k: v for k, v in rec.items() if k != "password_hash"}
            for rec in self._store.values()
        ]

    async def create_operator(self, operator_id: str, password_hash: str) -> None:
        if operator_id in self._store:
            raise ValueError(f"Operator {operator_id!r} already exists")
        self._store[operator_id] = {
            "id": operator_id,
            "password_hash": password_hash,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "last_login": None,
        }

    async def delete_operator(self, operator_id: str) -> None:
        if len(self._store) <= 1:
            raise ValueError("Cannot remove the last operator account")
        if operator_id not in self._store:
            raise ValueError(f"Operator {operator_id!r} not found")
        del self._store[operator_id]

    async def update_password(self, operator_id: str, password_hash: str) -> None:
        if operator_id not in self._store:
            raise ValueError(f"Operator {operator_id!r} not found")
        self._store[operator_id]["password_hash"] = password_hash

    async def record_login(self, operator_id: str) -> None:
        if operator_id in self._store:
            self._store[operator_id]["last_login"] = datetime.now(tz=UTC).isoformat()

    def seed(self, operator_id: str, password_hash: str) -> None:
        """Convenience: add an operator directly (bypasses async create_operator)."""
        self._store[operator_id] = {
            "id": operator_id,
            "password_hash": password_hash,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "last_login": None,
        }


# ---------------------------------------------------------------------------
# SQLite — DB-backed, async
# ---------------------------------------------------------------------------


class SQLiteOperatorStore:
    """SQLite-backed operator store.

    ``init()`` must be awaited before any other method.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_TABLE)
        await self._db.commit()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SQLiteOperatorStore.init() must be called before use")
        return self._db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get_operator(self, operator_id: str) -> dict[str, object] | None:
        async with self._conn.execute(
            "SELECT id, password_hash, created_at, last_login FROM operators WHERE id = ?",
            (operator_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "password_hash": str(row["password_hash"]),
            "created_at": str(row["created_at"]),
            "last_login": str(row["last_login"]) if row["last_login"] else None,
        }

    async def list_operators(self) -> list[dict[str, object]]:
        async with self._conn.execute(
            "SELECT id, created_at, last_login FROM operators ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "created_at": str(row["created_at"]),
                "last_login": str(row["last_login"]) if row["last_login"] else None,
            }
            for row in rows
        ]

    @serialized_write
    async def create_operator(self, operator_id: str, password_hash: str) -> None:
        try:
            await self._conn.execute(
                "INSERT INTO operators (id, password_hash, created_at) VALUES (?, ?, ?)",
                (operator_id, password_hash, datetime.now(tz=UTC).isoformat()),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError:
            raise ValueError(f"Operator {operator_id!r} already exists")

    @serialized_write
    async def delete_operator(self, operator_id: str) -> None:
        # Count total operators first
        async with self._conn.execute("SELECT COUNT(*) as cnt FROM operators") as cur:
            row = await cur.fetchone()
        count = int(row["cnt"]) if row else 0
        if count <= 1:
            raise ValueError("Cannot remove the last operator account")
        await self._conn.execute("DELETE FROM operators WHERE id = ?", (operator_id,))
        await self._conn.commit()

    @serialized_write
    async def update_password(self, operator_id: str, password_hash: str) -> None:
        await self._conn.execute(
            "UPDATE operators SET password_hash = ? WHERE id = ?",
            (password_hash, operator_id),
        )
        await self._conn.commit()

    @serialized_write
    async def record_login(self, operator_id: str) -> None:
        await self._conn.execute(
            "UPDATE operators SET last_login = ? WHERE id = ?",
            (datetime.now(tz=UTC).isoformat(), operator_id),
        )
        await self._conn.commit()
