"""Tests for the harness registry, failover, and exhaustion routing (SPEC §14).

Coverage targets (see coverage_map.yaml §14):
  §14.3  decide_harness truth table
  §14.4  FailoverHarnessPort failover algorithm
  §14.5  AllHarnessesExhausted → HOLD (never escalate)
  Engine.dispatch: AllHarnessesExhausted → return None (entity stays QUEUED)
  Engine reconcile RC-4: AllHarnessesExhausted → no counter increment
  HarnessConfig / HarnessRegistryEntry helpers
  HarnessRegistry from_json parsing
  Exhaustion vs. task-failure routing distinction
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.domain.types import (
    HARNESS_COOLDOWN_S,
    HARNESSES_JSON_ENV,
    DispatchContext,
    IssueRef,
    PRRef,
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
    HarnessRegistry,
    HarnessRegistryEntry,
    decide_harness,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="api")
_PR_REF = PRRef(repo=_REPO, number=1)
_ISSUE_REF = IssueRef(repo=_REPO, number=10)


def _make_context() -> DispatchContext:
    return DispatchContext(
        issue_ref=_ISSUE_REF,
        contract="agents/orchestrator.md",
        model="claude-sonnet-4-6",
        max_turns=40,
        forge_token_scope="repo-branch",
    )


def _make_port(*, quota_exhausted: bool = False, run_id: str = "run-1") -> AsyncMock:
    """Build a minimal FakeHarnessPort as an AsyncMock."""
    port = AsyncMock()
    if quota_exhausted:
        port.dispatch.side_effect = HarnessQuotaExhausted("test", "rate limited")
    else:
        port.dispatch.return_value = RunHandle(run_id=run_id)
    port.get_run_status.return_value = RunStatus(state="completed", conclusion="success")
    port.cancel.return_value = None
    port.trigger_ci.return_value = None
    port.trigger_workflow.return_value = None
    port.get_run_verdict.return_value = None
    return port


def _make_entry(
    *,
    harness_id: str = "primary",
    priority: int = 1,
    quota_exhausted: bool = False,
    cooled_until: datetime | None = None,
    run_id: str = "run-1",
) -> HarnessRegistryEntry:
    config = HarnessConfig(id=harness_id, priority=priority)
    port = _make_port(quota_exhausted=quota_exhausted, run_id=run_id)
    entry = HarnessRegistryEntry(config=config, port=port)
    entry.cooled_until = cooled_until
    return entry


# ---------------------------------------------------------------------------
# HarnessConfig
# ---------------------------------------------------------------------------


class TestHarnessConfig:
    def test_valid_config(self) -> None:
        cfg = HarnessConfig(id="primary", priority=1)
        assert cfg.id == "primary"
        assert cfg.priority == 1

    def test_empty_id_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            HarnessConfig(id="", priority=1)

    def test_whitespace_id_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            HarnessConfig(id="   ", priority=1)

    def test_negative_priority_raises(self) -> None:
        with pytest.raises(ValueError, match="priority"):
            HarnessConfig(id="x", priority=-1)

    def test_zero_priority_valid(self) -> None:
        cfg = HarnessConfig(id="high", priority=0)
        assert cfg.priority == 0

    def test_frozen(self) -> None:
        cfg = HarnessConfig(id="a", priority=1)
        with pytest.raises((AttributeError, TypeError)):
            cfg.id = "b"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# HarnessRegistryEntry
# ---------------------------------------------------------------------------


class TestHarnessRegistryEntry:
    def test_is_available_no_cooldown(self) -> None:
        entry = _make_entry()
        assert entry.is_available(datetime.now(UTC)) is True

    def test_is_available_expired_cooldown(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=1)
        entry = _make_entry(cooled_until=past)
        assert entry.is_available(datetime.now(UTC)) is True

    def test_is_available_exactly_at_boundary(self) -> None:
        """cooled_until == now → available (strict <=)."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        entry = _make_entry(cooled_until=now)
        assert entry.is_available(now) is True

    def test_is_not_available_future_cooldown(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=60)
        entry = _make_entry(cooled_until=future)
        assert entry.is_available(datetime.now(UTC)) is False

    def test_set_cooldown_arms_future(self) -> None:
        entry = _make_entry()
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        entry.set_cooldown(now)
        assert entry.cooled_until == now + timedelta(seconds=HARNESS_COOLDOWN_S)

    def test_set_cooldown_custom_duration(self) -> None:
        entry = _make_entry()
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        entry.set_cooldown(now, duration_s=60)
        assert entry.cooled_until == now + timedelta(seconds=60)

    def test_reset_cooldown_clears(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=300)
        entry = _make_entry(cooled_until=future)
        entry.reset_cooldown()
        assert entry.cooled_until is None

    def test_id_and_priority_proxy(self) -> None:
        entry = _make_entry(harness_id="fallback", priority=2)
        assert entry.id == "fallback"
        assert entry.priority == 2


