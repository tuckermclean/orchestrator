"""Integration tests for #93 — dispatched runs surfaced via list_runs/get_run.

Verifies:
- Dispatching via Engine (issues:labeled) records the run in OrchestratorService.list_runs.
- list_runs is scoped to the correct repo (no cross-repo leakage).
- get_run returns the run detail after dispatch.
- promote() dispatches record in the run store.
- dev_dispatch() records a run.
- FakeRunStore isolation: runs for repo-A do not appear in list_runs for repo-B.
"""

from __future__ import annotations

import asyncio

from src.db.run_store import FakeRunStore
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_TRIAGE,
    IssueRef,
    RepoRef,
)
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService


def _make_service(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    session: FakeSessionPort,
    run_store: FakeRunStore | None = None,
) -> OrchestratorService:
    from src.db.audit import AuditLog

    audit = AuditLog()
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["trusted-user"],
        owner="acme",
        run_store=run_store,
    )


# ---------------------------------------------------------------------------
# Basic: dispatch via handle_event records run in list_runs
# ---------------------------------------------------------------------------


async def test_dispatched_run_appears_in_list_runs() -> None:
    """After dispatching an issue event, the run appears in list_runs for that repo.

    The dispatch sub-machine (orchestrator→implementer) runs in the background;
    we drain it before asserting on list_runs.
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=1)

    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK], author="trusted-user")

    service = _make_service(forge, harness, session)
    await service.startup()

    await service.handle_event("issues", {
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},  # SPEC §11.1: labeled:agent-work → dispatch
        "issue": {"number": 1},
        "repository": {"name": "service", "owner": {"login": "acme"}},
    })

    # Drain background dispatch sub-machine before asserting.
    tasks = list(service._dispatch_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    runs = await service.list_runs(repo)
    assert len(runs) >= 1
    assert any(r.repo.owner == "acme" and r.repo.name == "service" for r in runs)


async def test_dispatched_run_not_visible_for_other_repo() -> None:
    """Runs dispatched for repo-A must NOT appear in list_runs for repo-B.

    The dispatch sub-machine (orchestrator→implementer) runs in the background;
    we drain it before asserting on list_runs.
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    repo_a = RepoRef(owner="acme", name="alpha")
    repo_b = RepoRef(owner="acme", name="beta")
    issue_ref = IssueRef(repo=repo_a, number=1)

    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK], author="trusted-user")

    service = _make_service(forge, harness, session)
    await service.startup()

    await service.handle_event("issues", {
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},  # SPEC §11.1: labeled:agent-work → dispatch
        "issue": {"number": 1},
        "repository": {"name": "alpha", "owner": {"login": "acme"}},
    })

    # Drain background dispatch sub-machine before asserting.
    tasks = list(service._dispatch_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    runs_a = await service.list_runs(repo_a)
    runs_b = await service.list_runs(repo_b)

    assert len(runs_a) >= 1
    assert len(runs_b) == 0


# ---------------------------------------------------------------------------
# get_run returns detail after dispatch
# ---------------------------------------------------------------------------


async def test_get_run_returns_dispatched_run() -> None:
    """get_run(run_id) returns the run detail populated at dispatch time.

    The dispatch sub-machine (orchestrator→implementer) runs in the background;
    we drain it before asserting.  Only the orchestrator is dispatched here
    (no implementing PR is seeded), so exactly 1 dispatch_call is expected.
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=2)

    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK], author="trusted-user")

    run_store = FakeRunStore()
    service = _make_service(forge, harness, session, run_store=run_store)
    await service.startup()

    await service.handle_event("issues", {
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},  # SPEC §11.1: labeled:agent-work → dispatch
        "issue": {"number": 2},
        "repository": {"name": "service", "owner": {"login": "acme"}},
    })

    # Drain background dispatch sub-machine before asserting.
    tasks = list(service._dispatch_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Only the orchestrator run is dispatched — no implementing PR was seeded,
    # so the sub-machine stops after the orchestrator (see Engine.dispatch §10.1).
    assert len(harness.dispatch_calls) == 1
    runs = await service.list_runs(repo)
    assert len(runs) == 1

    run_id = runs[0].run_id
    detail = await service.get_run(run_id)
    assert detail.run_id == run_id
    assert detail.repo.owner == "acme"
    assert detail.repo.name == "service"


# ---------------------------------------------------------------------------
# promote() records a run
# ---------------------------------------------------------------------------


async def test_promote_records_run() -> None:
    """promote() dispatches a run that appears in list_runs."""
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=3)

    forge.seed_issue(
        issue_ref,
        labels=[LABEL_TRIAGE, LABEL_AWAITING_PROMOTION],
        author="external-user",
    )

    from src.db.audit import AuditLog
    from src.db.converge_state import SQLiteConvergeStateStore
    from src.db.counter import SQLiteCounterStore

    audit = AuditLog()
    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="acme",
        counter=SQLiteCounterStore(":memory:"),
        converge_state=SQLiteConvergeStateStore(":memory:"),
    )
    await service.startup()
    await service.engine.counter.init()  # type: ignore[union-attr]
    await service.engine.converge_state.init()  # type: ignore[union-attr]

    handle = await service.promote(issue_ref, operator="admin")
    assert handle is not None

    runs = await service.list_runs(repo)
    assert any(r.run_id == handle.run_id for r in runs)


# ---------------------------------------------------------------------------
# dev_dispatch() records a run
# ---------------------------------------------------------------------------


async def test_dev_dispatch_records_run() -> None:
    """dev_dispatch() records a run visible via list_runs."""
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    repo = RepoRef(owner="acme", name="service")

    service = _make_service(forge, harness, session)
    await service.startup()

    handle = await service.dev_dispatch(repo)

    runs = await service.list_runs(repo)
    assert any(r.run_id == handle.run_id for r in runs)


# ---------------------------------------------------------------------------
# FakeRunStore isolation (white-box)
# ---------------------------------------------------------------------------


async def test_fake_run_store_repo_isolation() -> None:
    """FakeRunStore.list_runs returns only runs for the queried repo."""
    from datetime import UTC, datetime

    store = FakeRunStore()
    repo_a = RepoRef(owner="acme", name="alpha")
    repo_b = RepoRef(owner="acme", name="beta")

    store.record("run-1", repo_a, type="implementer", model="m", started_at=datetime.now(tz=UTC))
    store.record("run-2", repo_b, type="implementer", model="m", started_at=datetime.now(tz=UTC))

    runs_a = await store.list_runs(repo_a)
    runs_b = await store.list_runs(repo_b)

    assert len(runs_a) == 1
    assert runs_a[0].run_id == "run-1"
    assert len(runs_b) == 1
    assert runs_b[0].run_id == "run-2"


async def test_fake_run_store_get_run_returns_none_for_unknown() -> None:
    """FakeRunStore.get_run returns None for an unrecognised run_id."""
    store = FakeRunStore()
    result = await store.get_run("nonexistent-run-id")
    assert result is None


async def test_fake_run_store_set_status_updates_summary() -> None:
    """FakeRunStore.set_status is reflected in list_runs status field."""
    from datetime import UTC, datetime

    store = FakeRunStore()
    repo = RepoRef(owner="acme", name="alpha")

    store.record("run-x", repo, type="implementer", model="m", started_at=datetime.now(tz=UTC))
    store.set_status("run-x", "completed", completed_at=datetime.now(tz=UTC))

    runs = await store.list_runs(repo)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].completed_at is not None
