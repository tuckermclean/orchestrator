"""Database layer — SQLite-backed audit log and state stores."""

from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Any

import aiosqlite

_log = logging.getLogger(__name__)

# Busy timeout applied to every file-backed SQLite connection (ms).
# Allows writers to wait instead of failing immediately on lock contention.
# Each store opens its own connection to the shared DB file, so several short
# writes (intake counter/audit/run-store status sinks + converge round state)
# can contend under load. 15s gives ample headroom for WAL writers to serialise
# rather than surfacing "database is locked" to the caller (mitigates #109).
SQLITE_BUSY_TIMEOUT_MS = 15000

# Belt-and-suspenders retry on residual lock contention (e.g. WAL checkpoint).
# With the process-wide write lock below this should rarely trigger, but guards
# against any lock that originates outside this process (e.g. litestream, a
# manual sqlite3 session) without dropping the write.
SQLITE_LOCKED_RETRY_MAX = 5
SQLITE_LOCKED_RETRY_DELAY_S = 0.05  # 50 ms between retries

# ---------------------------------------------------------------------------
# Process-wide write serialisation lock (#109)
#
# All SQLite-backed stores write to the same on-disk file.  Under concurrent
# dispatch (reconciler + converge + multiple webhook-triggered writes), issuing
# simultaneous commits from N independent aiosqlite connections creates N
# concurrent SQLite writers.  Even with WAL + busy_timeout, high-concurrency
# scenarios exceed the timeout and raise OperationalError("database is locked"),
# silently dropping run records and audit rows.
#
# Fix: one asyncio.Lock guards every write + commit across ALL stores in this
# process.  Because there is a single asyncio event loop (replicaCount=1), all
# coroutines share this lock.  Concurrent writes are serialised to one-at-a-time
# at the Python layer → the SQLite file never sees concurrent writers from this
# process.  Reads are NOT serialised (WAL allows concurrent readers).
#
# The lock is created lazily on first use (not at import time) so that it is
# always bound to the running event loop — important for pytest-asyncio which
# creates a fresh loop per test, and harmless in production where the lock is
# first acquired after the ASGI server has started its event loop.
# ---------------------------------------------------------------------------
_db_write_lock: asyncio.Lock | None = None


def _get_write_lock() -> asyncio.Lock:
    """Return the process-wide write lock, creating it lazily if needed.

    Thread-safe for single-threaded asyncio: the asyncio event loop never
    interleaves two coroutines between the ``is None`` check and the
    assignment — the GIL plus asyncio's cooperative scheduler guarantee this.
    """
    global _db_write_lock
    if _db_write_lock is None:
        _db_write_lock = asyncio.Lock()
    return _db_write_lock


def reset_write_lock() -> None:
    """Reset the write lock to ``None`` so the next call to ``_get_write_lock``
    creates a fresh lock on the current event loop.

    Intended for test teardown only — do not call in production code.
    """
    global _db_write_lock
    _db_write_lock = None

async def run_with_retry[T](
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
) -> T:
    """Execute *coro_fn()* under the process-wide write lock with retry.

    Acquires ``_db_write_lock`` before each attempt so all writes from this
    process are serialised (no concurrent writers to the SQLite file).

    On ``sqlite3.OperationalError`` containing ``"locked"`` retries up to
    ``SQLITE_LOCKED_RETRY_MAX`` times with exponential-ish backoff — this
    guards against external readers holding a lock (e.g. litestream, manual
    inspection) without indefinitely blocking progress.

    All other exceptions propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(1, SQLITE_LOCKED_RETRY_MAX + 1):
        async with _get_write_lock():
            try:
                return await coro_fn()
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_exc = exc
                _log.warning(
                    "SQLite write locked (attempt %d/%d): %r — retrying in %.0fms",
                    attempt,
                    SQLITE_LOCKED_RETRY_MAX,
                    exc,
                    SQLITE_LOCKED_RETRY_DELAY_S * 1000 * attempt,
                )
        # Back off outside the lock so other writers can make progress.
        await asyncio.sleep(SQLITE_LOCKED_RETRY_DELAY_S * attempt)
    raise RuntimeError(
        f"SQLite write failed after {SQLITE_LOCKED_RETRY_MAX} retries"
    ) from last_exc


def serialized_write[T](
    method: Callable[..., Coroutine[Any, Any, T]],
) -> Callable[..., Coroutine[Any, Any, T]]:
    """Decorator: wrap an async method so it runs under ``run_with_retry``.

    Usage::

        @serialized_write
        async def _do_write(self, ...) -> None:
            await self._conn.execute(...)
            await self._conn.commit()

    The decorated method acquires the process-wide write lock and retries on
    transient "locked" errors automatically.
    """

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await run_with_retry(lambda: method(*args, **kwargs))

    return wrapper


async def configure_sqlite_connection(db: aiosqlite.Connection) -> None:
    """Apply WAL journal mode and busy-timeout to *db*.

    Should be called immediately after ``aiosqlite.connect()``, before any
    DDL.  Safe on ``:memory:`` connections — ``PRAGMA journal_mode=WAL`` is a
    no-op there and ``PRAGMA busy_timeout`` has no effect on in-process shared
    cache, but neither causes an error.
    """
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