# ---------------------------------------------------------------------------
# decide_harness — pure selector (SPEC §14.3)
# ---------------------------------------------------------------------------


class TestDecideHarness:
    def test_single_available_entry_returned(self) -> None:
        entry = _make_entry()
        result = decide_harness([entry], now=datetime.now(UTC))
        assert result is entry

    def test_returns_none_when_list_empty(self) -> None:
        assert decide_harness([], now=datetime.now(UTC)) is None

    def test_skips_cooled_entry_returns_none_when_only_one(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=300)
        entry = _make_entry(cooled_until=future)
        assert decide_harness([entry], now=datetime.now(UTC)) is None

    def test_skips_cooled_picks_next_priority(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        future = now + timedelta(seconds=300)
        primary = _make_entry(harness_id="primary", priority=1, cooled_until=future)
        fallback = _make_entry(harness_id="fallback", priority=2)
        result = decide_harness([primary, fallback], now=now)
        assert result is fallback

    def test_prefers_lower_priority_number(self) -> None:
        now = datetime.now(UTC)
        high = _make_entry(harness_id="high", priority=1)
        low = _make_entry(harness_id="low", priority=2)
        # Pass in reverse order to confirm sorting, not order
        result = decide_harness([low, high], now=now)
        assert result is high

    def test_all_cooled_returns_none(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        future = now + timedelta(seconds=300)
        e1 = _make_entry(harness_id="p", priority=1, cooled_until=future)
        e2 = _make_entry(harness_id="f", priority=2, cooled_until=future)
        assert decide_harness([e1, e2], now=now) is None

    def test_boundary_exactly_at_now_is_available(self) -> None:
        """cooled_until == now → available (SPEC §14.3 strict <= boundary)."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        entry = _make_entry(cooled_until=now)
        result = decide_harness([entry], now=now)
        assert result is entry

    def test_one_second_before_expiry_still_cooling(self) -> None:
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        future = now + timedelta(seconds=1)
        entry = _make_entry(cooled_until=future)
        assert decide_harness([entry], now=now) is None

    def test_pure_does_not_mutate_entries(self) -> None:
        entry = _make_entry()
        before = entry.cooled_until
        decide_harness([entry], now=datetime.now(UTC))
        assert entry.cooled_until == before


# ---------------------------------------------------------------------------
# HarnessRegistry
# ---------------------------------------------------------------------------


class TestHarnessRegistry:
    def test_entries_sorted_by_priority(self) -> None:
        e1 = _make_entry(harness_id="p1", priority=3)
        e2 = _make_entry(harness_id="p2", priority=1)
        e3 = _make_entry(harness_id="p3", priority=2)
        reg = HarnessRegistry([e1, e2, e3])
        assert [e.priority for e in reg.entries()] == [1, 2, 3]

    def test_get_by_id(self) -> None:
        e = _make_entry(harness_id="fallback", priority=2)
        reg = HarnessRegistry([e])
        assert reg.get("fallback") is e
        assert reg.get("missing") is None

    def test_primary_returns_lowest_priority(self) -> None:
        e1 = _make_entry(harness_id="hi", priority=1)
        e2 = _make_entry(harness_id="lo", priority=5)
        reg = HarnessRegistry([e2, e1])
        assert reg.primary() is e1

    def test_primary_empty_registry_returns_none(self) -> None:
        assert HarnessRegistry([]).primary() is None

    def test_from_json_parses_entries(self) -> None:
        json_str = '[{"id": "primary", "priority": 1}, {"id": "backup", "priority": 2}]'
        calls: list[HarnessConfig] = []

        def factory(cfg: HarnessConfig) -> AsyncMock:
            calls.append(cfg)
            return _make_port()

        reg = HarnessRegistry.from_json(json_str, factory)
        assert len(reg.entries()) == 2
        assert reg.entries()[0].id == "primary"
        assert reg.entries()[1].id == "backup"
        assert len(calls) == 2
        assert calls[0].id == "primary"

    def test_from_json_duplicate_id_raises(self) -> None:
        json_str = '[{"id": "dup", "priority": 1}, {"id": "dup", "priority": 2}]'
        with pytest.raises(ValueError, match="Duplicate"):
            HarnessRegistry.from_json(json_str, lambda cfg: _make_port())

    def test_from_json_non_array_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            HarnessRegistry.from_json('{"id": "x"}', lambda cfg: _make_port())

    def test_from_json_non_object_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            HarnessRegistry.from_json('["not-an-object"]', lambda cfg: _make_port())

    def test_from_json_default_priority(self) -> None:
        json_str = '[{"id": "only"}]'
        reg = HarnessRegistry.from_json(json_str, lambda cfg: _make_port())
        assert reg.entries()[0].priority == 1


# ---------------------------------------------------------------------------
# FailoverHarnessPort — dispatch failover algorithm (SPEC §14.4)
# ---------------------------------------------------------------------------


class TestFailoverHarnessPortDispatch:
    @pytest.mark.asyncio
    async def test_single_harness_success(self) -> None:
        entry = _make_entry(run_id="run-ok")
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)
        ctx = _make_context()
        handle = await failover.dispatch(ctx)
        assert handle.run_id == "run-ok"
        entry.port.dispatch.assert_awaited_once_with(ctx)

    @pytest.mark.asyncio
    async def test_failover_to_second_on_quota_exhaustion(self) -> None:
        """Primary raises HarnessQuotaExhausted → failover picks secondary."""
        primary = _make_entry(harness_id="primary", priority=1, quota_exhausted=True)
        fallback = _make_entry(harness_id="fallback", priority=2, run_id="run-fallback")
        reg = HarnessRegistry([primary, fallback])
        failover = FailoverHarnessPort(reg)

        handle = await failover.dispatch(_make_context())
        assert handle.run_id == "run-fallback"

    @pytest.mark.asyncio
    async def test_cooldown_armed_on_quota_exhaustion(self) -> None:
        """After exhaustion, primary's cooled_until is set."""
        primary = _make_entry(harness_id="primary", priority=1, quota_exhausted=True)
        fallback = _make_entry(harness_id="fallback", priority=2)
        reg = HarnessRegistry([primary, fallback])
        failover = FailoverHarnessPort(reg)

        await failover.dispatch(_make_context())
        assert primary.cooled_until is not None
        # cooled_until must be in the future
        assert primary.cooled_until > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_all_cooled_raises_all_harnesses_exhausted(self) -> None:
        """All harnesses quota-exhausted → AllHarnessesExhausted raised."""
        p = _make_entry(harness_id="p", priority=1, quota_exhausted=True)
        f = _make_entry(harness_id="f", priority=2, quota_exhausted=True)
        reg = HarnessRegistry([p, f])
        failover = FailoverHarnessPort(reg)

        with pytest.raises(AllHarnessesExhausted):
            await failover.dispatch(_make_context())

    @pytest.mark.asyncio
    async def test_single_harness_all_cooled_raises(self) -> None:
        """Single harness exhausted → AllHarnessesExhausted (SPEC §14.6 compat)."""
        entry = _make_entry(quota_exhausted=True)
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)

        with pytest.raises(AllHarnessesExhausted):
            await failover.dispatch(_make_context())

    @pytest.mark.asyncio
    async def test_genuine_task_failure_propagates_immediately(self) -> None:
        """Non-quota exception is a genuine failure — propagates without failover."""
        port = AsyncMock()
        port.dispatch.side_effect = RuntimeError("agent crashed")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)

        with pytest.raises(RuntimeError, match="agent crashed"):
            await failover.dispatch(_make_context())
        # Ensure fallback was NOT attempted (there's no fallback — but the error
        # must propagate at the primary, not after attempting others).
        port.dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_genuine_failure_does_not_arm_cooldown(self) -> None:
        """A genuine task failure must NOT arm a cooldown on the harness."""
        port = AsyncMock()
        port.dispatch.side_effect = RuntimeError("network error")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)

        try:
            await failover.dispatch(_make_context())
        except RuntimeError:
            pass

        assert entry.cooled_until is None

    @pytest.mark.asyncio
    async def test_pre_cooled_entry_skipped_immediately(self) -> None:
        """A harness already cooled before dispatch is skipped without calling dispatch."""
        future = datetime.now(UTC) + timedelta(seconds=300)
        primary = _make_entry(harness_id="primary", priority=1, cooled_until=future)
        fallback = _make_entry(harness_id="fallback", priority=2, run_id="run-fb")
        reg = HarnessRegistry([primary, fallback])
        failover = FailoverHarnessPort(reg)

        handle = await failover.dispatch(_make_context())
        assert handle.run_id == "run-fb"
        primary.port.dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_three_harnesses_first_two_exhausted_third_succeeds(self) -> None:
        p1 = _make_entry(harness_id="p1", priority=1, quota_exhausted=True)
        p2 = _make_entry(harness_id="p2", priority=2, quota_exhausted=True)
        p3 = _make_entry(harness_id="p3", priority=3, run_id="run-p3")
        reg = HarnessRegistry([p1, p2, p3])
        failover = FailoverHarnessPort(reg)

        handle = await failover.dispatch(_make_context())
        assert handle.run_id == "run-p3"
        assert p1.cooled_until is not None
        assert p2.cooled_until is not None
        assert p3.cooled_until is None  # succeeded, no cooldown


