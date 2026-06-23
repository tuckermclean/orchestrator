"""Integration tests for run event streaming — RunEventStore backfill + live.

Root cause: stream_run() and get_run() read from the wrong store.
  - stream_run() called self.session.stream_events(run_id) — a SessionPort the
    harness backend never writes to, so the stream was always empty.
  - get_run() returned run_store.get_run() whose events list is always [] because
    the harness only writes into its own RunEventStore.

Fix: both now read from the harness RunEventStore (the authoritative transcript source):
  - stream_run() calls self.harness.subscribe_run_events(run_id) which yields the
    full backlog then live events (backfill + live, via RunEventStore.subscribe_events).
  - get_run() merges harness.get_run_events(run_id) into the returned RunDetail.

Tests in this file:
  1. RunEventStore.subscribe_events — backfill before subscriber attaches.
  2. RunEventStore.subscribe_events — live delivery after subscriber attaches.
  3. RunEventStore.subscribe_events — already-completed run: backlog only, no hang.
  4. stream_run() reads from the harness RunEventStore (not the session port).
  5. stream_run() — late subscriber still gets the full backlog.
  6. get_run() includes transcript events from the harness RunEventStore.
  7. Session-port stream_events is no longer used by stream_run (regression guard).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from src.db.run_store import FakeRunStore
from src.domain.types import (
    DispatchContext,
    IssueRef,
    RepoRef,
    RunEvent,
    RunStatus,
)
from src.ports.execution_backend import FakeExecutionBackend
from src.ports.fakes import FakeHarnessPort, FakeSessionPort
from src.ports.harness import ClaudeCodeHarnessPort, RunEventStore
from src.service.orchestrator import OrchestratorService, RunRecordingHarness

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="streaming-svc")
_ISSUE_REF = IssueRef(repo=_REPO, number=1)

_CLAUDE_TOKEN = "sk-ant-fake"
_APP_ID = "app-123"
_PRIVATE_KEY_PEM = "---fake-pem---"
_INSTALLATION_ID = "inst-456"


def _make_event(event_type: str = "text", text: str = "hello") -> RunEvent:
    return RunEvent(
        event_type=event_type,
        data={"text": text},
        timestamp=datetime.now(tz=UTC),
    )


def _make_context() -> DispatchContext:
    return DispatchContext(
        issue_ref=_ISSUE_REF,
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=10,
        forge_token_scope="repo-branch",
        allowed_agent_refs=None,
    )


def _make_real_harness(backend: FakeExecutionBackend) -> ClaudeCodeHarnessPort:
    return ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner="acme",
        repo_name="streaming-svc",
        execution_backend=backend,
    )


def _patch_mint(gh_token: str = "ghs_fake") -> patch:  # type: ignore[type-arg]
    return patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value=gh_token),
    )


def _make_service(
    harness: FakeHarnessPort,
    session: FakeSessionPort,
) -> OrchestratorService:
    from src.db.audit import AuditLog

    return OrchestratorService(
        forge=None,  # type: ignore[arg-type]
        harness=harness,
        session=session,
        audit=AuditLog(),
        allowlist=[],
        owner="acme",
        run_store=FakeRunStore(),
    )


# ---------------------------------------------------------------------------
# 1. RunEventStore.subscribe_events — backfill before subscriber attaches
# ---------------------------------------------------------------------------


async def test_subscribe_events_yields_backlog_before_subscriber_attaches() -> None:
    """A subscriber that registers AFTER events are appended still gets the full backlog.

    This is the core contract: backfill makes late subscribers whole.
    """
    store = RunEventStore()
    store.register("run-1")

    # Append events BEFORE subscribing.
    e1 = _make_event("text", "line-1")
    e2 = _make_event("text", "line-2")
    store.append("run-1", e1)
    store.append("run-1", e2)

    # Signal completion so the subscriber iterator exits.
    store.set_status("run-1", RunStatus(state="completed", conclusion="success"))

    # Subscribe after the fact — should still yield both events.
    collected: list[RunEvent] = []
    async for event in store.subscribe_events("run-1"):
        collected.append(event)

    assert len(collected) == 2
    assert collected[0].data["text"] == "line-1"
    assert collected[1].data["text"] == "line-2"


# ---------------------------------------------------------------------------
# 2. RunEventStore.subscribe_events — live delivery after subscriber attaches
# ---------------------------------------------------------------------------


async def test_subscribe_events_yields_live_events_after_subscription() -> None:
    """A subscriber receives events appended AFTER it registered."""
    store = RunEventStore()
    store.register("run-2")

    collected: list[RunEvent] = []

    async def _consume() -> None:
        async for event in store.subscribe_events("run-2"):
            collected.append(event)

    task = asyncio.create_task(_consume())

    # Yield control so the subscriber can register its queue.
    await asyncio.sleep(0)

    # Append events live.
    e1 = _make_event("text", "live-1")
    e2 = _make_event("text", "live-2")
    store.append("run-2", e1)
    store.append("run-2", e2)
    store.set_status("run-2", RunStatus(state="completed", conclusion="success"))

    await task

    assert len(collected) == 2
    assert collected[0].data["text"] == "live-1"
    assert collected[1].data["text"] == "live-2"


# ---------------------------------------------------------------------------
# 3. RunEventStore.subscribe_events — already-completed: backlog only, no hang
# ---------------------------------------------------------------------------


async def test_subscribe_events_already_completed_exits_without_hanging() -> None:
    """Subscribing to an already-completed run yields the backlog and exits cleanly.

    The subscriber must not block waiting for a sentinel that was put before it
    registered — it creates a fresh queue, replays the backlog, then exits.
    """
    store = RunEventStore()
    store.register("run-3")

    e1 = _make_event("text", "done-1")
    store.append("run-3", e1)
    store.set_status("run-3", RunStatus(state="completed", conclusion="success"))

    collected: list[RunEvent] = []
    async for event in store.subscribe_events("run-3"):
        collected.append(event)

    assert len(collected) == 1
    assert collected[0].data["text"] == "done-1"


# ---------------------------------------------------------------------------
# 4. stream_run() reads from the harness RunEventStore, not the session port
# ---------------------------------------------------------------------------


async def test_stream_run_reads_from_harness_event_store() -> None:
    """OrchestratorService.stream_run yields events from the harness RunEventStore.

    Previously stream_run() called self.session.stream_events() which reads from
    a separate store the harness backend never writes to — always empty.
    After the fix, it reads from self.harness.subscribe_run_events() which reads
    from the RunEventStore that the backend actually writes into.
    """
    backend = FakeExecutionBackend()
    real_harness = _make_real_harness(backend)
    session = FakeSessionPort()
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=real_harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())

    # Manually append events to the harness RunEventStore.
    event_store = real_harness._event_store
    e1 = _make_event("text", "harness-event-1")
    e2 = _make_event("text", "harness-event-2")
    event_store.append(handle.run_id, e1)
    event_store.append(handle.run_id, e2)
    # Signal completion so the iterator terminates.
    event_store.set_status(handle.run_id, RunStatus(state="completed", conclusion="success"))

    # Collect via stream_run() — should see harness events, not session events.
    collected: list[RunEvent] = []
    async for event in recording.subscribe_run_events(handle.run_id):
        collected.append(event)

    assert len(collected) == 2
    assert collected[0].data["text"] == "harness-event-1"
    assert collected[1].data["text"] == "harness-event-2"

    # Confirm session port stream_events was NOT called.
    assert session.stream_events_calls == []


# ---------------------------------------------------------------------------
# 5. stream_run() — late subscriber gets the full backlog (end-to-end)
# ---------------------------------------------------------------------------


async def test_stream_run_late_subscriber_gets_full_backlog() -> None:
    """A stream_run() call opened after events were appended yields all events.

    This is the end-to-end version of the backfill test, exercising the
    OrchestratorService.stream_run → RunRecordingHarness.subscribe_run_events →
    ClaudeCodeHarnessPort.subscribe_run_events → RunEventStore.subscribe_events
    chain.
    """
    backend = FakeExecutionBackend()
    real_harness = _make_real_harness(backend)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=real_harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())

    event_store = real_harness._event_store

    # Append backlog BEFORE subscriber opens.
    for i in range(3):
        event_store.append(handle.run_id, _make_event("text", f"backlog-{i}"))

    # Complete the run before subscribing.
    event_store.set_status(handle.run_id, RunStatus(state="completed", conclusion="success"))

    collected: list[RunEvent] = []
    async for event in recording.subscribe_run_events(handle.run_id):
        collected.append(event)

    assert len(collected) == 3
    for i, event in enumerate(collected):
        assert event.data["text"] == f"backlog-{i}", (
            f"Expected backlog-{i}, got {event.data['text']!r}"
        )


# ---------------------------------------------------------------------------
# 6. get_run() includes transcript events from the harness RunEventStore
# ---------------------------------------------------------------------------


async def test_get_run_includes_harness_transcript_events() -> None:
    """OrchestratorService.get_run() merges harness RunEventStore events into RunDetail.

    Previously get_run() returned run_store.get_run() whose events list is always
    empty (the run_store only tracks metadata; the harness never writes events there).
    After the fix, harness.get_run_events() is merged so the returned RunDetail
    contains the transcript.
    """
    backend = FakeExecutionBackend()
    real_harness = _make_real_harness(backend)
    session = FakeSessionPort()
    run_store = FakeRunStore()

    # Build the OrchestratorService wired to the real harness.
    from src.db.audit import AuditLog
    from src.ports.fakes import FakeForgePort

    forge = FakeForgePort()
    service = OrchestratorService(
        forge=forge,
        harness=real_harness,
        session=session,
        audit=AuditLog(),
        allowlist=[],
        owner="acme",
        run_store=run_store,
    )

    with _patch_mint():
        handle = await service.harness.dispatch(_make_context())

    # Append transcript events to the harness RunEventStore.
    event_store = real_harness._event_store
    e1 = _make_event("text", "transcript-line-1")
    e2 = _make_event("tool_use", "ran a tool")
    event_store.append(handle.run_id, e1)
    event_store.append(handle.run_id, e2)

    detail = await service.get_run(handle.run_id)

    assert len(detail.events) == 2
    assert detail.events[0].data["text"] == "transcript-line-1"
    assert detail.events[1].event_type == "tool_use"


# ---------------------------------------------------------------------------
# 7. Regression guard: session port stream_events not called by stream_run
# ---------------------------------------------------------------------------


async def test_stream_run_does_not_call_session_port() -> None:
    """stream_run() must not delegate to session.stream_events() any more.

    After the fix, stream_run() sources events from the harness RunEventStore.
    Calling the session port was the original bug (the session port's queue was
    never populated by the real backends).  This test is a regression guard.
    """
    backend = FakeExecutionBackend()
    real_harness = _make_real_harness(backend)
    session = FakeSessionPort()
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=real_harness, run_store=run_store)

    with _patch_mint():
        handle = await recording.dispatch(_make_context())

    # Mark the run as completed so the stream terminates.
    real_harness._event_store.set_status(
        handle.run_id,
        RunStatus(state="completed", conclusion="success"),
    )

    # Drain the stream.
    async for _ in recording.subscribe_run_events(handle.run_id):
        pass

    # The session port must NOT have been called.
    assert session.stream_events_calls == [], (
        "stream_run() must not delegate to session.stream_events(); "
        f"got calls: {session.stream_events_calls}"
    )


# ---------------------------------------------------------------------------
# 8. RunRecordingHarness.get_run_events returns [] for FakeHarnessPort (fallback)
# ---------------------------------------------------------------------------


def test_run_recording_harness_get_run_events_fallback_for_fake_harness() -> None:
    """RunRecordingHarness.get_run_events returns [] when the underlying harness
    doesn't expose get_run_events (e.g. FakeHarnessPort in unit tests).

    The fallback ensures the service degrades gracefully instead of raising.
    """
    session = FakeSessionPort()
    fake_harness = FakeHarnessPort(session=session)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=fake_harness, run_store=run_store)

    result = recording.get_run_events("any-run-id")

    assert result == []


# ---------------------------------------------------------------------------
# 9. RunRecordingHarness.subscribe_run_events returns empty iter for FakeHarnessPort
# ---------------------------------------------------------------------------


async def test_run_recording_harness_subscribe_fallback_is_empty() -> None:
    """RunRecordingHarness.subscribe_run_events returns an empty iterator when the
    underlying harness doesn't expose subscribe_run_events.

    The fallback ensures stream_run() returns immediately (no hang) for harnesses
    that don't support event streaming.
    """
    session = FakeSessionPort()
    fake_harness = FakeHarnessPort(session=session)
    run_store = FakeRunStore()
    recording = RunRecordingHarness(harness=fake_harness, run_store=run_store)

    collected: list[RunEvent] = []
    async for event in recording.subscribe_run_events("any-run-id"):
        collected.append(event)

    assert collected == []
