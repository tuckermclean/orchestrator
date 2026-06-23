"""Database layer — SQLite-backed audit log and state stores.

Single-writer architecture (#109 fix, supersedes PR #137)
----------------------------------------------------------
PR #137 added a process-wide ``asyncio.Lock`` + 5× retry around every write.
That did NOT fix the live "database is locked" failures because:

1. The lock was held *across* the failing write — the contention was NOT from
   another control-plane coroutine but from WAL-checkpoint write-locks taken by
   SQLite on a *different* connection while a commit was in flight on this one.
   On Longhorn (networked/replicated PVC), cross-connection POSIX/WAL-shm
   locking is unreliable, producing "database is locked" even for a single
   serialised writer.

2. With 6 stores each opening their own ``aiosqlite`` connection (~13 open fds
   to the same SQLite file), there are always N concurrent writers regardless of
   the asyncio lock — every ``aiosqlite`` connection runs its own background
   thread, and SQLite only permits one writer thread at a time across all
   connections to a file.

3. The 5× retry + backoff (≈1 s total) held the store's connection thread,
   queueing reads on the same connection behind it → ``/api/runs`` timeouts.

Fix: one shared ``aiosqlite`` connection for ALL writes + a separate read
connection.  aiosqlite serialises all ops on a connection to a single
background thread — so shared-write-connection = zero concurrent writers, zero
cross-connection WAL contention, zero POSIX lock races.  Reads use a separate
connection so they are never blocked by the write queue.

``:memory:`` handling
~~~~~~~~~~~~~~~~~~~~~
SQLite in-memory databases are per-connection: a second connection to
``:memory:`` sees an empty database.  When ``db_path`` is ``:memory:``, both
``_write`` and ``_read`` point to the same connection so reads see what was
written.  DDL and writes use ``_write``; reads use ``_read`` (identical object
for ``:memory:``).

Backward compatibility
~~~~~~~~~~~~~~~~~~~~~~
All public store classes (``AuditLog``, ``SQLiteCounterStore``, …) accept
either a ``SharedDB`` instance (production) or a bare path string (tests /
legacy callers).  When passed a string they create a ``SharedDB`` internally.
"""

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
SQLITE_BUSY_TIMEOUT_MS = 15000

# Belt-and-suspenders retry for external lock sources (litestream, manual
# sqlite3 session).  The single-writer connection eliminates inter-store
# contention; these handle the rare case of an out-of-process writer.
SQLITE_LOCKED_RETRY_MAX = 5
SQLITE_LOCKED_RETRY_DELAY_S = 0.05  # 50 ms between retries


# ---------------------------------------------------------------------------
# SharedDB — one write + one read connection for all stores
# ---------------------------------------------------------------------------


