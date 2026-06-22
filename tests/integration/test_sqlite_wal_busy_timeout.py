"""Integration tests — SQLite WAL journal mode and busy_timeout (issue #96).

Verifies:
  - PRAGMA journal_mode=WAL is set on file-backed connections for all three
    SQLite-backed stores (AuditLog, SQLiteCounterStore, SQLiteConvergeStateStore).
  - PRAGMA busy_timeout is set to the expected value.
  - Concurrent writes to a file-backed store do not raise "database is locked".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from src.db import SQLITE_BUSY_TIMEOUT_MS
from src.db.audit import AuditLog
from src.db.converge_state import SQLiteConvergeStateStore
from src.db.counter import SQLiteCounterStore
from src.domain.types import PRRef, RepoRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr(n: int = 1) -> PRRef:
    return PRRef(repo=RepoRef(owner="org", name="repo"), number=n)


def _repo() -> RepoRef:
    return RepoRef(owner="org", name="repo")


async def _get_journal_mode(conn: aiosqlite.Connection) -> str:
    """Query PRAGMA journal_mode from an already-opened aiosqlite connection."""
    async with conn.execute("PRAGMA journal_mode") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return str(row[0]).lower()


async def _get_busy_timeout(conn: aiosqlite.Connection) -> int:
    """Query PRAGMA busy_timeout from an already-opened aiosqlite connection."""
    async with conn.execute("PRAGMA busy_timeout") as cursor:
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# journal_mode=WAL and busy_timeout assertions (file-backed)
# ---------------------------------------------------------------------------


async def test_counter_store_journal_mode_wal(tmp_path: pytest.TempPathFactory) -> None:
    """SQLiteCounterStore sets journal_mode=WAL on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "counter_wal.db")
    store = SQLiteCounterStore(db_file)
    await store.init()
    assert store._db is not None
    mode = await _get_journal_mode(store._db)
    await store.close()
    assert mode == "wal", f"expected wal, got {mode!r}"


async def test_counter_store_busy_timeout(tmp_path: pytest.TempPathFactory) -> None:
    """SQLiteCounterStore sets busy_timeout=5000 on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "counter_bt.db")
    store = SQLiteCounterStore(db_file)
    await store.init()
    assert store._db is not None
    timeout = await _get_busy_timeout(store._db)
    await store.close()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS


async def test_converge_store_journal_mode_wal(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """SQLiteConvergeStateStore sets journal_mode=WAL on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "converge_wal.db")
    store = SQLiteConvergeStateStore(db_file)
    await store.init()
    assert store._db is not None
    mode = await _get_journal_mode(store._db)
    await store.close()
    assert mode == "wal", f"expected wal, got {mode!r}"


async def test_converge_store_busy_timeout(tmp_path: pytest.TempPathFactory) -> None:
    """SQLiteConvergeStateStore sets busy_timeout=5000 on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "converge_bt.db")
    store = SQLiteConvergeStateStore(db_file)
    await store.init()
    assert store._db is not None
    timeout = await _get_busy_timeout(store._db)
    await store.close()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS


async def test_audit_log_journal_mode_wal(tmp_path: pytest.TempPathFactory) -> None:
    """AuditLog sets journal_mode=WAL on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "audit_wal.db")
    store = AuditLog(db_path=db_file)
    await store.init()
    assert store._db is not None
    mode = await _get_journal_mode(store._db)
    await store.close()
    assert mode == "wal", f"expected wal, got {mode!r}"


async def test_audit_log_busy_timeout(tmp_path: pytest.TempPathFactory) -> None:
    """AuditLog sets busy_timeout=5000 on a file-backed connection."""
    db_file = str(Path(str(tmp_path)) / "audit_bt.db")
    store = AuditLog(db_path=db_file)
    await store.init()
    assert store._db is not None
    timeout = await _get_busy_timeout(store._db)
    await store.close()
    assert timeout == SQLITE_BUSY_TIMEOUT_MS


# ---------------------------------------------------------------------------
# Concurrent write safety — file-backed (issue #96 regression guard)
# ---------------------------------------------------------------------------


async def test_concurrent_counter_increments_no_lock_error(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Concurrent counter increments on a file-backed store do not raise 'database is locked'.

    Opens N independent store instances against the same DB file (simulating
    concurrent writers) and gathers many increment tasks.  With WAL +
    busy_timeout these should all succeed; without them they would fail with
    OperationalError: database is locked.
    """
    db_file = str(Path(str(tmp_path)) / "counter_concurrent.db")

    # Initialise schema via first connection.
    seed = SQLiteCounterStore(db_file)
    await seed.init()
    await seed.close()

    n_stores = 5
    n_increments_each = 10
    ref = _pr(1)

    stores = [SQLiteCounterStore(db_file) for _ in range(n_stores)]
    for s in stores:
        await s.init()

    async def _inc(s: SQLiteCounterStore) -> None:
        for _ in range(n_increments_each):
            await s.increment(ref, "concurrent-test")

    # Gather all increments concurrently; must not raise OperationalError.
    await asyncio.gather(*[_inc(s) for s in stores])

    for s in stores:
        await s.close()

    # Re-open and verify no updates were lost.
    verify = SQLiteCounterStore(db_file)
    await verify.init()
    total = await verify.get_count(ref, "concurrent-test")
    await verify.close()

    assert total == n_stores * n_increments_each, (
        f"Lost updates: expected {n_stores * n_increments_each}, got {total}"
    )


async def test_concurrent_audit_appends_no_lock_error(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Concurrent audit appends on a file-backed store do not raise 'database is locked'."""
    db_file = str(Path(str(tmp_path)) / "audit_concurrent.db")

    n_stores = 5
    n_appends_each = 10
    repo = _repo()
    ref = _pr(1)

    stores = [AuditLog(db_path=db_file) for _ in range(n_stores)]
    for s in stores:
        await s.init()

    async def _append(s: AuditLog) -> None:
        for _ in range(n_appends_each):
            await s.record(repo, ref, action="test-action")

    await asyncio.gather(*[_append(s) for s in stores])

    for s in stores:
        await s.close()

    verify = AuditLog(db_path=db_file)
    await verify.init()
    entries = await verify.list_entries(repo)
    await verify.close()

    assert len(entries) == n_stores * n_appends_each, (
        f"Lost writes: expected {n_stores * n_appends_each}, got {len(entries)}"
    )


async def test_concurrent_converge_state_writes_no_lock_error(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Concurrent converge-state writes on a file-backed store do not raise 'database is locked'."""
    db_file = str(Path(str(tmp_path)) / "converge_concurrent.db")

    # Seed schema.
    seed = SQLiteConvergeStateStore(db_file)
    await seed.init()
    await seed.close()

    n_stores = 5
    stores = [SQLiteConvergeStateStore(db_file) for _ in range(n_stores)]
    for s in stores:
        await s.init()

    # Each store writes to a distinct PR to avoid contention on the same row
    # while still exercising concurrent multi-connection WAL writes.
    async def _write(s: SQLiteConvergeStateStore, pr_num: int) -> None:
        ref = _pr(pr_num)
        for rnd in range(1, 6):
            await s.set_converge_round(ref, rnd)

    await asyncio.gather(*[_write(s, i + 1) for i, s in enumerate(stores)])

    for s in stores:
        await s.close()
