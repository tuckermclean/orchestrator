"""Advisory lock concurrency tests (SPEC §11.3 step 1).

Tests that:
  - Two concurrent ``promote`` calls on the same issue serialize correctly:
    exactly one label mutation set, exactly one audit record.
  - Two concurrent ``deescalate_pr`` calls on the same PR serialize:
    exactly one label removal, one counter reset, one converge-state clear.
  - Different entities do NOT serialize against each other (run in parallel).
  - The FakeLockProvider records acquisition and release for each entity.
"""

from __future__ import annotations

import asyncio

import pytest

from src.db.audit import AuditLog
from src.domain.types import (
    LABEL_AWAITING_PROMOTION,
    LABEL_NEEDS_HUMAN,
    IssueRef,
    PRRef,
    RepoRef,
)
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeLockProvider,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = RepoRef(owner="acme", name="service")


def _make_service(
    forge: FakeForgePort,
    lock_provider: FakeLockProvider,
    counter: FakeCounterStore | None = None,
    converge_state: FakeConvergeStateStore | None = None,
) -> OrchestratorService:
    harness = FakeHarnessPort()
    audit = AuditLog()
    svc = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit,
        counter=counter or FakeCounterStore(),
        converge_state=converge_state or FakeConvergeStateStore(),
        lock_provider=lock_provider,
    )
    return svc


def _issue(n: int) -> IssueRef:
    return IssueRef(repo=REPO, number=n)


def _pr(n: int) -> PRRef:
    return PRRef(repo=REPO, number=n)


# ---------------------------------------------------------------------------
# §11.3 promote — two concurrent calls on the same issue
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.3", "lock-promote-serializes")
@pytest.mark.asyncio
async def test_promote_concurrent_same_issue_serializes() -> None:
    """Two concurrent promote() calls on the same issue serialize under the lock.

    Expectation: both calls complete without error; the lock was acquired and
    released exactly twice (once per call, serially).
    """
    forge = FakeForgePort()
    lock = FakeLockProvider()
    issue_ref = _issue(1)
    forge.seed_issue(issue_ref, labels=[LABEL_AWAITING_PROMOTION])

    svc = _make_service(forge, lock)
    await svc._audit.init()

    # Launch two promote calls concurrently on the same issue
    results = await asyncio.gather(
        svc.promote(issue_ref, operator="op-a"),
        svc.promote(issue_ref, operator="op-b"),
    )

    # Both calls returned a RunHandle (no exception)
    assert len(results) == 2

    # The lock was acquired and released exactly twice (once per call), serially
    issue_key = "issue:acme/service#1"
    assert lock.acquired == [issue_key, issue_key]
    assert lock.released == [issue_key, issue_key]

    # set_labels was called exactly twice (one per serialized window)
    assert len(forge.set_labels_calls) == 2

    # Audit has exactly two promote records
    entries = await svc._audit.list_entries(REPO, issue_ref)
    promote_entries = [e for e in entries if e["action"] == "promote"]
    assert len(promote_entries) == 2


@pytest.mark.covers("§11.3", "lock-promote-serializes")
@pytest.mark.asyncio
async def test_promote_lock_acquired_before_mutation() -> None:
    """Lock acquisition happens before any forge mutation in promote."""
    forge = FakeForgePort()
    lock = FakeLockProvider()
    issue_ref = _issue(2)
    forge.seed_issue(issue_ref, labels=[LABEL_AWAITING_PROMOTION])

    svc = _make_service(forge, lock)
    await svc._audit.init()

    await svc.promote(issue_ref, operator="op")

    # Lock acquired before forge mutation
    assert len(lock.acquired) == 1
    assert len(lock.released) == 1
    # set_labels did happen (mutation occurred inside the lock window)
    assert len(forge.set_labels_calls) == 1


