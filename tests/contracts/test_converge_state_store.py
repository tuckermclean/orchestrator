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


async def test_converge_state_run_handle_none_initial(store: FakeConvergeStateStore) -> None:
    """get_last_run_handle returns None when no handle has been stored."""
    assert await store.get_last_run_handle(_PR) is None


async def test_converge_state_set_get_run_handle(store: FakeConvergeStateStore) -> None:
    """set_last_run_handle persists the handle; get_last_run_handle retrieves it."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-99")
    await store.set_last_run_handle(_PR, handle)
    retrieved = await store.get_last_run_handle(_PR)
    assert retrieved is not None
    assert retrieved.run_id == "test-run-99"


async def test_converge_state_clear_resets_run_handle(store: FakeConvergeStateStore) -> None:
    """clear_converge_state also clears the persisted run handle."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-100")
    await store.set_last_run_handle(_PR, handle)
    await store.clear_converge_state(_PR)
    assert await store.get_last_run_handle(_PR) is None


async def test_converge_state_run_handle_isolation(store: FakeConvergeStateStore) -> None:
    """Run handle for one PR does not leak to another PR."""
    from src.domain.types import RunHandle

    handle = RunHandle(run_id="test-run-101")
    await store.set_last_run_handle(_PR, handle)
    assert await store.get_last_run_handle(_PR_B) is None