# ---------------------------------------------------------------------------
# FailoverHarnessPort — delegation methods (SPEC §14.4)
# ---------------------------------------------------------------------------


class TestFailoverHarnessPortDelegation:
    @pytest.mark.asyncio
    async def test_get_run_status_delegates_to_primary(self) -> None:
        primary = _make_entry(harness_id="primary", priority=1)
        fallback = _make_entry(harness_id="fallback", priority=2)
        reg = HarnessRegistry([primary, fallback])
        failover = FailoverHarnessPort(reg)

        handle = RunHandle(run_id="r1")
        await failover.get_run_status(handle)
        primary.port.get_run_status.assert_awaited_once_with(handle)
        fallback.port.get_run_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_delegates_to_primary(self) -> None:
        primary = _make_entry(harness_id="primary", priority=1)
        reg = HarnessRegistry([primary])
        failover = FailoverHarnessPort(reg)

        handle = RunHandle(run_id="r1")
        await failover.cancel(handle)
        primary.port.cancel.assert_awaited_once_with(handle)

    @pytest.mark.asyncio
    async def test_trigger_ci_delegates_to_primary(self) -> None:
        primary = _make_entry()
        reg = HarnessRegistry([primary])
        failover = FailoverHarnessPort(reg)

        await failover.trigger_ci(_PR_REF)
        primary.port.trigger_ci.assert_awaited_once_with(_PR_REF)

    @pytest.mark.asyncio
    async def test_trigger_workflow_delegates_to_primary(self) -> None:
        primary = _make_entry()
        reg = HarnessRegistry([primary])
        failover = FailoverHarnessPort(reg)

        await failover.trigger_workflow("ci.yml", "main", {"k": "v"})
        primary.port.trigger_workflow.assert_awaited_once_with("ci.yml", "main", {"k": "v"})

    @pytest.mark.asyncio
    async def test_get_run_verdict_delegates_to_primary(self) -> None:
        primary = _make_entry()
        reg = HarnessRegistry([primary])
        failover = FailoverHarnessPort(reg)

        handle = RunHandle(run_id="r1")
        await failover.get_run_verdict(handle)
        primary.port.get_run_verdict.assert_awaited_once_with(handle)

    def test_primary_port_empty_registry_raises(self) -> None:
        failover = FailoverHarnessPort(HarnessRegistry([]))
        with pytest.raises(RuntimeError, match="empty"):
            failover._primary_port()


