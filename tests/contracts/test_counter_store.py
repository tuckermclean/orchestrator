"""Contract tests for CounterStore against FakeCounterStore — SPEC §8.2a / TESTING.md §3.5."""

from __future__ import annotations

import asyncio

import pytest

from src.domain.types import PRRef, RepoRef
from src.ports.fakes import FakeCounterStore

_REPO = RepoRef(owner="acme", name="repo")
_PR = PRRef(repo=_REPO, number=1)


@pytest.fixture
def counter_store() -> FakeCounterStore:
    return FakeCounterStore()


async def test_counter_get_zero_initial(counter_store: FakeCounterStore) -> None:
    assert await counter_store.get_count(_PR, "stale-pr") == 0


async def test_counter_increment_returns_new_value(counter_store: FakeCounterStore) -> None:
    assert await counter_store.increment(_PR, "stale-pr") == 1


async def test_counter_increment_twice(counter_store: FakeCounterStore) -> None:
    await counter_store.increment(_PR, "stale-pr")
    await counter_store.increment(_PR, "stale-pr")
    assert await counter_store.get_count(_PR, "stale-pr") == 2


async def test_counter_channel_isolation(counter_store: FakeCounterStore) -> None:
    await counter_store.increment(_PR, "stale-pr")
    await counter_store.increment(_PR, "stale-pr")
    await counter_store.increment(_PR, "orphan")
    assert await counter_store.get_count(_PR, "stale-pr") == 2
    assert await counter_store.get_count(_PR, "orphan") == 1


async def test_counter_stale_pr_channel(counter_store: FakeCounterStore) -> None:
    for _ in range(3):
        await counter_store.increment(_PR, "stale-pr")
    assert await counter_store.get_count(_PR, "stale-pr") == 3


async def test_counter_reset_returns_zero(counter_store: FakeCounterStore) -> None:
    for _ in range(3):
        await counter_store.increment(_PR, "stale-pr")
    await counter_store.reset(_PR, "stale-pr")
    assert await counter_store.get_count(_PR, "stale-pr") == 0


async def test_counter_atomic_increment_concurrent(counter_store: FakeCounterStore) -> None:
    results = await asyncio.gather(
        counter_store.increment(_PR, "stale-pr"),
        counter_store.increment(_PR, "stale-pr"),
    )
    assert sorted(results) == [1, 2]  # unique values; no lost update
    assert await counter_store.get_count(_PR, "stale-pr") == 2
