"""Contract tests for SQLiteConvergeStateStore — parity with FakeConvergeStateStore.

Mirrors tests/contracts/test_converge_state_store.py exactly, substituting
``SQLiteConvergeStateStore`` for the fake.  Both suites must pass identically to
prove the SQLite impl behaves as the contract specifies (SPEC §9.4 / TESTING.md §3.6).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from src.db.converge_state import SQLiteConvergeStateStore
from src.domain.types import PRRef, RepoRef

_REPO = RepoRef(owner="acme", name="repo")
_PR = PRRef(repo=_REPO, number=1)
_PR_B = PRRef(repo=_REPO, number=2)


@pytest.fixture
async def store() -> AsyncGenerator[SQLiteConvergeStateStore, None]:
    """Yield an initialised in-memory SQLiteConvergeStateStore; close on teardown."""
    s = SQLiteConvergeStateStore(":memory:")
    await s.init()
    try:
        yield s
    finally:
        await s.close()


async def test_sqlite_converge_state_get_round_initial(
    store: SQLiteConvergeStateStore,
) -> None:
    assert await store.get_converge_round(_PR) == 0


async def test_sqlite_converge_state_set_get_round(
    store: SQLiteConvergeStateStore,
) -> None:
    await store.set_converge_round(_PR, 2)
    assert await store.get_converge_round(_PR) == 2


async def test_sqlite_converge_state_round_starts_at_1(
    store: SQLiteConvergeStateStore,
) -> None:
    """Fresh PR: get_converge_round() + 1 == 1 (converge loop starts at round 1)."""
    assert await store.get_converge_round(_PR) + 1 == 1


async def test_sqlite_converge_state_round_started_none_initial(
    store: SQLiteConvergeStateStore,
) -> None:
    assert await store.get_round_started(_PR) is None


async def test_sqlite_converge_state_set_get_round_started(
    store: SQLiteConvergeStateStore,
) -> None:
    from datetime import UTC, datetime

    t = datetime.now(tz=UTC)
    await store.set_round_started(_PR, t)
    result = await store.get_round_started(_PR)
    assert result is not None
    # Compare at second resolution (ISO roundtrip loses sub-microsecond precision).
    assert result.replace(microsecond=0) == t.replace(microsecond=0)


async def test_sqlite_converge_state_clear_resets_all(
    store: SQLiteConvergeStateStore,
) -> None:
    from datetime import UTC, datetime

    await store.set_converge_round(_PR, 3)
    await store.set_round_started(_PR, datetime.now(tz=UTC))
    await store.clear_converge_state(_PR)
    assert await store.get_converge_round(_PR) == 0
    assert await store.get_round_started(_PR) is None


async def test_sqlite_converge_state_isolation(
    store: SQLiteConvergeStateStore,
) -> None:
    from datetime import UTC, datetime

    await store.set_converge_round(_PR, 2)
    await store.set_round_started(_PR, datetime.now(tz=UTC))
    assert await store.get_converge_round(_PR_B) == 0
    assert await store.get_round_started(_PR_B) is None


async def test_sqlite_converge_state_run_handle_none_initial(
    store: SQLiteConvergeStateStore,
) -> None:
    """get_last_run_handle returns None when no handle has been stored."""
    assert await store.get_last_run_handle(_PR) is None


async def test_sqlite_converge_state_set_get_run_handle(
    store: SQLiteConvergeStateStore,
) -> None:
    """set_last_run_handle persists the handle; get_last_run_handle retrieves it."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-99")
    await store.set_last_run_handle(_PR, handle)
    retrieved = await store.get_last_run_handle(_PR)
    assert retrieved is not None
    assert retrieved.run_id == "test-run-99"


async def test_sqlite_converge_state_clear_resets_run_handle(
    store: SQLiteConvergeStateStore,
) -> None:
    """clear_converge_state also clears the persisted run handle."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-100")
    await store.set_last_run_handle(_PR, handle)
    await store.clear_converge_state(_PR)
    assert await store.get_last_run_handle(_PR) is None


async def test_sqlite_converge_state_run_handle_isolation(
    store: SQLiteConvergeStateStore,
) -> None:
    """Run handle for one PR does not leak to another PR."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-101")
    await store.set_last_run_handle(_PR, handle)
    assert await store.get_last_run_handle(_PR_B) is None


async def test_sqlite_converge_state_multiple_upserts(
    store: SQLiteConvergeStateStore,
) -> None:
    """Repeated set_converge_round calls update (not duplicate) the row."""
    await store.set_converge_round(_PR, 1)
    await store.set_converge_round(_PR, 2)
    await store.set_converge_round(_PR, 3)
    assert await store.get_converge_round(_PR) == 3


async def test_sqlite_converge_state_init_idempotent(
    store: SQLiteConvergeStateStore,
) -> None:
    """Calling init() a second time on an already-open store is a no-op."""
    await store.init()  # second call — must not raise or re-open
    await store.set_converge_round(_PR, 5)
    assert await store.get_converge_round(_PR) == 5