# ---------------------------------------------------------------------------
# AllHarnessesExhausted — HOLD semantics (SPEC §14.5)
# ---------------------------------------------------------------------------


class TestAllHarnessesExhaustedHoldSemantics:
    """Verify the HOLD invariant: exhaustion never escalates, entity state unchanged."""

    def test_is_distinct_from_quota_exhausted(self) -> None:
        """AllHarnessesExhausted must NOT be a subclass of HarnessQuotaExhausted."""
        assert not issubclass(AllHarnessesExhausted, HarnessQuotaExhausted)

    def test_all_harnesses_exhausted_is_exception(self) -> None:
        exc = AllHarnessesExhausted("all cooled")
        assert isinstance(exc, Exception)

    def test_harness_quota_exhausted_carries_id_and_detail(self) -> None:
        exc = HarnessQuotaExhausted("primary", "HTTP 429")
        assert exc.harness_id == "primary"
        assert exc.detail == "HTTP 429"
        assert "primary" in str(exc)

    @pytest.mark.asyncio
    async def test_engine_dispatch_returns_none_on_all_exhausted(self) -> None:
        """Engine.dispatch must return None (not escalate) when all harnesses exhausted."""
        from src.ports.fakes import FakeCounterStore, FakeForgePort, FakeSessionPort

        forge = FakeForgePort()
        session = FakeSessionPort()
        counter = FakeCounterStore()

        # Wire an issue with agent-work label
        forge.seed_issue(
            ref=_ISSUE_REF,
            title="Test",
            body="",
            labels=["agent-work"],
            author="alice",
        )

        # Build a FailoverHarnessPort that always raises AllHarnessesExhausted
        port = AsyncMock()
        port.dispatch.side_effect = AllHarnessesExhausted("all cooled")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)

        from src.engine.dispatch import Engine
        engine = Engine(forge=forge, harness=failover, session=session, counter=counter)

        result = await engine.dispatch("issues", issue_ref=_ISSUE_REF)
        assert result is None, "dispatch must return None (HOLD) when all harnesses exhausted"

    @pytest.mark.asyncio
    async def test_engine_dispatch_held_entity_has_no_needs_human_label(self) -> None:
        """Issue must NOT gain needs-human label when dispatch is HELD."""
        from src.ports.fakes import FakeCounterStore, FakeForgePort, FakeSessionPort
        forge = FakeForgePort()
        session = FakeSessionPort()
        counter = FakeCounterStore()

        forge.seed_issue(
            ref=_ISSUE_REF,
            title="Test",
            body="",
            labels=["agent-work"],
            author="alice",
        )

        port = AsyncMock()
        port.dispatch.side_effect = AllHarnessesExhausted("all cooled")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        failover = FailoverHarnessPort(HarnessRegistry([entry]))

        from src.engine.dispatch import Engine
        engine = Engine(forge=forge, harness=failover, session=session, counter=counter)
        await engine.dispatch("issues", issue_ref=_ISSUE_REF)

        issue = await forge.get_issue(_ISSUE_REF)
        assert "needs-human" not in issue.labels, (
            "HOLD must not add needs-human label — entity stays QUEUED"
        )


