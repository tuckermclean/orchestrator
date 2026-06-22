"""Database layer — SQLite-backed audit log and state stores."""

from __future__ import annotations

import aiosqlite

# Busy timeout applied to every file-backed SQLite connection (ms).
# Allows writers to wait instead of failing immediately on lock contention.
# Each store opens its own connection to the shared DB file, so several short
# writes (intake counter/audit/run-store status sinks + converge round state)
# can contend under load. 15s gives ample headroom for WAL writers to serialise
# rather than surfacing "database is locked" to the caller (mitigates #109).
SQLITE_BUSY_TIMEOUT_MS = 15000


async def configure_sqlite_connection(db: aiosqlite.Connection) -> None:
    """Apply WAL journal mode and busy-timeout to *db*.

    Should be called immediately after ``aiosqlite.connect()``, before any
    DDL.  Safe on ``:memory:`` connections — ``PRAGMA journal_mode=WAL`` is a
    no-op there and ``PRAGMA busy_timeout`` has no effect on in-process shared
    cache, but neither causes an error.
    """
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
