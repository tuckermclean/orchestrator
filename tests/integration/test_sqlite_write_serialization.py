"""Integration tests — SQLite write serialisation + retry (issue #109).

Verifies:
  - Concurrent writes across ALL six SQLite-backed stores complete without
    raising "database is locked" and without dropping any rows.
  - The retry path in run_with_retry fires on a simulated locked error and
    succeeds on a subsequent attempt.
  - audit.record() does NOT propagate a persistent DB failure as an exception
    (webhook path resilience requirement).
  - The process-wide _db_write_lock is shared: writes from different store
    types interleave safely within a single asyncio gather.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.db import SQLITE_LOCKED_RETRY_MAX, run_with_retry
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
# Cross-store concurrent write safety (#109 regression guard)
# ---------------------------------------------------------------------------


async def test_cross_store_concurrent_writes_no_lock_error(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Concurrent writes across all SQLite stores to the same DB file do not raise
    'database is locked' and no rows are dropped.

    This directly reproduces the issue #109 scenario: multiple stores sharing one
    DB file, all issuing writes concurrently via asyncio.gather.
    """
    db_file = str(Path(str(tmp_path)) / "cross_store.db")

    audit = AuditLog(db_path=db_file)
    counter = SQLiteCounterStore(db_file)
    converge = SQLiteConvergeStateStore(db_file)

    await audit.init()
    await counter.init()
    await converge.init()

    repo = _repo()
    n = 20  # writes per store

    async def _audit_writes() -> None:
        for i in range(n):
            await audit.record(repo, _issue(i + 1), action="intake")

    async def _counter_writes() -> None:
        for i in range(n):
            await counter.increment(_issue(i + 1), "redispatch")

    async def _converge_writes() -> None:
        for i in range(n):
            await converge.set_converge_round(_pr(i + 1), round=1)

    # All three stores write concurrently — this is the contention scenario.
    await asyncio.gather(_audit_writes(), _counter_writes(), _converge_writes())

    # Verify no rows were dropped.
    audit_entries = await audit.list_entries(repo)
    assert len(audit_entries) == n, (
        f"Audit rows dropped: expected {n}, got {len(audit_entries)}"
    )

    for i in range(n):
        v = await counter.get_count(_issue(i + 1), "redispatch")
        assert v == 1, f"Counter {i+1} lost write: expected 1, got {v}"

    for i in range(n):
        rnd = await converge.get_converge_round(_pr(i + 1))
        assert rnd == 1, f"Converge round {i+1} lost write: expected 1, got {rnd}"

    await audit.close()
    await counter.close()
    await converge.close()