# ---------------------------------------------------------------------------
# RC-4 reconciler: AllHarnessesExhausted → no counter increment (SPEC §14.7)
# ---------------------------------------------------------------------------


class TestRC4AllHarnessesExhausted:
    @pytest.mark.asyncio
    async def test_rc4_held_does_not_increment_counter(self) -> None:
        """RC-4: when all harnesses exhausted, orphan counter must NOT be incremented."""
        from src.engine.dispatch import Engine
        from src.ports.fakes import (
            FakeConvergeStateStore,
            FakeCounterStore,
            FakeForgePort,
            FakeSessionPort,
        )

        forge = FakeForgePort()
        session = FakeSessionPort()
        counter = FakeCounterStore()

        forge.seed_issue(
            ref=_ISSUE_REF,
            title="Test",
            body="",
            labels=["agent-work"],
            author="alice",
            closed=False,
        )
        # No open PRs → orphan condition

        port = AsyncMock()
        port.dispatch.side_effect = AllHarnessesExhausted("all cooled")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        failover = FailoverHarnessPort(HarnessRegistry([entry]))

        engine = Engine(
            forge=forge,
            harness=failover,
            session=session,
            counter=counter,
            converge_state=FakeConvergeStateStore(),
        )

        report = await engine.reconcile(_REPO)
        # redispatched must be 0 (held, not actually dispatched)
        assert report.redispatched == 0
        # counter must remain 0
        count = await counter.get_count(_ISSUE_REF, "orphan")
        assert count == 0

    @pytest.mark.asyncio
    async def test_rc4_held_issue_has_no_needs_human(self) -> None:
        """RC-4 HOLD must not add needs-human label to the issue."""
        from src.engine.dispatch import Engine
        from src.ports.fakes import (
            FakeConvergeStateStore,
            FakeCounterStore,
            FakeForgePort,
            FakeSessionPort,
        )

        forge = FakeForgePort()
        session = FakeSessionPort()
        counter = FakeCounterStore()

        forge.seed_issue(
            ref=_ISSUE_REF,
            title="Test",
            body="",
            labels=["agent-work"],
            author="alice",
            closed=False,
        )

        port = AsyncMock()
        port.dispatch.side_effect = AllHarnessesExhausted("all cooled")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        failover = FailoverHarnessPort(HarnessRegistry([entry]))

        engine = Engine(
            forge=forge,
            harness=failover,
            session=session,
            counter=counter,
            converge_state=FakeConvergeStateStore(),
        )

        await engine.reconcile(_REPO)

        issue = await forge.get_issue(_ISSUE_REF)
        assert "needs-human" not in issue.labels, (
            "HOLD must never add needs-human to the issue"
        )


