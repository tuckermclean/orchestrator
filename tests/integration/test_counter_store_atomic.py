"""Integration tests — SQLiteCounterStore atomic increment (SPEC §8.2a).

Verifies that concurrent increments produce no lost updates and that the
real CounterStore satisfies the CounterStore port contract.
"""

from __future__ import annotations

import asyncio

import pytest

from src.db.counter import SQLiteCounterStore
from src.domain.types import IssueRef, PRRef, RepoRef

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def store() -> SQLiteCounterStore:  # type: ignore[misc]
    s = SQLiteCounterStore(":memory:")
    await s.init()
    yield s
    await s.close()


def _pr_ref(n: int = 1) -> PRRef:
    return PRRef(repo=RepoRef(owner="org", name="repo"), number=n)


def _issue_ref(n: int = 1) -> IssueRef:
    return IssueRef(repo=RepoRef(owner="org", name="repo"), number=n)


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counter_store_initial_value_is_zero(store: SQLiteCounterStore) -> None:
    """get_count returns 0 for an entity that has never been incremented."""
    assert await store.get_count(_pr_ref(), "stale-pr") == 0


@pytest.mark.asyncio
async def test_counter_store_increment_returns_new_value(
    store: SQLiteCounterStore,
) -> None:
    """increment returns the new value (1 on first call)."""
    result = await store.increment(_pr_ref(), "stale-pr")
    assert result == 1


@pytest.mark.asyncio
async def test_counter_store_increment_increments(store: SQLiteCounterStore) -> None:
    """Two increments produce count == 2."""
    await store.increment(_pr_ref(), "stale-pr")
    await store.increment(_pr_ref(), "stale-pr")
    assert await store.get_count(_pr_ref(), "stale-pr") == 2


@pytest.mark.asyncio
async def test_counter_store_reset_zeroes(store: SQLiteCounterStore) -> None:
    """reset sets the counter back to 0."""
    await store.increment(_pr_ref(), "stale-pr")
    await store.increment(_pr_ref(), "stale-pr")
    await store.reset(_pr_ref(), "stale-pr")
    assert await store.get_count(_pr_ref(), "stale-pr") == 0


@pytest.mark.asyncio
async def test_counter_store_channels_independent(store: SQLiteCounterStore) -> None:
    """Different channels for the same entity are tracked independently."""
    ref = _pr_ref(1)
    await store.increment(ref, "stale-pr")
    await store.increment(ref, "stale-pr")
    await store.increment(ref, "converge-retry")

    assert await store.get_count(ref, "stale-pr") == 2
    assert await store.get_count(ref, "converge-retry") == 1


@pytest.mark.asyncio
async def test_counter_store_entities_independent(store: SQLiteCounterStore) -> None:
    """Different entity refs for the same channel are tracked independently."""
    pr1 = _pr_ref(1)
    pr2 = _pr_ref(2)
    await store.increment(pr1, "stale-pr")
    await store.increment(pr1, "stale-pr")
    await store.increment(pr2, "stale-pr")

    assert await store.get_count(pr1, "stale-pr") == 2
    assert await store.get_count(pr2, "stale-pr") == 1


@pytest.mark.asyncio
async def test_counter_store_issue_and_pr_independent(
    store: SQLiteCounterStore,
) -> None:
    """Issue refs and PR refs with the same number do not collide."""
    pr = _pr_ref(1)
    issue = _issue_ref(1)
    await store.increment(pr, "stale-pr")
    await store.increment(issue, "stale-pr")

    assert await store.get_count(pr, "stale-pr") == 1
    assert await store.get_count(issue, "stale-pr") == 1


# ---------------------------------------------------------------------------
# Atomic concurrent increments — no lost updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_counter_store_atomic_concurrent_increments(
    store: SQLiteCounterStore,
) -> None:
    """Concurrent increments produce no lost updates.

    Spawns N concurrent tasks each incrementing the same counter. The final
    count must equal N — if any update were lost the count would be less.
    """
    ref = _pr_ref(99)
    n = 20

    async def _inc() -> None:
        await store.increment(ref, "orphan")

    await asyncio.gather(*[_inc() for _ in range(n)])
    assert await store.get_count(ref, "orphan") == n


@pytest.mark.asyncio
async def test_counter_store_atomic_concurrent_mixed_channels(
    store: SQLiteCounterStore,
) -> None:
    """Concurrent increments on different channels do not interfere."""
    ref = _pr_ref(42)
    n = 10

    async def _inc_stale() -> None:
        await store.increment(ref, "stale-pr")

    async def _inc_retry() -> None:
        await store.increment(ref, "converge-retry")

    tasks = [_inc_stale() for _ in range(n)] + [_inc_retry() for _ in range(n)]
    await asyncio.gather(*tasks)

    assert await store.get_count(ref, "stale-pr") == n
    assert await store.get_count(ref, "converge-retry") == n


# ---------------------------------------------------------------------------
# Port protocol conformance
# ---------------------------------------------------------------------------


def test_sqlite_counter_store_satisfies_protocol() -> None:
    """SQLiteCounterStore satisfies the CounterStore protocol (SPEC §8.2a)."""
    from src.ports.base import CounterStore

    store_instance = SQLiteCounterStore()
    assert isinstance(store_instance, CounterStore)
