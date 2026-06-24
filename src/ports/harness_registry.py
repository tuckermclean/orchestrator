"""Harness registry and failover coordinator (SPEC §14).

This module provides:
  - HarnessConfig        — per-backend configuration (id, priority; no credentials)
  - HarnessRegistryEntry — config + live port instance + in-memory cooldown state
  - HarnessRegistry      — ordered collection of entries; eligibility queries
  - FakeHarnessRegistry  — in-process fake for tests
  - HarnessQuotaExhausted — raised by a HarnessPort when quota/rate-limit is hit
  - AllHarnessesExhausted — raised by FailoverHarnessPort when all backends cool
  - decide_harness        — pure synchronous selector (SPEC §14.3)
  - FailoverHarnessPort   — HarnessPort-compatible failover coordinator (SPEC §14.4)

Design notes
------------
- Cooldown state is in-memory only. A process restart clears it; exhaustion is
  re-discovered naturally on the next dispatch attempt.
- decide_harness is pure+synchronous: it receives a snapshot of entries and a
  caller-supplied datetime, making it deterministically testable.
- Credentials never enter HarnessConfig (I3). They live exclusively in PortProvider.
- FailoverHarnessPort satisfies the HarnessPort Protocol so the Engine receives it
  transparently wherever a bare HarnessPort was used before.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.domain.types import (
    HARNESS_COOLDOWN_S,
    HARNESSES_JSON_ENV,
    SESSION_LIMIT_COOLDOWN_FLOOR_S,
    RunEvent,
    RunStatus,
)
from src.ports.base import HarnessPort

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local async iterator helpers
# ---------------------------------------------------------------------------


async def _empty_async_iter() -> AsyncIterator[RunEvent]:
    """Async generator that yields nothing — fallback for harnesses without streaming."""
    # The unreachable yield makes this an async generator (not a plain coroutine)
    # so callers get an AsyncIterator, not a coroutine object.
    if False:  # pragma: no cover
        yield  # noqa: RET504


# ---------------------------------------------------------------------------
# Exhaustion signals (SPEC §14.2, §14.5)
# ---------------------------------------------------------------------------


class HarnessQuotaExhausted(Exception):
    """Raised by a HarnessPort.dispatch when quota or rate-limit is exhausted.

    This is the ONLY signal that triggers failover in FailoverHarnessPort.
    Any other exception is a genuine task failure and must NOT trigger failover.

    Harness adapter implementations must map provider-level quota/rate-limit
    errors (e.g. HTTP 429, HTTP 529, provider "overloaded" codes) to this
    exception.  All other errors must propagate unchanged.

    Attributes
    ----------
    harness_id:
        The id of the harness that signalled exhaustion (from HarnessConfig.id).
    detail:
        Human-readable description of the underlying provider error.
    reset_at:
        ISO-8601 UTC timestamp at which the quota is expected to reset,
        parsed from Claude's "resets <time>" output.  None when the reset
        time could not be determined; the cooldown then falls back to the
        fixed HARNESS_COOLDOWN_S.  Set by SubprocessBackend / K8sJobBackend
        when they detect a session-limit in the run output (SPEC §14.8).
    """

    def __init__(
        self,
        harness_id: str,
        detail: str = "",
        reset_at: str | None = None,
    ) -> None:
        self.harness_id = harness_id
        self.detail = detail
        self.reset_at = reset_at
        super().__init__(f"harness {harness_id!r} quota exhausted: {detail}")


class AllHarnessesExhausted(Exception):
    """Raised by FailoverHarnessPort when every harness is currently cooled down.

    CRITICAL: this must NEVER be converted to a needs-human escalation.
    The entity stays in its current forge-label state.  The reconciler re-attempts
    on the next tick; cooldowns expire over time so the system self-heals.
    See SPEC §14.5 for the full invariant.
    """


class SessionLimitHold(AllHarnessesExhausted):
    """Raised by Engine._await_run when a run concludes with ``awaiting_quota``.

    This is a subclass of AllHarnessesExhausted so every existing
    ``except AllHarnessesExhausted`` handler (dispatch.py 110/169/203/225,
    reconcile.py 330) catches it as a HOLD — no label change, entity stays
    in its current forge state.

    The harness cooldown is already armed by the FailoverHarnessPort status sink
    when the run completes (SPEC §14.8).  This exception propagates the HOLD
    deterministically through the await boundary so the converge/dispatch sub-
    machine never mistakes a quota-concluded run for success.

    Attributes
    ----------
    run_id:
        The run_id of the run that hit the session/usage limit.
    quota_reset_at:
        ISO-8601 UTC timestamp from the run status (may be None).  Carried
        for structured logging so operators can see the reset deadline.
    """

    def __init__(self, run_id: str, quota_reset_at: str | None = None) -> None:
        self.run_id = run_id
        self.quota_reset_at = quota_reset_at
        super().__init__(
            f"run {run_id!r} hit session/usage limit (awaiting_quota); "
            f"HOLD until quota resets at {quota_reset_at or 'unknown'}"
        )


# ---------------------------------------------------------------------------
# HarnessConfig (SPEC §14.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessConfig:
    """Immutable per-backend configuration.

    id:
        Unique identifier for this harness instance, e.g. "primary".
        Used for logging and credential namespacing in PortProvider.
    priority:
        Lower number = higher priority.  dispatch() iterates entries in
        ascending order and picks the first available harness.
    """

    id: str
    priority: int

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("HarnessConfig.id must be a non-empty string")
        if self.priority < 0:
            raise ValueError("HarnessConfig.priority must be >= 0")


# ---------------------------------------------------------------------------
# HarnessRegistryEntry (SPEC §14.2)
# ---------------------------------------------------------------------------


@dataclass
class HarnessRegistryEntry:
    """Registry row: config + port instance + in-memory cooldown timestamp.

    cooled_until is set by FailoverHarnessPort when a dispatch raises
    HarnessQuotaExhausted; it is cleared (reset to None) when the cooldown
    expires or when reset() is called directly (tests).
    """

    config: HarnessConfig
    port: HarnessPort
    cooled_until: datetime | None = field(default=None)

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def priority(self) -> int:
        return self.config.priority

    def is_available(self, now: datetime) -> bool:
        """Return True when this harness is not currently on cooldown."""
        return self.cooled_until is None or self.cooled_until <= now

    def set_cooldown(
        self,
        now: datetime,
        duration_s: int = HARNESS_COOLDOWN_S,
        reset_at: str | None = None,
    ) -> None:
        """Mark this harness as cooled-down until a computed deadline.

        When ``reset_at`` is provided (ISO-8601 UTC from Claude's "resets <T>"
        message), the cooldown expires at that time.  A floor of
        ``SESSION_LIMIT_COOLDOWN_FLOOR_S`` seconds is applied in case the
        parsed time is already in the past (clock skew) or imminent.

        When ``reset_at`` is None (no parseable reset time), falls back to the
        fixed ``duration_s`` cooldown from ``now`` (SPEC §14.2 default).
        """
        if reset_at is not None:
            try:
                reset_dt = datetime.fromisoformat(reset_at)
                # Ensure timezone-aware.
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=UTC)
                floor = now + timedelta(seconds=SESSION_LIMIT_COOLDOWN_FLOOR_S)
                self.cooled_until = max(reset_dt, floor)
                return
            except (ValueError, TypeError):
                # Unparseable — fall through to fixed cooldown.
                pass
        self.cooled_until = now + timedelta(seconds=duration_s)

    def reset_cooldown(self) -> None:
        """Clear cooldown unconditionally (test helper / explicit operator recovery)."""
        self.cooled_until = None


# ---------------------------------------------------------------------------
# decide_harness — pure synchronous selector (SPEC §14.3)
# ---------------------------------------------------------------------------


def decide_harness(
    entries: list[HarnessRegistryEntry],
    now: datetime,
) -> HarnessRegistryEntry | None:
    """Select the highest-priority available harness (SPEC §14.3).

    Pure synchronous function — no I/O, no side effects, deterministic.
    Returns the first entry (ascending priority order) that is not on cooldown,
    or None when all entries are currently cooled down.

    The boundary condition ``cooled_until <= now`` means a harness whose
    cooldown expires exactly at ``now`` is immediately eligible (boundary = not
    guarded, symmetric with REARM_RECENT_GUARD_S strict < convention).
    """
    for entry in sorted(entries, key=lambda e: e.priority):
        if entry.is_available(now):
            return entry
    return None


# ---------------------------------------------------------------------------
# HarnessRegistry (SPEC §14.2)
# ---------------------------------------------------------------------------


class HarnessRegistry:
    """Ordered collection of HarnessRegistryEntry values.

    Mirrors RepoRegistry: the entry list is ordered by ascending priority and
    supports lookup by id.  Cooldown mutations are performed on the entry
    objects directly (FailoverHarnessPort holds a reference to the registry
    and calls entry.set_cooldown() on quota exhaustion).

    Thread-safety: single-threaded asyncio; no locks required.
    """

    def __init__(self, entries: list[HarnessRegistryEntry]) -> None:
        # Sort by priority at construction time; preserve original objects.
        self._entries: list[HarnessRegistryEntry] = sorted(
            entries, key=lambda e: e.priority
        )
        self._index: dict[str, HarnessRegistryEntry] = {
            e.id: e for e in self._entries
        }

    def entries(self) -> list[HarnessRegistryEntry]:
        """Return all entries in ascending priority order (snapshot)."""
        return list(self._entries)

    def get(self, harness_id: str) -> HarnessRegistryEntry | None:
        """Look up an entry by id; return None if not present."""
        return self._index.get(harness_id)

    def primary(self) -> HarnessRegistryEntry | None:
        """Return the highest-priority (lowest priority number) entry, or None."""
        return self._entries[0] if self._entries else None

    @classmethod
    def from_json(
        cls,
        raw: str,
        port_factory: Any,
    ) -> HarnessRegistry:
        """Build a registry from a HARNESSES_JSON value.

        ``port_factory(config: HarnessConfig) -> HarnessPort`` is called once
        per entry to construct the actual port instance.  Credentials are read
        inside the factory (PortProvider), never stored in HarnessConfig (I3).

        Expected JSON format::

            [
                {"id": "primary",    "priority": 1},
                {"id": "fallback-1", "priority": 2}
            ]
        """
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError(f"{HARNESSES_JSON_ENV} must be a JSON array")

        entries = []
        seen_ids: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(
                    f"Each {HARNESSES_JSON_ENV} entry must be a JSON object, got {item!r}"
                )
            harness_id = str(item["id"])
            if harness_id in seen_ids:
                raise ValueError(
                    f"Duplicate harness id {harness_id!r} in {HARNESSES_JSON_ENV}"
                )
            seen_ids.add(harness_id)
            priority = int(item.get("priority", 1))
            config = HarnessConfig(id=harness_id, priority=priority)
            port: HarnessPort = port_factory(config)
            entries.append(HarnessRegistryEntry(config=config, port=port))
        return cls(entries)


# ---------------------------------------------------------------------------
# FakeHarnessRegistry — in-process fake for tests
# ---------------------------------------------------------------------------


class FakeHarnessRegistry(HarnessRegistry):
    """In-process fake for tests; supports direct entry manipulation."""

    def __init__(self, entries: list[HarnessRegistryEntry] | None = None) -> None:
        super().__init__(entries or [])

    def add_entry(self, entry: HarnessRegistryEntry) -> None:
        """Append an entry and re-sort by priority."""
        self._entries.append(entry)
        self._entries.sort(key=lambda e: e.priority)
        self._index[entry.id] = entry


# ---------------------------------------------------------------------------
# FailoverHarnessPort (SPEC §14.4)
# ---------------------------------------------------------------------------


class FailoverHarnessPort:
    """HarnessPort-compatible failover coordinator (SPEC §14.4).

    Wraps a HarnessRegistry and implements the dispatch failover algorithm:
    1. Select the highest-priority available harness via decide_harness.
    2. Attempt dispatch.
    3. On HarnessQuotaExhausted: arm cooldown on that harness, try next.
    4. If all harnesses are cooled down: raise AllHarnessesExhausted.

    Dispatch delegation: ``dispatch`` routes to whichever harness ``decide_harness``
    selects (with failover on quota exhaustion).

    Event-read + status-sink delegation (SPEC §14.4 amendment): ``get_run_events``,
    ``subscribe_run_events``, ``register_run_status_sink``, and ``get_live_status``
    must route to the harness that **owns** the given run_id (the one whose
    ``dispatch`` produced it) — not blindly to the primary.  Without this, the
    primary's empty RunEventStore shadows the owning harness's event store and
    makes every transcript invisible (ev0 across all runs).
    Ownership is determined via ``entry.port.has_run(run_id)``; when no owner is
    found (race: run not yet registered, or primary is the only harness) the call
    falls back to primary.

    Maintenance calls that are NOT run-specific (``trigger_workflow``, ``trigger_ci``)
    are delegated to primary unconditionally.

    ``get_run_status``, ``cancel``, ``get_run_verdict`` are also routed via
    ``_owning_port`` so they reach the harness whose RunEventStore holds the run
    state — a correctness fix for multi-harness deployments where a non-primary
    harness dispatched the run.

    I3: No credentials stored here; they live in the HarnessPort instances
    constructed by PortProvider.
    """

    def __init__(self, registry: HarnessRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # Dispatch — with failover (SPEC §14.4)
    # ------------------------------------------------------------------

    async def dispatch(self, context: Any) -> Any:
        """Dispatch with failover; raises AllHarnessesExhausted if all cooled.

        Iterates harnesses in ascending priority order.  On quota exhaustion,
        arms a HARNESS_COOLDOWN_S cooldown on that harness and tries the next.
        Any non-quota exception is a genuine task failure and propagates
        immediately without triggering failover.
        """
        # Build a working list of candidates (snapshot of current entries).
        # We mutate cooled_until on the live entry objects so state is preserved
        # across calls within the same process lifetime.
        candidates = list(self._registry.entries())

        while True:
            now = datetime.now(UTC)
            entry = decide_harness(candidates, now)
            if entry is None:
                raise AllHarnessesExhausted(
                    "All configured harnesses are on cooldown; work is HELD. "
                    "The reconciler will retry when a cooldown expires."
                )

            try:
                handle = await entry.port.dispatch(context)
                _log.debug("dispatch succeeded via harness %r", entry.id)
                # Register a post-completion status sink that arms the cooldown
                # when the run finishes with conclusion="awaiting_quota"
                # (SPEC §14.8 — session-limit wait-and-retry).
                # The sink is synchronous (RunEventStore contract), captures
                # ``entry`` by closure, and is only called once (the RunEventStore
                # guards against double-completion).
                live_entry = entry  # capture for closure

                def _quota_sink(run_id: str, status: RunStatus) -> None:
                    if (
                        status.state == "completed"
                        and status.conclusion == "awaiting_quota"
                    ):
                        live_entry.set_cooldown(
                            now=datetime.now(UTC),
                            reset_at=status.quota_reset_at,
                        )
                        _log.warning(
                            "harness %r: run %s hit session/usage limit; "
                            "cooled until %s (reset_at=%s)",
                            live_entry.id,
                            run_id,
                            live_entry.cooled_until,
                            status.quota_reset_at,
                        )

                port_any: Any = entry.port
                if hasattr(port_any, "register_run_status_sink"):
                    port_any.register_run_status_sink(handle.run_id, _quota_sink)
                    # Catch-up: if the run already completed synchronously
                    # (e.g. FakeExecutionBackend), apply the sink now.
                    if hasattr(port_any, "get_live_status"):
                        live_status: RunStatus = port_any.get_live_status(handle.run_id)
                        if live_status.state == "completed":
                            _quota_sink(handle.run_id, live_status)

                return handle

            except HarnessQuotaExhausted as exc:
                # Arm cooldown on the live registry entry (persists across calls).
                # When exc.reset_at is set (parsed from Claude's "resets <T>"
                # output), the cooldown expires at that specific time rather
                # than the fixed HARNESS_COOLDOWN_S offset (SPEC §14.8).
                entry.set_cooldown(now=datetime.now(UTC), reset_at=exc.reset_at)
                _log.warning(
                    "harness %r quota exhausted (%s); cooled until %s; trying next",
                    entry.id,
                    exc.detail,
                    entry.cooled_until,
                )
                # Remove this entry from the local candidate list so the next
                # decide_harness call does not return the same entry again
                # (it was just cooled; the global state is updated but decide_harness
                # receives the candidates list, which we must also update).
                candidates = [c for c in candidates if c.id != entry.id]

    # ------------------------------------------------------------------
    # Delegated methods — routing helpers
    # ------------------------------------------------------------------

    def _primary_port(self) -> HarnessPort:
        """Return the primary (lowest-priority-number) harness port.

        Raises RuntimeError when the registry is empty (misconfiguration).
        """
        primary = self._registry.primary()
        if primary is None:
            raise RuntimeError("HarnessRegistry is empty — no harness configured")
        return primary.port

    def _owning_port(self, run_id: str) -> HarnessPort:
        """Return the harness port that owns *run_id*, falling back to primary.

        Iterates all registry entries and calls ``port.has_run(run_id)`` on each.
        The first match is the owning harness (the one whose ``dispatch`` created
        the run and whose RunEventStore holds its events and status).

        Falls back to the primary harness when:
        - No entry exposes ``has_run`` (e.g. a minimal fake used in tests).
        - No entry claims ownership (race: run_id not yet registered, or the
          primary is the only/owning harness and both paths lead to the same port).

        This routing is critical for multi-harness deployments: when ``dispatch``
        selects a non-primary harness, the run's events live in THAT harness's
        RunEventStore.  Routing reads to primary would silently return an empty
        transcript — exactly the ev0/tx0 bug this fix addresses.
        """
        for entry in self._registry.entries():
            port: Any = entry.port
            if hasattr(port, "has_run"):
                # has_run must be synchronous (ClaudeCodeHarnessPort contract).
                # Guard against test fakes that accidentally return an awaitable.
                result = port.has_run(run_id)
                if not hasattr(result, "__await__") and result:
                    return entry.port
        # No owner found — fall back to primary (covers single-harness config
        # and the not-yet-registered race).
        return self._primary_port()

    # ------------------------------------------------------------------
    # Delegated methods — non-run-specific; always go to primary
    # ------------------------------------------------------------------

    async def trigger_workflow(
        self,
        name: str,
        ref: str,
        inputs: dict[str, object],
    ) -> None:
        await self._primary_port().trigger_workflow(name, ref, inputs)

    async def trigger_ci(self, pr_ref: Any) -> None:
        await self._primary_port().trigger_ci(pr_ref)

    # ------------------------------------------------------------------
    # Delegated methods — run-specific; route to owning harness (SPEC §14.4)
    # ------------------------------------------------------------------

    async def get_run_status(self, handle: Any) -> Any:
        return await self._owning_port(handle.run_id).get_run_status(handle)

    async def cancel(self, handle: Any) -> None:
        await self._owning_port(handle.run_id).cancel(handle)

    async def get_run_verdict(self, handle: Any) -> Any:
        return await self._owning_port(handle.run_id).get_run_verdict(handle)

    # ------------------------------------------------------------------
    # Event-read surface — route to owning harness (root-cause fix)
    # ------------------------------------------------------------------

    def get_run_events(self, run_id: str) -> list[RunEvent]:
        """Return the transcript event backlog from the run-owning harness.

        Routes to the harness whose RunEventStore holds this run's events.
        Falls back to primary when ownership cannot be determined (e.g. the
        run is not yet registered, or primary is the only harness).

        Without this method, RunRecordingHarness.get_run_events() falls through
        to its ``hasattr`` guard's else-branch and returns [] — making every
        run's transcript invisible (ev0 across all runs).
        """
        port: Any = self._owning_port(run_id)
        if hasattr(port, "get_run_events"):
            result: list[RunEvent] = port.get_run_events(run_id)
            return result
        return []

    def subscribe_run_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Subscribe to the run-owning harness's event stream (backfill + live).

        Routes to the harness whose RunEventStore holds this run's events.
        Falls back to primary when ownership cannot be determined.

        Without this method, RunRecordingHarness.subscribe_run_events() falls
        through to its ``hasattr`` guard's else-branch and returns an empty
        async iterator — making the SSE stream always empty.
        """
        port: Any = self._owning_port(run_id)
        if hasattr(port, "subscribe_run_events"):
            it: AsyncIterator[RunEvent] = port.subscribe_run_events(run_id)
            return it
        return _empty_async_iter()

    def register_run_status_sink(self, run_id: str, sink: Any) -> None:
        """Register a write-through status sink on the run-owning harness.

        Routes to the harness that owns the run so the sink receives status
        updates from the correct RunEventStore.  Falls back to primary when
        ownership cannot be determined.
        """
        port: Any = self._owning_port(run_id)
        if hasattr(port, "register_run_status_sink"):
            port.register_run_status_sink(run_id, sink)

    def get_live_status(self, run_id: str) -> RunStatus:
        """Return the current live status from the run-owning harness (issue #101).

        Routes to the harness whose RunEventStore holds this run's status.
        Falls back to primary when ownership cannot be determined.
        """
        port: Any = self._owning_port(run_id)
        if hasattr(port, "get_live_status"):
            status: RunStatus = port.get_live_status(run_id)
            return status
        return RunStatus(state="queued")