# ---------------------------------------------------------------------------
# Exhaustion vs. genuine task failure routing (SPEC §14.2 exhaustion detection)
# ---------------------------------------------------------------------------


class TestExhaustionVsTaskFailureRouting:
    @pytest.mark.asyncio
    async def test_quota_exhausted_triggers_failover_not_propagation(self) -> None:
        """HarnessQuotaExhausted on primary must not propagate — failover runs instead."""
        primary = _make_entry(harness_id="primary", priority=1, quota_exhausted=True)
        fallback = _make_entry(harness_id="fallback", priority=2, run_id="fb-ok")
        reg = HarnessRegistry([primary, fallback])
        failover = FailoverHarnessPort(reg)

        handle = await failover.dispatch(_make_context())
        assert handle.run_id == "fb-ok"

    @pytest.mark.asyncio
    async def test_runtime_error_propagates_as_task_failure(self) -> None:
        """RuntimeError (non-quota) must propagate immediately — never triggers failover."""
        port = AsyncMock()
        port.dispatch.side_effect = RuntimeError("disk full")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        reg = HarnessRegistry([entry])
        failover = FailoverHarnessPort(reg)

        with pytest.raises(RuntimeError, match="disk full"):
            await failover.dispatch(_make_context())

    @pytest.mark.asyncio
    async def test_value_error_propagates_as_task_failure(self) -> None:
        """ValueError (non-quota) must propagate immediately."""
        port = AsyncMock()
        port.dispatch.side_effect = ValueError("bad context")
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="primary", priority=1),
            port=port,
        )
        failover = FailoverHarnessPort(HarnessRegistry([entry]))

        with pytest.raises(ValueError):
            await failover.dispatch(_make_context())

    @pytest.mark.asyncio
    async def test_quota_on_both_escalates_only_all_harnesses_exhausted(self) -> None:
        """Both harnesses quota-exhausted → AllHarnessesExhausted, never propagates
        a raw HarnessQuotaExhausted to the caller."""
        p = _make_entry(harness_id="p", priority=1, quota_exhausted=True)
        f = _make_entry(harness_id="f", priority=2, quota_exhausted=True)
        reg = HarnessRegistry([p, f])
        failover = FailoverHarnessPort(reg)

        # Must raise AllHarnessesExhausted, not HarnessQuotaExhausted
        with pytest.raises(AllHarnessesExhausted):
            await failover.dispatch(_make_context())