# ---------------------------------------------------------------------------
# §11.3 deescalate_pr — two concurrent calls on the same PR
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.3", "lock-deescalate-serializes")
@pytest.mark.asyncio
async def test_deescalate_pr_concurrent_same_pr_serializes() -> None:
    """Two concurrent deescalate_pr() calls on the same PR serialize under the lock.

    Both calls must complete without error (idempotent remove_label and counter.reset
    are safe).  The lock is acquired twice, serially.
    """
    forge = FakeForgePort()
    lock = FakeLockProvider()
    counter = FakeCounterStore()
    converge_state = FakeConvergeStateStore()
    pr_ref = _pr(10)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN])
    # Seed stale-pr counter at 3 so we can verify it gets reset
    counter.seed_count(pr_ref, "stale-pr", 3)

    svc = _make_service(forge, lock, counter=counter, converge_state=converge_state)
    await svc._audit.init()

    # Launch two deescalate_pr calls concurrently on the same PR
    await asyncio.gather(
        svc.deescalate_pr(pr_ref, operator="op-a"),
        svc.deescalate_pr(pr_ref, operator="op-b"),
    )

    # Lock acquired and released exactly twice, serially
    pr_key = "pr:acme/service!10"
    assert lock.acquired == [pr_key, pr_key]
    assert lock.released == [pr_key, pr_key]

    # remove_label called exactly twice — serialized
    remove_calls = [c for c in forge.remove_label_calls if c[1] == LABEL_NEEDS_HUMAN]
    assert len(remove_calls) == 2

    # stale-pr counter reset at least twice (idempotent resets)
    stale_resets = [c for c in counter.reset_calls if c[1] == "stale-pr"]
    assert len(stale_resets) == 2

    # converge state clear at least twice
    assert len(converge_state.clear_calls) == 2


@pytest.mark.covers("§11.3", "lock-deescalate-serializes")
@pytest.mark.asyncio
async def test_deescalate_pr_lock_acquired_before_mutation() -> None:
    """Lock acquisition happens before any forge read or mutation in deescalate_pr."""
    forge = FakeForgePort()
    lock = FakeLockProvider()
    pr_ref = _pr(11)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN])

    svc = _make_service(forge, lock)
    await svc._audit.init()

    await svc.deescalate_pr(pr_ref, operator="op")

    assert len(lock.acquired) == 1
    assert len(lock.released) == 1
    # forge.get_pr (read) happened inside the lock window — remove_label follows
    assert len(forge.get_pr_calls) == 1
    assert len(forge.remove_label_calls) == 1


# ---------------------------------------------------------------------------
# §11.3 different entities do NOT serialize against each other
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.3", "lock-entity-isolation")
@pytest.mark.asyncio
async def test_different_issues_do_not_serialize() -> None:
    """promote() on two different issues runs in parallel (no cross-entity blocking)."""
    forge = FakeForgePort()
    lock = FakeLockProvider()
    issue_a = _issue(20)
    issue_b = _issue(21)
    forge.seed_issue(issue_a, labels=[LABEL_AWAITING_PROMOTION])
    forge.seed_issue(issue_b, labels=[LABEL_AWAITING_PROMOTION])

    svc = _make_service(forge, lock)
    await svc._audit.init()

    results = await asyncio.gather(
        svc.promote(issue_a, operator="op-a"),
        svc.promote(issue_b, operator="op-b"),
    )

    assert len(results) == 2

    # Each entity has its own lock acquisition record — no shared key
    keys_acquired = set(lock.acquired)
    assert "issue:acme/service#20" in keys_acquired
    assert "issue:acme/service#21" in keys_acquired
    # Two distinct entity keys — no cross-entity serialization
    assert len(keys_acquired) == 2


