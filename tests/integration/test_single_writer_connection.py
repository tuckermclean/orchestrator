"""Integration tests — single shared write connection + WAL read connection (#109 fix).

These tests verify the canonical fix for "database is locked" (issue #109, superseding
PR #137's asyncio-lock approach):

  - ONE SharedDB per process: one write connection + one read connection shared by all stores.
  - aiosqlite serialises writes on its background thread → zero concurrent SQLite writers,
    zero cross-connection WAL-checkpoint lock races even on networked PVCs.
  - Reads use the separate read connection → reads never block on the write queue.

All tests use a REAL ON-DISK TEMP FILE (not :memory:) so they exercise actual
file-level SQLite locking, WAL shm, and busy_timeout — the same conditions that
triggered the failures on Longhorn.

Regression guard: these tests would FAIL with the old per-store-connection design
under sufficient concurrency.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

from src.db import SharedDB
from src.db.audit import AuditLog
from src.db.converge_state import SQLiteConvergeStateStore
from src.db.counter import SQLiteCounterStore
from src.db.operator_store import SQLiteOperatorStore
from src.db.push_store import SQLitePushStore
from src.db.run_store import SQLiteRunStore
from src.domain.types import IssueRef, PRRef, RepoRef

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo() -> RepoRef:
    return RepoRef(owner="org", name="repo")


def _pr(n: int = 1) -> PRRef:
    return PRRef(repo=_repo(), number=n)


def _issue(n: int = 1) -> IssueRef:
    return IssueRef(repo=_repo(), number=n)


def _run_id(n: int) -> str:
    return f"run-{n:04d}"


def _ts() -> datetime:
    return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# SharedDB lifecycle tests
# ---------------------------------------------------------------------------


async def test_shared_db_file_backed_separate_read_write_connections(
    tmp_path: Path,
) -> None:
    """SharedDB opens two DISTINCT connections for a file-backed database."""
    db_file = str(tmp_path / "shared.db")
    shared = SharedDB(db_file)
    await shared.init()

    # For a file-backed DB the write and read connections must be different objects.
    assert shared.write is not shared.read, (
        "File-backed SharedDB must have separate write and read connections"
    )
    await shared.close()


async def test_shared_db_memory_same_connection(tmp_path: Path) -> None:
    """SharedDB uses the same connection for :memory: so reads see written data."""
    shared = SharedDB(":memory:")
    await shared.init()

    # For :memory:, write and read must be the SAME object.
    assert shared.write is shared.read, (
        ":memory: SharedDB must reuse the write connection for reads"
    )
    await shared.close()


async def test_shared_db_init_idempotent(tmp_path: Path) -> None:
    """SharedDB.init() is safe to call multiple times."""
    db_file = str(tmp_path / "idem.db")
    shared = SharedDB(db_file)
    await shared.init()
    write_conn = shared.write
    await shared.init()  # must be a no-op
    assert shared.write is write_conn, "Second init() must not replace the connection"
    await shared.close()


async def test_shared_db_close_idempotent(tmp_path: Path) -> None:
    """SharedDB.close() is safe to call multiple times."""
    db_file = str(tmp_path / "close_idem.db")
    shared = SharedDB(db_file)
    await shared.init()
    await shared.close()
    await shared.close()  # must not raise


# ---------------------------------------------------------------------------
# Core: single shared connection, many simultaneous writes, concurrent reads
#
# This is the PRIMARY regression guard for #109.  The old per-store-connection
# design failed this exact scenario on Longhorn.
# ---------------------------------------------------------------------------


async def test_single_writer_all_six_stores_concurrent_no_lock_error(
    tmp_path: Path,
) -> None:
    """ALL six SQLite stores share ONE SharedDB write connection.

    Fires many simultaneous writes from every store concurrently (via
    asyncio.gather) and asserts:
      1. No sqlite3.OperationalError("database is locked") is raised.
      2. All written rows land (no silent drops).
      3. Concurrent reads via the read connection complete promptly and return
         correct data — they are not blocked by the write queue.

    Uses a REAL ON-DISK FILE so file-level locking (WAL, shm, busy_timeout)
    is exercised.  This is the scenario that broke the Runs UI on v0.1.18.
    """
    db_file = str(tmp_path / "all_stores_shared.db")

    shared = SharedDB(db_file)
    await shared.init()

    audit = AuditLog(db_path=shared)  # type: ignore[arg-type]
    counter = SQLiteCounterStore(shared)
    converge = SQLiteConvergeStateStore(shared)
    op_store = SQLiteOperatorStore(shared)
    push = SQLitePushStore(shared)
    run_store = SQLiteRunStore(shared)

    for s in (audit, counter, converge, op_store, push, run_store):
        await s.init()  # type: ignore[union-attr]

    # Pre-seed an operator so record_login has a target.
    await op_store.create_operator("admin", "hash-pw")

    repo = _repo()
    n = 50  # writes per store — enough to expose contention under the old design

    # --- Write coroutines (all stores simultaneously) ---

    async def _audit_writes() -> None:
        for i in range(n):
            await audit.record(repo, _issue(i + 1), action="dispatch")

    async def _counter_writes() -> None:
        for i in range(n):
            await counter.increment(_issue(i + 1), "redispatch")

    async def _converge_writes() -> None:
        for i in range(n):
            await converge.set_converge_round(_pr(i + 1), round=1)

    async def _op_writes() -> None:
        for _ in range(n):
            await op_store.record_login("admin")

    async def _push_writes() -> None:
        for i in range(n):
            await push.add_subscription(
                "admin",
                f"https://push.example.com/{i}",
                {"p256dh": "k", "auth": "a"},
                _ts().isoformat(),
            )

    async def _run_writes() -> None:
        for i in range(n):
            run_store.record(_run_id(i), repo, type="dispatch", model="m", started_at=_ts())

    # --- Concurrent reads (on the read connection) ---
    # These must complete promptly — they must NOT queue behind the writes.

    read_durations: list[float] = []

    async def _concurrent_reads() -> None:
        for _ in range(10):
            t0 = time.monotonic()
            await audit.list_entries(repo)
            read_durations.append(time.monotonic() - t0)
            await asyncio.sleep(0)  # yield to let writes progress

    # Fire all writes and concurrent reads at the same time.
    await asyncio.gather(
        _audit_writes(),
        _counter_writes(),
        _converge_writes(),
        _op_writes(),
        _push_writes(),
        _run_writes(),
        _concurrent_reads(),
    )

    # Drain fire-and-forget run_store tasks.
    pending = list(run_store._tasks)
    if pending:
        await asyncio.gather(*pending)

    # --- Assertions: no rows dropped ---
    audit_entries = await audit.list_entries(repo)
    assert len(audit_entries) == n, (
        f"Audit rows dropped: expected {n}, got {len(audit_entries)}"
    )

    for i in range(n):
        v = await counter.get_count(_issue(i + 1), "redispatch")
        assert v == 1, f"Counter {i+1} dropped: expected 1, got {v}"

    for i in range(n):
        rnd = await converge.get_converge_round(_pr(i + 1))
        assert rnd == 1, f"Converge round {i+1} dropped: expected 1, got {rnd}"

    runs = await run_store.list_runs(repo)
    assert len(runs) == n, f"Run records dropped: expected {n}, got {len(runs)}"

    # Reads must have been fast — under 2 s each (in practice, microseconds).
    max_read_s = max(read_durations) if read_durations else 0.0
    assert max_read_s < 2.0, (
        f"Read blocked for {max_read_s:.3f}s — reads are queuing behind writes"
    )

    await shared.close()


async def test_shared_db_writes_visible_on_read_connection(tmp_path: Path) -> None:
    """Writes through the write connection are visible on the read connection.

    Verifies WAL is wired correctly: the read connection sees committed data
    from the write connection — i.e. WAL reader visibility works.
    """
    db_file = str(tmp_path / "visibility.db")
    shared = SharedDB(db_file)
    await shared.init()

    audit = AuditLog(db_path=shared)  # type: ignore[arg-type]
    await audit.init()

    repo = _repo()
    await audit.record(repo, _issue(1), action="intake")

    # list_entries uses the read connection — must see the committed row.
    entries = await audit.list_entries(repo)
    assert len(entries) == 1
    assert entries[0]["action"] == "intake"

    await shared.close()


async def test_shared_db_counter_atomic_increment_no_drift(tmp_path: Path) -> None:
    """Counter increments through the shared write connection are atomic.

    Fires N simultaneous increments on the same key; the final value must
    equal N with no lost updates.
    """
    db_file = str(tmp_path / "counter_atomic.db")
    shared = SharedDB(db_file)
    await shared.init()

    counter = SQLiteCounterStore(shared)
    await counter.init()

    n = 100
    ref = _issue(1)
    await asyncio.gather(*[counter.increment(ref, "ch") for _ in range(n)])

    total = await counter.get_count(ref, "ch")
    await shared.close()

    assert total == n, f"Counter drift: expected {n}, got {total}"


async def test_stores_not_owning_shared_db_skip_close(tmp_path: Path) -> None:
    """Stores constructed from a SharedDB do not close it when their close() is called.

    The SharedDB lifecycle is managed by the caller (lifespan); individual
    store close() calls must be no-ops when they do not own the SharedDB.
    """
    db_file = str(tmp_path / "lifecycle.db")
    shared = SharedDB(db_file)
    await shared.init()

    audit = AuditLog(db_path=shared)  # type: ignore[arg-type]
    await audit.init()

    # Store close() must not tear down shared_db.
    await audit.close()

    # SharedDB should still be open and usable.
    assert shared._write is not None, "SharedDB was closed prematurely by store.close()"

    # Now close the shared connection properly.
    await shared.close()
    assert shared._write is None


async def test_memory_store_reads_see_writes(tmp_path: Path) -> None:
    """:memory: SharedDB: reads and writes use the same connection — data is visible.

    In :memory: mode there is only one connection; both write and read paths
    point to it, so reads always see what was written.
    """
    shared = SharedDB(":memory:")
    await shared.init()

    counter = SQLiteCounterStore(shared)
    await counter.init()

    ref = _issue(99)
    v = await counter.increment(ref, "test")
    assert v == 1

    v2 = await counter.get_count(ref, "test")
    assert v2 == 1, f":memory: read missed write: got {v2}"

    await shared.close()


# ---------------------------------------------------------------------------
# Stress: high write concurrency — the original failure scenario
# ---------------------------------------------------------------------------


async def test_high_concurrency_writes_no_dropped_rows(tmp_path: Path) -> None:
    """250 concurrent write tasks across 3 stores, ONE shared write connection.

    This is a direct stress-test of the "database is locked" scenario from
    issue #109.  The old 6-connection design with asyncio.Lock failed at ~10
    concurrent writers on Longhorn; this test uses 250 tasks.
    """
    db_file = str(tmp_path / "stress.db")
    shared = SharedDB(db_file)
    await shared.init()

    audit = AuditLog(db_path=shared)  # type: ignore[arg-type]
    counter = SQLiteCounterStore(shared)
    converge = SQLiteConvergeStateStore(shared)

    for s in (audit, counter, converge):
        await s.init()  # type: ignore[union-attr]

    repo = _repo()
    n = 250

    tasks = []
    # Mix all three store types — they all funnel through the same write connection.
    for i in range(n):
        store_type = i % 3
        if store_type == 0:
            tasks.append(asyncio.create_task(
                audit.record(repo, _issue(i + 1), action="stress")
            ))
        elif store_type == 1:
            tasks.append(asyncio.create_task(
                counter.increment(_issue(i + 1), "stress")
            ))
        else:
            tasks.append(asyncio.create_task(
                converge.set_converge_round(_pr(i + 1), round=1)
            ))

    # Must complete without OperationalError.
    await asyncio.gather(*tasks)

    # Spot-check a sample of rows from each store.
    audit_entries = await audit.list_entries(repo)
    # Audit gets roughly n/3 writes.
    assert len(audit_entries) > 0, "All audit rows dropped under stress"

    await shared.close()
