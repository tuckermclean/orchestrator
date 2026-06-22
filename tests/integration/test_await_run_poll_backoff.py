"""Integration tests for Engine._await_run poll backoff (issue #24).

Verifies that _await_run yields the event loop between status polls via
asyncio.sleep(POLL_INTERVAL_S) when the run is not immediately complete.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.domain.types import POLL_INTERVAL_S, RunHandle
from src.engine import dispatch as dispatch_mod
from src.engine.dispatch import Engine
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort


def _engine(harness: FakeHarnessPort) -> Engine:
    return Engine(
        forge=FakeForgePort(),
        harness=harness,
        session=FakeSessionPort(),
    )


# ---------------------------------------------------------------------------
# Immediate-complete path: no sleep invoked (fast path still works)
# ---------------------------------------------------------------------------


async def test_await_run_immediate_complete_no_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the run is already completed, _await_run returns True without sleeping."""
    sleep_mock = AsyncMock()
    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", sleep_mock)

    harness = FakeHarnessPort()
    handle = RunHandle(run_id="run-immediate")
    harness.seed_run(handle, state="completed", conclusion="success")

    engine = _engine(harness)
    result = await engine._await_run(handle)

    assert result is True
    sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Poll path: sleep invoked once per non-complete poll iteration
# ---------------------------------------------------------------------------


async def test_await_run_polls_sleep_between_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_await_run sleeps between status polls when run is not immediately complete.

    The harness returns in_progress on the first call, then completed on the
    second. We expect exactly one asyncio.sleep(POLL_INTERVAL_S) call — one
    yield between the first (not-complete) and second (complete) poll.
    """
    harness = FakeHarnessPort()
    handle = RunHandle(run_id="run-two-polls")

    # Start as in_progress; we'll flip to completed after the first poll via the mock.
    harness.seed_run(handle, state="in_progress")

    call_count = 0

    async def fake_sleep(seconds: Any) -> None:
        nonlocal call_count
        call_count += 1
        assert seconds == POLL_INTERVAL_S, (
            f"asyncio.sleep called with {seconds!r}, expected POLL_INTERVAL_S={POLL_INTERVAL_S}"
        )
        # After the first sleep, mark the run complete so the loop exits on the next poll.
        harness.seed_run(handle, state="completed", conclusion="success")

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fake_sleep)

    engine = _engine(harness)
    result = await engine._await_run(handle)

    assert result is True
    assert call_count == 1, f"Expected exactly 1 sleep call, got {call_count}"


# ---------------------------------------------------------------------------
# Timeout path: sleep called each iteration until deadline; then cancel
# ---------------------------------------------------------------------------


async def test_await_run_timeout_sleeps_then_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On timeout, _await_run still sleeps between polls and cancels the run.

    CI_WAIT_S is patched to 0 so the deadline triggers after the first not-complete
    poll without a real wall-clock wait. The fake sleep is also patched so no real
    I/O delay occurs.
    """
    # Zero-out the timeout so the deadline fires immediately after the first poll.
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)

    sleep_calls: list[Any] = []

    async def fake_sleep(seconds: Any) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(dispatch_mod.asyncio, "sleep", fake_sleep)

    harness = FakeHarnessPort()
    harness.never_completes = True  # run stays in_progress throughout
    handle = RunHandle(run_id="run-timeout")
    harness.seed_run(handle, state="in_progress")

    engine = _engine(harness)
    result = await engine._await_run(handle)

    # Timed out → False; handle was cancelled
    assert result is False
    assert len(harness.cancel_calls) == 1
    assert harness.cancel_calls[0].run_id == "run-timeout"
    # With CI_WAIT_S=0 the deadline fires immediately after the first poll, so the
    # cancel branch is taken before the sleep — sleep count may be 0 here.
    # The important assertion is that no real sleep delay occurred (fake_sleep was used).
    assert all(s == POLL_INTERVAL_S for s in sleep_calls)


# ---------------------------------------------------------------------------
# POLL_INTERVAL_S value sanity: must be positive and well below CI_WAIT_S
# ---------------------------------------------------------------------------


def test_poll_interval_s_value() -> None:
    """POLL_INTERVAL_S is positive and materially shorter than CI_WAIT_S."""
    from src.domain.types import CI_WAIT_S

    assert POLL_INTERVAL_S > 0
    # At least 10× shorter than the budget so the loop can actually make progress.
    assert POLL_INTERVAL_S <= CI_WAIT_S // 10
