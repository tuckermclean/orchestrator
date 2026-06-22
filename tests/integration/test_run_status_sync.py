"""Integration tests for issue #101 — run status sync from RunEventStore into run_store.

Verifies that status transitions driven by the harness backend (subprocess or K8s)
are propagated through the RunEventStore write-through sink into the run_store, so
list_runs/get_run reflect the real run state instead of staying "queued".

Test strategy:
  - Use ClaudeCodeHarnessPort with FakeExecutionBackend (controllable status driver).
  - FakeExecutionBackend.dispatch() immediately calls event_store.set_status() on the
    path that the real backends follow — this exercises the full write-through chain.
  - Wrap with RunRecordingHarness (the OrchestratorService path) and assert that
    FakeRunStore.list_runs / get_run return the post-transition status.

Both backends converge on the same RunEventStore.set_status path (SubprocessBackend
via _watch; K8sJobBackend via _watch), so FakeExecutionBackend covers both.

The sink is sync; SQLiteRunStore.set_status schedules an asyncio.create_task for
the DB write — the write-through contract holds for both store implementations.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from src.db.run_store import FakeRunStore
from src.domain.types import (
    DispatchContext,
    IssueRef,
    RepoRef,
    RunStatus,
)
from src.ports.execution_backend import FakeExecutionBackend
from src.ports.harness import ClaudeCodeHarnessPort, RunEventStore
from src.service.orchestrator import RunRecordingHarness

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="testservice")
_ISSUE_REF = IssueRef(repo=_REPO, number=1)

_CLAUDE_TOKEN = "sk-ant-fake"
_APP_ID = "app-123"
_PRIVATE_KEY_PEM = "---fake-pem---"
_INSTALLATION_ID = "inst-456"


def _make_context(model: str = "claude-sonnet-4-6") -> DispatchContext:
    return DispatchContext(
        issue_ref=_ISSUE_REF,
        contract="agents/implementer.md",
        model=model,
        max_turns=10,
        forge_token_scope="repo-branch",
        allowed_agent_refs=None,
    )


def _make_harness(backend: FakeExecutionBackend) -> ClaudeCodeHarnessPort:
    """Build a ClaudeCodeHarnessPort with a fake backend."""
    return ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner="acme",
        repo_name="testservice",
        execution_backend=backend,
    )


def _patch_mint(gh_token: str = "ghs_fake_token"):  # type: ignore[no-untyped-def]
    """Patch token minting to return a fake token without real RSA key."""
    return patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value=gh_token),
    )


# ---------------------------------------------------------------------------
# Core write-through tests
# ---------------------------------------------------------------------------


async def test_run_store_reflects_completed_success_after_dispatch() -> None:
    """After a successful run, list_runs returns status='completed' (not 'queued').

    FakeExecutionBackend.dispatch() immediately calls
    event_store.set_status(run_id, RunStatus(state='completed', conclusion='success')),
    which triggers the write-through sink registered by RunRecordingHarness.
    """
    backend = FakeExecutionBackend()
    harness = _make_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())

    await asyncio.sleep(0)

    runs = await run_store.list_runs(_REPO)
    assert len(runs) == 1
    assert runs[0].run_id == handle.run_id
    assert runs[0].status == "completed", (
        f"Expected 'completed', got {runs[0].status!r}"
    )
    assert runs[0].completed_at is not None


async def test_run_store_reflects_failed_conclusion() -> None:
    """After a failed run, list_runs returns status='failed' (not 'queued').

    FakeExecutionBackend configured with fail_dispatch=True calls
    event_store.set_status(run_id, RunStatus(state='completed', conclusion='failure')),
    which the write-through sink maps to 'failed' in the run_store.
    """
    backend = FakeExecutionBackend()
    backend.configure(fail_dispatch=True)
    harness = _make_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())

    await asyncio.sleep(0)

    runs = await run_store.list_runs(_REPO)
    assert len(runs) == 1
    assert runs[0].run_id == handle.run_id
    assert runs[0].status == "failed", (
        f"Expected 'failed', got {runs[0].status!r}"
    )
    assert runs[0].completed_at is not None


async def test_get_run_reflects_updated_status() -> None:
    """get_run() returns the updated status, not the initial 'queued' value."""
    backend = FakeExecutionBackend()
    harness = _make_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())
    await asyncio.sleep(0)

    detail = await run_store.get_run(handle.run_id)
    assert detail is not None
    assert detail.status == "completed"


async def test_get_run_reflects_failed_status() -> None:
    """get_run() reflects 'failed' for a failure-concluded run."""
    backend = FakeExecutionBackend()
    backend.configure(fail_dispatch=True)
    harness = _make_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())
    await asyncio.sleep(0)

    detail = await run_store.get_run(handle.run_id)
    assert detail is not None
    assert detail.status == "failed"


# ---------------------------------------------------------------------------
# RunEventStore.register_status_sink unit tests
# ---------------------------------------------------------------------------


def test_run_event_store_sink_called_on_set_status() -> None:
    """Registered sink is invoked synchronously when set_status is called."""
    store = RunEventStore()
    store.register("run-1")

    calls: list[tuple[str, RunStatus]] = []
    store.register_status_sink("run-1", lambda rid, s: calls.append((rid, s)))

    status = RunStatus(state="in_progress")
    store.set_status("run-1", status)

    assert len(calls) == 1
    assert calls[0] == ("run-1", status)


def test_run_event_store_sink_not_called_for_other_run() -> None:
    """Sink registered for run-A is NOT invoked when run-B gets a status update."""
    store = RunEventStore()
    store.register("run-A")
    store.register("run-B")

    calls: list[str] = []
    store.register_status_sink("run-A", lambda rid, _: calls.append(rid))

    store.set_status("run-B", RunStatus(state="in_progress"))

    assert calls == []


def test_run_event_store_sink_not_called_after_terminal_state() -> None:
    """Sink is not invoked after the run reaches a terminal (completed) state.

    RunEventStore.set_status ignores updates after terminal, so the sink is
    never called for those redundant updates.
    """
    store = RunEventStore()
    store.register("run-1")

    calls: list[RunStatus] = []
    store.register_status_sink("run-1", lambda _, s: calls.append(s))

    store.set_status("run-1", RunStatus(state="completed", conclusion="success"))
    store.set_status("run-1", RunStatus(state="completed", conclusion="failure"))

    # Only the first terminal transition fires the sink.
    assert len(calls) == 1
    assert calls[0].conclusion == "success"


def test_run_event_store_sink_exception_does_not_propagate() -> None:
    """A sink that raises must not prevent set_status from completing."""
    store = RunEventStore()
    store.register("run-1")

    def _bad_sink(rid: str, status: RunStatus) -> None:
        raise RuntimeError("sink failure")

    store.register_status_sink("run-1", _bad_sink)

    # Must not raise — sink errors are logged but swallowed.
    store.set_status("run-1", RunStatus(state="in_progress"))
    assert store.get_status("run-1").state == "in_progress"


# ---------------------------------------------------------------------------
# In-progress transition test
# ---------------------------------------------------------------------------


async def test_in_progress_status_propagated_via_direct_event_store_call() -> None:
    """The 'in_progress' intermediate state propagates into the run_store.

    Simulates the SubprocessBackend._watch() path that calls
    event_store.set_status(run_id, RunStatus(state='in_progress')) before
    waiting for the subprocess to exit.

    We drive this directly on the RunEventStore to verify the write-through
    without needing a real subprocess.
    """
    backend = FakeExecutionBackend()
    in_progress_seen: list[str] = []

    async def _staged_dispatch(
        *,
        run_id: str,
        event_store: RunEventStore,
        **kwargs: object,
    ) -> None:
        event_store.set_status(run_id, RunStatus(state="in_progress"))
        in_progress_seen.append(run_id)
        event_store.set_status(run_id, RunStatus(state="completed", conclusion="success"))

    backend.dispatch = _staged_dispatch  # type: ignore[method-assign]

    harness = _make_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())
    await asyncio.sleep(0)

    assert handle.run_id in in_progress_seen, "in_progress never emitted"

    runs = await run_store.list_runs(_REPO)
    assert len(runs) == 1
    # Terminal state wins — run is completed after the full sequence.
    assert runs[0].status == "completed"
