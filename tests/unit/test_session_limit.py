"""Tests for session/usage-limit wait-and-retry behavior (SPEC §14.8).

Coverage:
  (a) A run result carrying a session-limit signature → classified as
      awaiting_quota (not failed), reset time parsed correctly.
  (b) The entity stays CONVERGING/BUILDING and the harness is cooled-down
      until the reset time.
  (c) The reconciler skips re-arm until the reset time, then re-arms
      (via the normal decide_rearm_action + AllHarnessesExhausted machinery).
  (d) Status serializes through the API (RunSummary.quota_reset_at).
  (e) is_session_limit and parse_reset_time pure-function contracts.
  (f) set_cooldown with reset_at uses the parsed time (not fixed cooldown).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.types import (
    HARNESS_COOLDOWN_S,
    SESSION_LIMIT_COOLDOWN_FLOOR_S,
    DispatchContext,
    IssueRef,
    RepoRef,
    RunHandle,
    RunStatus,
)
from src.ports.harness_registry import (
    AllHarnessesExhausted,
    FailoverHarnessPort,
    FakeHarnessRegistry,
    HarnessConfig,
    HarnessQuotaExhausted,
    HarnessRegistryEntry,
)
from src.ports.session_limit import is_session_limit, parse_reset_time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="repo")
_ISSUE_REF = IssueRef(repo=_REPO, number=42)


def _make_context() -> DispatchContext:
    return DispatchContext(
        issue_ref=_ISSUE_REF,
        contract="agents/orchestrator.md",
        model="claude-sonnet-4-6",
        max_turns=40,
        forge_token_scope="repo-branch",
    )


def _make_port(
    *,
    run_id: str = "run-xyz",
    quota_exhausted: bool = False,
) -> AsyncMock:
    port = AsyncMock()
    if quota_exhausted:
        port.dispatch.side_effect = HarnessQuotaExhausted("primary", "rate limited")
    else:
        port.dispatch.return_value = RunHandle(run_id=run_id)
    port.get_run_status.return_value = RunStatus(state="completed", conclusion="success")
    port.cancel.return_value = None
    port.trigger_ci.return_value = None
    port.trigger_workflow.return_value = None
    port.get_run_verdict.return_value = None
    port.has_run = MagicMock(return_value=False)
    return port


def _make_entry(
    harness_id: str = "primary",
    priority: int = 1,
    *,
    quota_exhausted: bool = False,
    run_id: str = "run-xyz",
) -> HarnessRegistryEntry:
    config = HarnessConfig(id=harness_id, priority=priority)
    port = _make_port(quota_exhausted=quota_exhausted, run_id=run_id)
    return HarnessRegistryEntry(config=config, port=port)


# ---------------------------------------------------------------------------
# (e) is_session_limit — pure-function tests
# ---------------------------------------------------------------------------


class TestIsSessionLimit:
    def test_hits_session_limit_message(self) -> None:
        text = "You've hit your session limit · resets 4:30 PM PDT"
        assert is_session_limit(text) is True

    def test_usage_limit_variant(self) -> None:
        assert is_session_limit("You've hit your usage limit · resets 4:30 PM") is True

    def test_long_form_have(self) -> None:
        assert is_session_limit("You have hit your session limit") is True

    def test_case_insensitive(self) -> None:
        assert is_session_limit("YOU'VE HIT YOUR SESSION LIMIT") is True

    def test_http_429(self) -> None:
        assert is_session_limit("HTTP 429 Too Many Requests from Anthropic API") is True

    def test_quota_exhausted(self) -> None:
        assert is_session_limit("Error: quota exhausted — please wait") is True

    def test_rate_limit_exceeded(self) -> None:
        assert is_session_limit("rate limit exceeded") is True

    def test_rate_limit_hit(self) -> None:
        assert is_session_limit("rate limit hit") is True

    def test_normal_failure(self) -> None:
        assert is_session_limit("Process exited with code 1") is False

    def test_empty(self) -> None:
        assert is_session_limit("") is False

    def test_unrelated_text(self) -> None:
        assert is_session_limit("The implementation looks good.") is False


# ---------------------------------------------------------------------------
# (e) parse_reset_time — pure-function tests
# ---------------------------------------------------------------------------


class TestParseResetTime:
    def test_absolute_time_with_tz(self) -> None:
        # The parsed time should be an ISO-8601 string.
        text = "You've hit your session limit · resets 4:30 PM PDT"
        result = parse_reset_time(text)
        assert result is not None
        # Validate it's a parseable ISO-8601 string.
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_utc_time(self) -> None:
        text = "resets 16:30 UTC"
        result = parse_reset_time(text)
        assert result is not None
        dt = datetime.fromisoformat(result)
        # 16:30 UTC — converted from UTC stays 16:30.
        assert dt.minute == 30

    def test_relative_minutes(self) -> None:
        # "in 5 minutes" should give a time ~5 minutes from now.
        text = "resets in 5 minutes"
        before = datetime.now(UTC)
        result = parse_reset_time(text)
        after = datetime.now(UTC)
        assert result is not None
        dt = datetime.fromisoformat(result)
        low = before + timedelta(minutes=4, seconds=59)
        high = after + timedelta(minutes=5, seconds=1)
        assert low <= dt <= high

    def test_relative_hours(self) -> None:
        text = "resets in 2 hours"
        before = datetime.now(UTC)
        result = parse_reset_time(text)
        after = datetime.now(UTC)
        assert result is not None
        dt = datetime.fromisoformat(result)
        low = before + timedelta(hours=1, minutes=59)
        high = after + timedelta(hours=2, seconds=1)
        assert low <= dt <= high

    def test_no_reset_in_text(self) -> None:
        assert parse_reset_time("You've hit your session limit") is None

    def test_parse_reset_time_empty(self) -> None:
        assert parse_reset_time("") is None

    def test_malformed_time(self) -> None:
        # "resets" with garbage after it — should return None or a valid time.
        result = parse_reset_time("resets xyz blah")
        # Either None or a valid datetime — we just check it doesn't crash.
        if result is not None:
            datetime.fromisoformat(result)  # must be parseable


# ---------------------------------------------------------------------------
# (f) HarnessRegistryEntry.set_cooldown with reset_at
# ---------------------------------------------------------------------------


class TestSetCooldownWithResetAt:
    def test_reset_at_future_uses_that_time(self) -> None:
        entry = _make_entry()
        now = datetime.now(UTC)
        future = (now + timedelta(minutes=30)).isoformat()
        entry.set_cooldown(now, reset_at=future)
        assert entry.cooled_until is not None
        # Should be roughly 30 min from now (within 1 s).
        expected = now + timedelta(minutes=30)
        assert abs((entry.cooled_until - expected).total_seconds()) < 2

    def test_reset_at_past_applies_floor(self) -> None:
        entry = _make_entry()
        now = datetime.now(UTC)
        past = (now - timedelta(minutes=5)).isoformat()
        entry.set_cooldown(now, reset_at=past)
        assert entry.cooled_until is not None
        # Floor: at least SESSION_LIMIT_COOLDOWN_FLOOR_S seconds from now.
        floor = now + timedelta(seconds=SESSION_LIMIT_COOLDOWN_FLOOR_S)
        assert entry.cooled_until >= floor - timedelta(seconds=1)

    def test_reset_at_none_uses_default_cooldown(self) -> None:
        entry = _make_entry()
        now = datetime.now(UTC)
        entry.set_cooldown(now, reset_at=None)
        expected = now + timedelta(seconds=HARNESS_COOLDOWN_S)
        assert entry.cooled_until is not None
        assert abs((entry.cooled_until - expected).total_seconds()) < 2

    def test_reset_at_invalid_string_falls_back(self) -> None:
        entry = _make_entry()
        now = datetime.now(UTC)
        entry.set_cooldown(now, reset_at="not-a-datetime")
        # Falls back to fixed cooldown.
        expected = now + timedelta(seconds=HARNESS_COOLDOWN_S)
        assert entry.cooled_until is not None
        assert abs((entry.cooled_until - expected).total_seconds()) < 2

    def test_reset_at_naive_datetime_treated_as_utc(self) -> None:
        entry = _make_entry()
        now = datetime.now(UTC)
        future_naive = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        entry.set_cooldown(now, reset_at=future_naive)
        # Naive is treated as UTC — should be ~1 hour from now.
        assert entry.cooled_until is not None
        expected = now + timedelta(hours=1)
        assert abs((entry.cooled_until - expected).total_seconds()) < 5


# ---------------------------------------------------------------------------
# (b) FailoverHarnessPort registers a post-completion sink that arms cooldown
#     when awaiting_quota is detected.
# ---------------------------------------------------------------------------


class TestFailoverCooldownOnAwaitingQuota:
    """Verify FailoverHarnessPort arms the harness cooldown when a dispatched run
    completes with conclusion=awaiting_quota (SPEC §14.8).
    """

    @pytest.mark.asyncio
    async def test_awaiting_quota_conclusion_arms_cooldown(self) -> None:
        """Run completes with awaiting_quota → harness entry is put on cooldown."""
        run_id = "run-awaiting"
        reset_iso = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()

        # Build a port that exposes register_run_status_sink and get_live_status.
        port = AsyncMock()
        port.dispatch.return_value = RunHandle(run_id=run_id)
        port.has_run = MagicMock(return_value=False)
        port.get_run_status.return_value = RunStatus(
            state="completed",
            conclusion="awaiting_quota",
            quota_reset_at=reset_iso,
        )
        port.get_run_verdict.return_value = None

        # register_run_status_sink: capture the registered sink so we can invoke it.
        registered_sink: list[object] = []

        def _capture_sink(rid: str, sink: object) -> None:
            registered_sink.append(sink)

        port.register_run_status_sink = MagicMock(side_effect=_capture_sink)
        # get_live_status returns queued initially (sink not yet needed for catch-up).
        port.get_live_status = MagicMock(
            return_value=RunStatus(state="queued")
        )

        config = HarnessConfig(id="primary", priority=1)
        entry = HarnessRegistryEntry(config=config, port=port)
        registry = FakeHarnessRegistry([entry])
        failover = FailoverHarnessPort(registry)

        context = _make_context()
        handle = await failover.dispatch(context)
        assert handle.run_id == run_id

        # The sink must have been registered.
        assert len(registered_sink) == 1
        assert entry.cooled_until is None  # not yet cooled

        # Simulate the run completing with awaiting_quota.
        sink = registered_sink[0]
        assert callable(sink)
        sink(run_id, RunStatus(
            state="completed",
            conclusion="awaiting_quota",
            quota_reset_at=reset_iso,
        ))

        # The entry should now be on cooldown until ~reset_iso.
        assert entry.cooled_until is not None
        expected = datetime.fromisoformat(reset_iso)
        assert abs((entry.cooled_until - expected).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_success_conclusion_does_not_arm_cooldown(self) -> None:
        """Normal success run completion should not put the harness on cooldown."""
        run_id = "run-success"

        port = AsyncMock()
        port.dispatch.return_value = RunHandle(run_id=run_id)
        port.has_run = MagicMock(return_value=False)
        port.get_run_status.return_value = RunStatus(state="completed", conclusion="success")

        registered_sink: list[object] = []

        def _capture_sink(rid: str, sink: object) -> None:
            registered_sink.append(sink)

        port.register_run_status_sink = MagicMock(side_effect=_capture_sink)
        port.get_live_status = MagicMock(return_value=RunStatus(state="queued"))

        config = HarnessConfig(id="primary", priority=1)
        entry = HarnessRegistryEntry(config=config, port=port)
        registry = FakeHarnessRegistry([entry])
        failover = FailoverHarnessPort(registry)

        await failover.dispatch(_make_context())
        assert len(registered_sink) == 1

        sink = registered_sink[0]
        sink(run_id, RunStatus(state="completed", conclusion="success"))

        # No cooldown should be armed.
        assert entry.cooled_until is None

    @pytest.mark.asyncio
    async def test_awaiting_quota_without_reset_time_uses_fixed_cooldown(self) -> None:
        """awaiting_quota with no reset_at falls back to HARNESS_COOLDOWN_S."""
        run_id = "run-no-reset"

        port = AsyncMock()
        port.dispatch.return_value = RunHandle(run_id=run_id)
        port.has_run = MagicMock(return_value=False)
        port.get_run_status.return_value = RunStatus(
            state="completed", conclusion="awaiting_quota"
        )

        registered_sink: list[object] = []
        port.register_run_status_sink = MagicMock(
            side_effect=lambda rid, sink: registered_sink.append(sink)
        )
        port.get_live_status = MagicMock(return_value=RunStatus(state="queued"))

        config = HarnessConfig(id="primary", priority=1)
        entry = HarnessRegistryEntry(config=config, port=port)
        registry = FakeHarnessRegistry([entry])
        failover = FailoverHarnessPort(registry)

        before = datetime.now(UTC)
        await failover.dispatch(_make_context())
        assert len(registered_sink) == 1

        sink = registered_sink[0]
        sink(run_id, RunStatus(state="completed", conclusion="awaiting_quota"))
        after = datetime.now(UTC)

        assert entry.cooled_until is not None
        expected_floor = before + timedelta(seconds=HARNESS_COOLDOWN_S)
        expected_ceil = after + timedelta(seconds=HARNESS_COOLDOWN_S + 1)
        assert expected_floor <= entry.cooled_until <= expected_ceil


# ---------------------------------------------------------------------------
# (c) Reconciler skips re-arm while harness is cooled (AllHarnessesExhausted
#     during cooldown), then re-arms after cooldown expires.
# ---------------------------------------------------------------------------


class TestReconcilesAfterCooldown:
    """Verify that AllHarnessesExhausted is raised during the cooldown window
    and that the harness becomes available again after it expires.
    """

    @pytest.mark.asyncio
    async def test_all_harnesses_exhausted_during_cooldown(self) -> None:
        now = datetime.now(UTC)
        future = now + timedelta(minutes=15)
        entry = _make_entry(run_id="r1")
        entry.cooled_until = future  # simulate cooldown armed by awaiting_quota
        registry = FakeHarnessRegistry([entry])
        failover = FailoverHarnessPort(registry)

        with pytest.raises(AllHarnessesExhausted):
            await failover.dispatch(_make_context())

    @pytest.mark.asyncio
    async def test_quota_cooldown_expired_allows_redispatch(self) -> None:
        now = datetime.now(UTC)
        past = now - timedelta(seconds=1)
        entry = _make_entry(run_id="r2")
        entry.cooled_until = past  # cooldown has already expired
        registry = FakeHarnessRegistry([entry])
        failover = FailoverHarnessPort(registry)

        # Should succeed (no AllHarnessesExhausted raised).
        port = entry.port
        port.register_run_status_sink = MagicMock()
        port.get_live_status = MagicMock(return_value=RunStatus(state="queued"))
        handle = await failover.dispatch(_make_context())
        assert handle is not None


# ---------------------------------------------------------------------------
# (d) RunStatus / RunSummary serialises quota_reset_at through the API
# ---------------------------------------------------------------------------


class TestRunStatusSerialization:
    def test_run_status_awaiting_quota_round_trip(self) -> None:
        reset = "2026-06-24T21:30:00+00:00"
        rs = RunStatus(
            state="completed",
            conclusion="awaiting_quota",
            quota_reset_at=reset,
        )
        serialized = rs.model_dump()
        assert serialized["state"] == "completed"
        assert serialized["conclusion"] == "awaiting_quota"
        assert serialized["quota_reset_at"] == reset

    def test_run_status_failure_no_quota_reset(self) -> None:
        rs = RunStatus(state="completed", conclusion="failure")
        assert rs.quota_reset_at is None

    def test_run_status_success_no_quota_reset(self) -> None:
        rs = RunStatus(state="completed", conclusion="success")
        serialized = rs.model_dump()
        assert serialized["quota_reset_at"] is None

    def test_run_summary_has_quota_reset_at(self) -> None:
        from src.domain.types import RunSummary

        reset = "2026-06-24T21:30:00+00:00"
        summary = RunSummary(
            run_id="r1",
            repo=_REPO,
            type="converge-reviewer",
            status="awaiting_quota",
            started_at=datetime.now(UTC),
            quota_reset_at=reset,
        )
        data = summary.model_dump()
        assert data["quota_reset_at"] == reset

    def test_run_summary_quota_reset_at_none_by_default(self) -> None:
        from src.domain.types import RunSummary

        summary = RunSummary(
            run_id="r2",
            repo=_REPO,
            type="implementer",
            status="completed",
            started_at=datetime.now(UTC),
        )
        assert summary.quota_reset_at is None


# ---------------------------------------------------------------------------
# (a) FakeExecutionBackend: awaiting_quota detection in the watcher path
# ---------------------------------------------------------------------------


class TestExecutionBackendSessionLimitDetection:
    """Test that SubprocessBackend._watch sets awaiting_quota when session-limit
    text appears in the event stream.

    Uses a FakeProcessRunner that exits non-zero and includes session-limit text
    in the JSONL output stream.
    """

    @pytest.mark.asyncio
    async def test_subprocess_sets_awaiting_quota_on_session_limit(self) -> None:
        """When claude exits non-zero with a session-limit message, the run status
        must be awaiting_quota (not failure)."""
        import json

        from src.ports.execution_backend import SubprocessBackend
        from src.ports.harness import RunEventStore

        # Build a fake process whose stdout contains a session-limit message
        # and exits with rc=1.
        session_limit_msg = "You've hit your session limit · resets in 5 minutes"
        jsonl_line = json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "result": session_limit_msg,
        }) + "\n"

        class FakeStream:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._pos = 0

            def __aiter__(self) -> FakeStream:
                return self

            async def __anext__(self) -> bytes:
                if self._pos >= len(self._data):
                    raise StopAsyncIteration
                # Yield line-by-line.
                end = self._data.find(b"\n", self._pos)
                if end == -1:
                    chunk = self._data[self._pos:]
                    self._pos = len(self._data)
                else:
                    chunk = self._data[self._pos:end + 1]
                    self._pos = end + 1
                return chunk

        from src.ports.harness import ProcessResult

        class FakeProcess:
            pid = 9999
            returncode: int | None = None
            stdout: FakeStream

            def __init__(self) -> None:
                self.stdout = FakeStream(jsonl_line.encode())

            async def wait(self) -> int:
                self.returncode = 1
                return 1

            async def terminate(self) -> None:
                pass

            async def kill(self) -> None:
                pass

        fake_proc = FakeProcess()
        fake_result = ProcessResult.__new__(ProcessResult)  # type: ignore[call-arg]
        # Manually set _process to avoid real asyncio.subprocess.Process.
        # We use duck-typing since ProcessResult wraps _process.
        fake_result._process = fake_proc  # type: ignore[attr-defined]

        async def _fake_runner(
            args: list[str],
            cwd: str,
            env: dict[str, str],
        ) -> ProcessResult:
            return fake_result

        event_store = RunEventStore()
        event_store.register("test-run-1")

        # Patch harness helpers used by SubprocessBackend.dispatch.
        harness_mock = MagicMock()
        harness_mock._clone_repo = AsyncMock()
        harness_mock._materialize_contract = MagicMock()
        harness_mock._configure_git_identity = AsyncMock()
        harness_mock._write_spawn_hook = MagicMock()

        backend = SubprocessBackend(process_runner=_fake_runner)

        # We call _watch directly (dispatch spawns a task; here we want synchronous
        # control for the test assertion).
        await backend._watch(
            run_id="test-run-1",
            process=fake_result,
            work_dir="/tmp/fake",
            event_store=event_store,
            repo_dir="/tmp/fake/repo",
            branch=None,
            forge_token_scope="repo-branch",
            child_env={},
        )

        status = event_store.get_status("test-run-1")
        assert status.state == "completed"
        assert status.conclusion == "awaiting_quota", (
            f"Expected awaiting_quota but got {status.conclusion!r}"
        )
        # Reset time should be approximately 5 minutes from now.
        assert status.quota_reset_at is not None
        reset_dt = datetime.fromisoformat(status.quota_reset_at)
        assert reset_dt > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_subprocess_failure_without_session_limit_stays_failure(self) -> None:
        """A normal non-zero exit without session-limit text stays 'failure'."""
        import json

        from src.ports.execution_backend import SubprocessBackend
        from src.ports.harness import ProcessResult, RunEventStore

        error_jsonl = json.dumps({
            "type": "result",
            "subtype": "error_max_turns",
            "result": "Ran out of turns.",
        }) + "\n"

        class FakeStream:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._pos = 0

            def __aiter__(self) -> FakeStream:
                return self

            async def __anext__(self) -> bytes:
                if self._pos >= len(self._data):
                    raise StopAsyncIteration
                end = self._data.find(b"\n", self._pos)
                if end == -1:
                    chunk = self._data[self._pos:]
                    self._pos = len(self._data)
                else:
                    chunk = self._data[self._pos:end + 1]
                    self._pos = end + 1
                return chunk

        class FakeProcess:
            pid = 9998
            returncode: int | None = None

            def __init__(self) -> None:
                self.stdout = FakeStream(error_jsonl.encode())

            async def wait(self) -> int:
                self.returncode = 1
                return 1

            async def terminate(self) -> None:
                pass

        fake_proc = FakeProcess()
        fake_result = ProcessResult.__new__(ProcessResult)  # type: ignore[call-arg]
        fake_result._process = fake_proc  # type: ignore[attr-defined]

        event_store = RunEventStore()
        event_store.register("test-run-2")

        backend = SubprocessBackend()

        await backend._watch(
            run_id="test-run-2",
            process=fake_result,
            work_dir="/tmp/fake2",
            event_store=event_store,
            repo_dir="/tmp/fake2/repo",
        )

        status = event_store.get_status("test-run-2")
        assert status.state == "completed"
        assert status.conclusion == "failure"
        assert status.quota_reset_at is None