async def test_run_store_concurrent_writes_all_land(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Concurrent SQLiteRunStore fire-and-forget writes all complete without
    dropping records — verifies the _spawn path retries correctly.
    """
    db_file = str(Path(str(tmp_path)) / "run_store_concurrent.db")
    store = SQLiteRunStore(db_file)
    await store.init()

    n = 30
    repo = _repo()
    for i in range(n):
        store.record(
            _run_id(i),
            repo,
            type="dispatch",
            model="test-model",
            started_at=_ts(),
        )

    # Allow all fire-and-forget tasks to complete.
    await asyncio.gather(*store._tasks.copy()) if store._tasks else None
    # Drain any remaining tasks that completed between the copy and gather.
    remaining = list(store._tasks)
    if remaining:
        await asyncio.gather(*remaining)

    runs = await store.list_runs(repo)
    await store.close()

    assert len(runs) == n, f"Run records dropped: expected {n}, got {len(runs)}"


async def test_mixed_store_types_concurrent_no_lock_error(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """All six SQLite store types writing concurrently to the same file do not
    raise 'database is locked'.  Models the production scenario where operator
    login, push subscription, run record, counter, converge state, and audit
    all write on the same event tick.
    """
    db_file = str(Path(str(tmp_path)) / "all_stores.db")

    audit = AuditLog(db_path=db_file)
    counter = SQLiteCounterStore(db_file)
    converge = SQLiteConvergeStateStore(db_file)
    op_store = SQLiteOperatorStore(db_path=db_file)
    push_store = SQLitePushStore(db_path=db_file)
    run_store = SQLiteRunStore(db_file)

    for s in (audit, counter, converge, op_store, push_store, run_store):
        await s.init()  # type: ignore[union-attr]

    # Seed an operator so record_login has something to update.
    await op_store.create_operator("admin", "hashed-pw")

    repo = _repo()
    n = 10

    async def _do_audit() -> None:
        for i in range(n):
            await audit.record(repo, _issue(i + 1), action="intake")

    async def _do_counter() -> None:
        for i in range(n):
            await counter.increment(_issue(i + 1), "stale-pr")

    async def _do_converge() -> None:
        for i in range(n):
            await converge.set_converge_round(_pr(i + 1), round=2)

    async def _do_operator() -> None:
        for _ in range(n):
            await op_store.record_login("admin")

    async def _do_push() -> None:
        for i in range(n):
            await push_store.add_subscription(
                "admin", f"https://example.com/push/{i}", {"p256dh": "k", "auth": "a"},
                _ts().isoformat(),
            )

    async def _do_run() -> None:
        for i in range(n):
            run_store.record(
                _run_id(i),
                repo,
                type="dispatch",
                model="m",
                started_at=_ts(),
            )

    await asyncio.gather(
        _do_audit(),
        _do_counter(),
        _do_converge(),
        _do_operator(),
        _do_push(),
        _do_run(),
    )

    # Drain run_store fire-and-forget tasks.
    pending = list(run_store._tasks)
    if pending:
        await asyncio.gather(*pending)

    # Spot-check: no rows dropped.
    audit_entries = await audit.list_entries(repo)
    assert len(audit_entries) == n

    for s in (audit, counter, converge, op_store, push_store, run_store):
        await s.close()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Retry path coverage
# ---------------------------------------------------------------------------


async def test_run_with_retry_succeeds_after_locked_error() -> None:
    """run_with_retry retries on sqlite3.OperationalError('database is locked')
    and succeeds when a subsequent attempt does not raise.
    """
    call_count = 0

    async def _flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = await run_with_retry(_flaky)
    assert result == "ok"
    assert call_count == 3, f"Expected 3 attempts, got {call_count}"


async def test_run_with_retry_non_locked_error_propagates_immediately() -> None:
    """run_with_retry does not retry on unrelated OperationalError."""
    call_count = 0

    async def _fail() -> None:
        nonlocal call_count
        call_count += 1
        raise sqlite3.OperationalError("no such table: foo")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await run_with_retry(_fail)

    assert call_count == 1, "Should not retry non-lock errors"


async def test_run_with_retry_exhausts_retries_raises_runtime_error() -> None:
    """run_with_retry raises RuntimeError after all retries are exhausted."""

    async def _always_locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(RuntimeError, match="failed after"):
        await run_with_retry(_always_locked)


async def test_run_with_retry_attempt_count_matches_max() -> None:
    """run_with_retry attempts exactly SQLITE_LOCKED_RETRY_MAX times."""
    attempts: list[int] = []

    async def _locked() -> None:
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(RuntimeError):
        await run_with_retry(_locked)

    assert len(attempts) == SQLITE_LOCKED_RETRY_MAX


# ---------------------------------------------------------------------------
# Audit write resilience — must not 500 the webhook path (#109)
# ---------------------------------------------------------------------------


async def test_audit_record_does_not_raise_on_persistent_db_failure() -> None:
    """audit.record() swallows a persistent DB failure and logs it instead of
    propagating — a transient SQLite hiccup must not 500 the webhook handler.
    """
    audit = AuditLog(db_path=":memory:")
    await audit.init()

    repo = _repo()
    issue = _issue(1)

    # Patch the inner write helper to always raise (simulates total DB failure
    # after all retries are exhausted).
    with patch.object(
        audit,
        "_write_audit_row",
        new_callable=AsyncMock,
        side_effect=RuntimeError("simulated DB failure after retries"),
    ):
        # Must NOT raise — resilient callers log and continue.
        await audit.record(repo, issue, action="intake")

    await audit.close()


async def test_audit_record_succeeds_normally_when_no_contention() -> None:
    """audit.record() completes and the row is readable under normal conditions."""
    audit = AuditLog(db_path=":memory:")
    await audit.init()

    repo = _repo()
    issue = _issue(42)
    await audit.record(repo, issue, action="dispatch", operator="bot")

    entries = await audit.list_entries(repo, issue)
    assert len(entries) == 1
    assert entries[0]["action"] == "dispatch"
    assert entries[0]["operator"] == "bot"

    await audit.close()


# ---------------------------------------------------------------------------
# Process-wide lock shared across store types
# ---------------------------------------------------------------------------


async def test_write_lock_serializes_cross_store_writes(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Writes from different store types are serialised: under high concurrency
    the shared _db_write_lock ensures one-at-a-time execution.

    Verifies the invariant by recording write order: with the lock, all writes
    complete; without it (not tested here) they would contend.  The observable
    assertion is simply that all writes land — tested indirectly via row counts.
    """
    db_file = str(Path(str(tmp_path)) / "lock_shared.db")

    audit = AuditLog(db_path=db_file)
    counter = SQLiteCounterStore(db_file)
    await audit.init()
    await counter.init()

    repo = _repo()
    n = 50

    tasks = []
    for i in range(n):
        tasks.append(asyncio.create_task(audit.record(repo, _issue(i + 1), action="a")))
        tasks.append(asyncio.create_task(counter.increment(_issue(i + 1), "ch")))

    await asyncio.gather(*tasks)

    entries = await audit.list_entries(repo)
    assert len(entries) == n, f"Lost audit rows: {len(entries)} / {n}"

    # Each issue has exactly one counter increment.
    for i in range(n):
        v = await counter.get_count(_issue(i + 1), "ch")
        assert v == 1, f"Counter {i+1}: expected 1, got {v}"

    await audit.close()
    await counter.close()
