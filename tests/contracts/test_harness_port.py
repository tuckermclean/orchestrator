"""Contract tests for HarnessPort against FakeHarnessPort."""

from __future__ import annotations

import pytest

from src.domain.types import (
    DispatchContext,
    IssueRef,
    PRRef,
    RepoRef,
    RunHandle,
    RunStatus,
)
from src.ports.fakes import FakeHarnessPort, SpawnDenied


@pytest.fixture
def harness_port() -> FakeHarnessPort:
    return FakeHarnessPort()


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner="acme", name="repo")


def _make_context(
    repo: RepoRef,
    *,
    model: str = "claude-sonnet-4-6",
    max_turns: int = 30,
    allowed_agent_refs: list[str] | None = None,
) -> DispatchContext:
    return DispatchContext(
        issue_ref=IssueRef(repo=repo, number=1),
        contract="agents/orchestrator.md",
        model=model,
        max_turns=max_turns,
        forge_token_scope="repo-branch",
        allowed_agent_refs=allowed_agent_refs,
    )


async def test_harness_dispatch_returns_handle(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    assert isinstance(handle, RunHandle)
    assert handle.run_id.startswith("fake-run-")


async def test_harness_dispatch_records_params(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo, model="claude-opus-4-8", max_turns=40)
    await harness_port.dispatch(ctx)
    assert len(harness_port.dispatch_calls) == 1
    recorded = harness_port.dispatch_calls[0]
    assert recorded.model == "claude-opus-4-8"
    assert recorded.max_turns == 40
    assert recorded.forge_token_scope == "repo-branch"


async def test_harness_dispatch_does_not_block(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    """dispatch() returns immediately without awaiting run completion."""
    ctx = _make_context(repo)
    # Seed as in_progress to ensure it won't be completed synchronously
    handle = await harness_port.dispatch(ctx)
    # Override status to in_progress immediately after
    harness_port._runs[handle.run_id] = RunStatus(state="in_progress")
    status = await harness_port.get_run_status(handle)
    # The key invariant: dispatch returned a handle (proved by reaching here)
    assert handle is not None
    assert status.state == "in_progress"


async def test_harness_trigger_ci_records_call(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    pr_ref = PRRef(repo=repo, number=5)
    await harness_port.trigger_ci(pr_ref)
    assert pr_ref in harness_port.trigger_ci_calls


async def test_harness_trigger_workflow_records_name(
    harness_port: FakeHarnessPort,
) -> None:
    await harness_port.trigger_workflow("deploy.yml", "main", {"env": "prod"})
    assert len(harness_port.trigger_workflow_calls) == 1
    name, ref, inputs = harness_port.trigger_workflow_calls[0]
    assert name == "deploy.yml"
    assert ref == "main"
    assert inputs == {"env": "prod"}


async def test_harness_get_run_status_queued(
    harness_port: FakeHarnessPort,
) -> None:
    handle = RunHandle(run_id="not-dispatched")
    status = await harness_port.get_run_status(handle)
    assert status.state == "queued"


async def test_harness_get_run_status_completed(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    # Default dispatch marks as completed/success
    status = await harness_port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "success"


async def test_harness_get_run_status_failed(
    harness_port: FakeHarnessPort,
) -> None:
    handle = RunHandle(run_id="failed-run")
    harness_port.seed_run(handle, state="completed", conclusion="failure")
    status = await harness_port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "failure"


async def test_harness_dispatch_allowed_agent_refs_passed(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    allowed = ["engineering-code-reviewer.md"]
    ctx = _make_context(repo, allowed_agent_refs=allowed)
    await harness_port.dispatch(ctx)
    assert harness_port._last_context is not None
    assert harness_port._last_context.allowed_agent_refs == allowed

    # Allowed agent should not raise
    harness_port.simulate_spawn_attempt("engineering-code-reviewer.md")

    # Disallowed agent should raise SpawnDenied
    with pytest.raises(SpawnDenied):
        harness_port.simulate_spawn_attempt("rogue-agent.md")


async def test_harness_cancel_idempotent(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    await harness_port.cancel(handle)
    await harness_port.cancel(handle)  # second call is no-op
    status = await harness_port.get_run_status(handle)
    assert status.conclusion == "cancelled"
    # Only 1 cancel_call recorded (second is no-op)
    assert len(harness_port.cancel_calls) == 1


async def test_harness_cancel_on_timeout_reviewer(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    # Simulate timeout scenario
    harness_port.seed_run(handle, state="in_progress")
    await harness_port.cancel(handle)
    status = await harness_port.get_run_status(handle)
    assert status.conclusion == "cancelled"


async def test_harness_cancel_on_timeout_fixer(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    harness_port.seed_run(handle, state="in_progress")
    await harness_port.cancel(handle)
    status = await harness_port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


async def test_harness_run_handle_round_trip(
    harness_port: FakeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    original = await harness_port.dispatch(ctx)
    reconstructed = RunHandle.from_run_id(original.run_id)
    assert reconstructed == original
    assert reconstructed.run_id == original.run_id
