"""Contract tests for ConvergeStateStore against the fake — SPEC §9.4 / TESTING.md §3.6."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.domain.types import PRRef, RepoRef
from src.ports.fakes import FakeConvergeStateStore

_REPO = RepoRef(owner="acme", name="repo")
_PR = PRRef(repo=_REPO, number=1)
_PR_B = PRRef(repo=_REPO, number=2)


@pytest.fixture
def store() -> FakeConvergeStateStore:
    return FakeConvergeStateStore()


async def test_converge_state_get_round_initial(store: FakeConvergeStateStore) -> None:
    assert await store.get_converge_round(_PR) == 0


async def test_converge_state_set_get_round(store: FakeConvergeStateStore) -> None:
    await store.set_converge_round(_PR, 2)
    assert await store.get_converge_round(_PR) == 2


async def test_converge_state_round_starts_at_1(store: FakeConvergeStateStore) -> None:
    """Fresh PR: get_converge_round() + 1 == 1 (converge loop starts at round 1)."""
    assert await store.get_converge_round(_PR) + 1 == 1


async def test_converge_state_round_started_none_initial(
    store: FakeConvergeStateStore,
) -> None:
    assert await store.get_round_started(_PR) is None


async def test_converge_state_set_get_round_started(
    store: FakeConvergeStateStore,
) -> None:
    t = datetime.now(tz=UTC)
    await store.set_round_started(_PR, t)
    assert await store.get_round_started(_PR) == t


async def test_converge_state_clear_resets_all(store: FakeConvergeStateStore) -> None:
    await store.set_converge_round(_PR, 3)
    await store.set_round_started(_PR, datetime.now(tz=UTC))
    await store.clear_converge_state(_PR)
    assert await store.get_converge_round(_PR) == 0
    assert await store.get_round_started(_PR) is None


async def test_converge_state_isolation(store: FakeConvergeStateStore) -> None:
    await store.set_converge_round(_PR, 2)
    await store.set_round_started(_PR, datetime.now(tz=UTC))
    assert await store.get_converge_round(_PR_B) == 0
    assert await store.get_round_started(_PR_B) is None
