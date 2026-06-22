"""Database layer — SQLite-backed audit log and state stores."""

from __future__ import annotations

import aiosqlite

# Busy timeout applied to every file-backed SQLite connection (ms).
# Allows writers to wait instead of failing immediately on lock contention.
SQLITE_BUSY_TIMEOUT_MS = 5000


async def configure_sqlite_connection(db: aiosqlite.Connection) -> None:
    """Apply WAL journal mode and busy-timeout to *db*.

    Should be called immediately after ``aiosqlite.connect()``, before any
    DDL.  Safe on ``:memory:`` connections — ``PRAGMA journal_mode=WAL`` is a
    no-op there and ``PRAGMA busy_timeout`` has no effect on in-process shared
    cache, but neither causes an error.
    """
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
