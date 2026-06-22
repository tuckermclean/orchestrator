"""SQLite-backed ConvergeStateStore (SPEC §9.4).

Per-PR converge loop state: current round, round-start timestamp, and the
last dispatched run handle.  Used in the production path only;
``FakeConvergeStateStore`` remains the default for all tests (non-regression).
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from src.db import configure_sqlite_connection
from src.domain.types import PRRef, RunHandle

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS converge_state (
    pr_key          TEXT    PRIMARY KEY,
    converge_round  INTEGER NOT NULL DEFAULT 0,
    round_started   TEXT,
    last_run_handle TEXT
)
"""


def _pr_key(pr_ref: PRRef) -> str:
    """Stable string key for a PR reference."""
    return f"{pr_ref.repo.owner}/{pr_ref.repo.name}!{pr_ref.number}"


class SQLiteConvergeStateStore:
    """SQLite-backed per-PR converge loop state store.

    ``init()`` must be awaited before any other method.  ``close()`` must be
    awaited on shutdown to avoid ``aiosqlite`` event-loop teardown warnings.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the connection and create the table if absent."""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._db_path)
        await configure_sqlite_connection(self._db)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_TABLE)
        await self._db.commit()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError(
                "SQLiteConvergeStateStore.init() must be called before use"
            )
        return self._db

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # ConvergeStateStore Protocol methods
    # ------------------------------------------------------------------

    async def get_converge_round(self, pr_ref: PRRef) -> int:
        """Return the current converge round (0 if no state recorded yet)."""
        async with self._conn.execute(
            "SELECT converge_round FROM converge_state WHERE pr_key = ?",
            (_pr_key(pr_ref),),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["converge_round"]) if row is not None else 0

    async def set_converge_round(self, pr_ref: PRRef, round: int) -> None:
        """Upsert the converge round for this PR."""
        await self._conn.execute(
            """
            INSERT INTO converge_state (pr_key, converge_round)
            VALUES (?, ?)
            ON CONFLICT (pr_key) DO UPDATE SET converge_round = excluded.converge_round
            """,
            (_pr_key(pr_ref), round),
        )
        await self._conn.commit()

    async def get_round_started(self, pr_ref: PRRef) -> datetime | None:
        """Return the round-start timestamp, or None if not set."""
        async with self._conn.execute(
            "SELECT round_started FROM converge_state WHERE pr_key = ?",
            (_pr_key(pr_ref),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["round_started"] is None:
            return None
        return datetime.fromisoformat(str(row["round_started"]))

    async def set_round_started(self, pr_ref: PRRef, started: datetime) -> None:
        """Upsert the round-start timestamp for this PR."""
        ts = started.isoformat()
        await self._conn.execute(
            """
            INSERT INTO converge_state (pr_key, round_started)
            VALUES (?, ?)
            ON CONFLICT (pr_key) DO UPDATE SET round_started = excluded.round_started
            """,
            (_pr_key(pr_ref), ts),
        )
        await self._conn.commit()

    async def clear_converge_state(self, pr_ref: PRRef) -> None:
        """Delete the row for this PR, resetting all state to defaults."""
        await self._conn.execute(
            "DELETE FROM converge_state WHERE pr_key = ?",
            (_pr_key(pr_ref),),
        )
        await self._conn.commit()

    async def get_last_run_handle(self, pr_ref: PRRef) -> RunHandle | None:
        """Return the last dispatched run handle, or None if not set."""
        async with self._conn.execute(
            "SELECT last_run_handle FROM converge_state WHERE pr_key = ?",
            (_pr_key(pr_ref),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["last_run_handle"] is None:
            return None
        return RunHandle(run_id=str(row["last_run_handle"]))

    async def set_last_run_handle(self, pr_ref: PRRef, handle: RunHandle) -> None:
        """Upsert the last dispatched run handle for this PR."""
        await self._conn.execute(
            """
            INSERT INTO converge_state (pr_key, last_run_handle)
            VALUES (?, ?)
            ON CONFLICT (pr_key) DO UPDATE SET last_run_handle = excluded.last_run_handle
            """,
            (_pr_key(pr_ref), handle.run_id),
        )
        await self._conn.commit()