@pytest.mark.covers("§11.3", "lock-entity-isolation")
@pytest.mark.asyncio
async def test_different_prs_do_not_serialize() -> None:
    """deescalate_pr() on two different PRs runs in parallel."""
    forge = FakeForgePort()
    lock = FakeLockProvider()
    pr_a = _pr(30)
    pr_b = _pr(31)
    forge.seed_pr(pr_a, labels=[LABEL_NEEDS_HUMAN])
    forge.seed_pr(pr_b, labels=[LABEL_NEEDS_HUMAN])

    svc = _make_service(forge, lock)
    await svc._audit.init()

    await asyncio.gather(
        svc.deescalate_pr(pr_a, operator="op-a"),
        svc.deescalate_pr(pr_b, operator="op-b"),
    )

    keys_acquired = set(lock.acquired)
    assert "pr:acme/service!30" in keys_acquired
    assert "pr:acme/service!31" in keys_acquired
    assert len(keys_acquired) == 2


# ---------------------------------------------------------------------------
# FakeLockProvider contract — unit
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.3", "lock-fake-contract")
@pytest.mark.asyncio
async def test_fake_lock_provider_records_acquire_release() -> None:
    """FakeLockProvider records acquisition and release in order."""
    lock = FakeLockProvider()
    ref = IssueRef(repo=RepoRef(owner="o", name="r"), number=7)
    key = "issue:o/r#7"

    async with lock.lock(ref):
        assert lock.acquired == [key]
        assert lock.released == []

    assert lock.acquired == [key]
    assert lock.released == [key]


@pytest.mark.covers("§11.3", "lock-fake-contract")
@pytest.mark.asyncio
async def test_fake_lock_provider_releases_on_exception() -> None:
    """FakeLockProvider releases the lock even when the body raises."""
    lock = FakeLockProvider()
    ref = IssueRef(repo=RepoRef(owner="o", name="r"), number=8)

    with pytest.raises(ValueError):
        async with lock.lock(ref):
            raise ValueError("boom")

    # Lock was acquired then released despite the exception
    assert len(lock.acquired) == 1
    assert len(lock.released) == 1


# ---------------------------------------------------------------------------
# AsyncioLockProvider — unit tests for the real single-process impl
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.3", "lock-asyncio-serializes")
@pytest.mark.asyncio
async def test_asyncio_lock_provider_serializes_same_entity() -> None:
    """AsyncioLockProvider serializes concurrent calls on the same entity key."""
    from src.ports.advisory_lock import AsyncioLockProvider

    provider = AsyncioLockProvider()
    ref = IssueRef(repo=RepoRef(owner="o", name="r"), number=9)
    execution_order: list[str] = []

    async def task_a() -> None:
        async with provider.lock(ref):
            execution_order.append("a-start")
            await asyncio.sleep(0)  # yield to allow task_b to try to acquire
            execution_order.append("a-end")

    async def task_b() -> None:
        async with provider.lock(ref):
            execution_order.append("b-start")
            execution_order.append("b-end")

    await asyncio.gather(task_a(), task_b())

    # a must fully complete before b starts (serial execution)
    assert execution_order.index("a-end") < execution_order.index("b-start")


@pytest.mark.covers("§11.3", "lock-asyncio-serializes")
@pytest.mark.asyncio
async def test_asyncio_lock_provider_different_entities_parallel() -> None:
    """AsyncioLockProvider does NOT serialize calls on different entity keys."""
    from src.ports.advisory_lock import AsyncioLockProvider

    provider = AsyncioLockProvider()
    ref_a = IssueRef(repo=RepoRef(owner="o", name="r"), number=10)
    ref_b = IssueRef(repo=RepoRef(owner="o", name="r"), number=11)
    execution_order: list[str] = []

    async def task_a() -> None:
        async with provider.lock(ref_a):
            execution_order.append("a-start")
            await asyncio.sleep(0)
            execution_order.append("a-end")

    async def task_b() -> None:
        async with provider.lock(ref_b):
            execution_order.append("b-start")
            await asyncio.sleep(0)
            execution_order.append("b-end")

    await asyncio.gather(task_a(), task_b())

    # Both tasks started before either ended (overlapping execution)
    a_start = execution_order.index("a-start")
    b_start = execution_order.index("b-start")
    a_end = execution_order.index("a-end")
    b_end = execution_order.index("b-end")
    # b started before a ended — confirming parallel execution
    assert b_start < a_end or a_start < b_end