class SharedDB:
    """Shared SQLite connection set: ONE write connection + ONE read connection.

    All stores in a single process share one ``SharedDB`` instance.  The key
    insight is that a single aiosqlite connection serialises its background
    thread, BUT multiple asyncio coroutines sharing the same connection can
    interleave their ``execute()`` + ``commit()`` sequences at ``await``
    points — producing ``"cannot commit transaction - SQL statements in
    progress"`` errors.

    To prevent this, ``SharedDB`` owns a single ``asyncio.Lock`` (``_write_lock``)
    that serialises the entire execute+commit sequence for every write across
    ALL stores in the process.  The lock is created lazily (on first
    ``init()`` call) so it is always bound to the current event loop — correct
    for pytest-asyncio which creates a fresh loop per test.

    This gives three properties that eliminate the #109 failures:

    1. **Zero concurrent SQLite writers** — the asyncio lock ensures only one
       execute+commit sequence runs at a time on the write connection.
    2. **Zero cross-connection WAL-checkpoint races** — all six stores share one
       connection; no second connection is committing while this one does.
    3. **Reads never block on writes** — the read connection is separate (WAL
       allows concurrent readers) so ``list_runs`` / ``get_run`` run freely.

    For ``:memory:`` databases both ``write`` and ``read`` point to the same
    connection (separate connections to ``:memory:`` see disjoint namespaces).

    Usage::

        shared = SharedDB("/data/orchestrator.db")
        await shared.init()
        # … pass shared to all stores …
        await shared.close()
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._write: aiosqlite.Connection | None = None
        self._read: aiosqlite.Connection | None = None
        # Lazily created on first init() so it is always on the current loop.
        self._write_lock: asyncio.Lock | None = None

    @property
    def write(self) -> aiosqlite.Connection:
        """The single shared write connection (all stores mutate through here)."""
        if self._write is None:
            raise RuntimeError("SharedDB.init() must be called before use")
        return self._write

    @property
    def read(self) -> aiosqlite.Connection:
        """The read connection (same as write for :memory:)."""
        if self._read is None:
            raise RuntimeError("SharedDB.init() must be called before use")
        return self._read

    def get_write_lock(self) -> asyncio.Lock:
        """Return the write lock, creating it if needed.

        Called by ``run_with_retry`` when a store has a ``SharedDB``.  The lock
        is created here (not in ``__init__``) so it binds to the running loop.
        """
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def init(self) -> None:
        """Open connections and apply WAL + busy_timeout.

        Idempotent — safe to call multiple times.
        """
        if self._write is not None:
            return

        # Ensure the lock exists on the current event loop before any I/O.
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()

        self._write = await aiosqlite.connect(self._db_path)
        await configure_sqlite_connection(self._write)
        self._write.row_factory = aiosqlite.Row

        if self._is_memory:
            # Reuse the same connection so reads see in-flight writes.
            self._read = self._write
        else:
            self._read = await aiosqlite.connect(self._db_path)
            await configure_sqlite_connection(self._read)
            self._read.row_factory = aiosqlite.Row

    async def close(self) -> None:
        """Close connections.  Idempotent."""
        if self._write is None:
            return
        if not self._is_memory and self._read is not None:
            await self._read.close()
        await self._write.close()
        self._write = None
        self._read = None
        self._write_lock = None


def _make_shared_db(db_path_or_shared: str | SharedDB) -> SharedDB:
    """Return a ``SharedDB`` for *db_path_or_shared*.

    If a ``SharedDB`` is already provided, return it unchanged (shared
    production path).  If a plain string path is given, wrap it in a new
    ``SharedDB`` (legacy / per-store test path — the store owns the
    connection and must call ``init()`` / ``close()`` itself).
    """
    if isinstance(db_path_or_shared, SharedDB):
        return db_path_or_shared
    return SharedDB(db_path_or_shared)


# ---------------------------------------------------------------------------
# Legacy helpers — kept for tests and belt-and-suspenders retry
# ---------------------------------------------------------------------------

# reset_write_lock is kept as a no-op so any test calling it doesn't break.
def reset_write_lock() -> None:
    """No-op stub retained for backward compatibility.

    PR #137 used a process-wide ``asyncio.Lock``; the single-writer
    architecture (this PR) eliminates the need for it.  Test teardown calls
    that previously called ``reset_write_lock()`` are harmless here.
    """


async def run_with_retry[T](
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    shared_db: SharedDB | None = None,
) -> T:
    """Execute *coro_fn()* under the shared write lock with retry.

    When *shared_db* is provided its ``_write_lock`` is acquired around the
    entire execute+commit sequence.  This prevents concurrent asyncio coroutines
    from interleaving their write sequences on the shared connection (which
    would produce ``"cannot commit transaction - SQL statements in progress"``).

    On ``sqlite3.OperationalError`` containing ``"locked"`` retries up to
    ``SQLITE_LOCKED_RETRY_MAX`` times with exponential-ish backoff — guards
    against external writers (litestream, manual sqlite3 sessions) holding a
    brief lock even through WAL.

    All other exceptions propagate immediately.
    """
    last_exc: Exception | None = None
    lock: asyncio.Lock | None = shared_db.get_write_lock() if shared_db is not None else None

    for attempt in range(1, SQLITE_LOCKED_RETRY_MAX + 1):
        try:
            if lock is not None:
                async with lock:
                    return await coro_fn()
            else:
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
            await asyncio.sleep(SQLITE_LOCKED_RETRY_DELAY_S * attempt)
    raise RuntimeError(
        f"SQLite write failed after {SQLITE_LOCKED_RETRY_MAX} retries"
    ) from last_exc


def serialized_write[T](
    method: Callable[..., Coroutine[Any, Any, T]],
) -> Callable[..., Coroutine[Any, Any, T]]:
    """Decorator: wrap an async method so it runs under ``run_with_retry``.

    Extracts the ``SharedDB`` from ``self._shared`` (if present) and passes it
    to ``run_with_retry`` so the shared write lock is acquired.  Stores that
    own a private ``SharedDB`` (per-path strings, test mode) get the lock from
    their own ``SharedDB``; stores that share a production ``SharedDB`` all
    contend on the same lock — serialising their writes.

    Usage::

        @serialized_write
        async def _do_write(self, ...) -> None:
            await self._conn.execute(...)
            await self._conn.commit()
    """

    @functools.wraps(method)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        # Extract SharedDB from first arg (self) if available.
        self_or_none = args[0] if args else None
        shared: SharedDB | None = getattr(self_or_none, "_shared", None)
        return await run_with_retry(lambda: method(*args, **kwargs), shared)

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