# ---------------------------------------------------------------------------
# HARNESS_COOLDOWN_S constant — single-sourced (SPEC §7, §14)
# ---------------------------------------------------------------------------


class TestHarnessCooldownConstant:
    def test_cooldown_constant_is_named(self) -> None:
        """HARNESS_COOLDOWN_S must be importable from domain.types (SPEC §7)."""
        assert HARNESS_COOLDOWN_S == 300

    def test_harnesses_json_env_constant(self) -> None:
        """HARNESSES_JSON_ENV must match the env var name used in PortProvider."""
        assert HARNESSES_JSON_ENV == "HARNESSES_JSON"

    def test_cooldown_arms_for_exactly_harness_cooldown_s(self) -> None:
        """set_cooldown() without args must use HARNESS_COOLDOWN_S."""
        entry = _make_entry()
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        entry.set_cooldown(now)
        expected = now + timedelta(seconds=HARNESS_COOLDOWN_S)
        assert entry.cooled_until == expected


# ---------------------------------------------------------------------------
# FakeHarnessRegistry
# ---------------------------------------------------------------------------


class TestFakeHarnessRegistry:
    def test_add_entry_and_sorted(self) -> None:
        reg = FakeHarnessRegistry()
        e1 = _make_entry(harness_id="a", priority=2)
        e2 = _make_entry(harness_id="b", priority=1)
        reg.add_entry(e1)
        reg.add_entry(e2)
        assert [e.id for e in reg.entries()] == ["b", "a"]

    def test_empty_on_init(self) -> None:
        reg = FakeHarnessRegistry()
        assert reg.entries() == []

    def test_from_list(self) -> None:
        e = _make_entry(harness_id="x", priority=5)
        reg = FakeHarnessRegistry([e])
        assert reg.get("x") is e


# ---------------------------------------------------------------------------
# Cooldown expiry self-heal — reconciler retries after cooldown
# ---------------------------------------------------------------------------


class TestCooldownExpirySelfHeal:
    @pytest.mark.asyncio
    async def test_dispatch_succeeds_after_cooldown_expires(self) -> None:
        """After cooldown expires, the previously-exhausted harness is eligible again."""
        now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        # Arm a cooldown that expires 1 second in the future relative to now
        entry = _make_entry(harness_id="primary", priority=1)
        entry.set_cooldown(now, duration_s=1)  # expires at now+1s

        # At now (before expiry): not available
        assert entry.is_available(now) is False

        # At now+1s (at exact expiry): available (boundary = available)
        at_expiry = now + timedelta(seconds=1)
        assert entry.is_available(at_expiry) is True

        # At now+2s: definitely available
        after_expiry = now + timedelta(seconds=2)
        assert entry.is_available(after_expiry) is True
