"""OrchestratorService — top-level service wiring."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

from src.db.audit import AuditLog
from src.db.run_store import FakeRunStore, SQLiteRunStore
from src.decisions.pipeline_health import pipeline_health
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_TRIAGE,
    RECONCILER_CRON,
    DispatchContext,
    HealthReport,
    IssueRef,
    PRRef,
    PRState,
    RepoRef,
    RunDetail,
    RunEvent,
    RunHandle,
    RunStatus,
    RunSummary,
    TriageItem,
    Verdict,
)
from src.engine.dispatch import Engine
from src.engine.intake import IntakeEngine
from src.engine.reconcile import ReconcileReport
from src.ports.advisory_lock import AsyncioLockProvider
from src.ports.base import (
    ConvergeStateStore,
    CounterStore,
    ForgePort,
    HarnessPort,
    LockProvider,
    SessionPort,
)
from src.service.registry import RepoRegistryPort

_log = logging.getLogger(__name__)

# Default LRU dedup window (number of delivery IDs to remember)
_DEFAULT_DEDUP_WINDOW = 1000


# ---------------------------------------------------------------------------
# RunRecordingHarness — HarnessPort wrapper that records dispatched runs
# ---------------------------------------------------------------------------


class RunRecordingHarness:
    """Wraps any HarnessPort and records each dispatched run into a RunStore.

    The run_store (FakeRunStore or SQLiteRunStore) becomes the single source of
    truth for run metadata that ``list_runs`` / ``get_run`` read.  The underlying
    harness's RunEventStore remains the authority for live events / status.

    Repo is extracted from DispatchContext.issue_ref or DispatchContext.pr_ref.
    When neither is set the dispatch is recorded under a placeholder repo so the
    run still appears in a full listing (it will not match repo-scoped queries).
    Type is derived from the contract path basename (e.g. "triager.md" → "triager").
    """

    def __init__(
        self,
        harness: HarnessPort,
        run_store: FakeRunStore | SQLiteRunStore,
    ) -> None:
        self._harness = harness
        self._run_store = run_store

    async def dispatch(self, context: DispatchContext) -> RunHandle:
        handle = await self._harness.dispatch(context)
        # Determine repo from context references (issue_ref takes priority).
        if context.issue_ref is not None:
            repo = context.issue_ref.repo
        elif context.pr_ref is not None:
            repo = context.pr_ref.repo
        else:
            repo = RepoRef(owner="unknown", name="unknown")
        # Derive a human-readable type label from the contract path basename.
        contract_base = context.contract.rsplit("/", 1)[-1].removesuffix(".md")
        self._run_store.record(
            run_id=handle.run_id,
            repo=repo,
            type=contract_base,
            model=context.model,
            started_at=datetime.now(tz=UTC),
        )

        # Write-through status propagation (issue #101).
        # Register a sync sink on the harness RunEventStore so every subsequent
        # set_status() call (queued → in_progress → completed/failure) is
        # immediately written into the run_store.  The sink maps RunStatus to
        # the run_store's flat (status_str, completed_at) interface.
        # Only wired when the harness exposes register_run_status_sink — the
        # FakeHarnessPort used in most tests does not, so those tests are
        # unaffected unless they specifically exercise the status path.
        if hasattr(self._harness, "register_run_status_sink"):
            run_id = handle.run_id
            run_store = self._run_store

            def _status_sink(rid: str, status: RunStatus) -> None:
                # Map RunStatus → run_store.set_status flat interface.
                # Terminal states use conclusion-derived strings so the UI can
                # distinguish completed-success from completed-failure without a
                # separate conclusion field in RunSummary.
                #   success   → "completed"
                #   failure   → "failed"
                #   cancelled → "cancelled"
                # Non-terminal: propagate state string directly.
                _CONCLUSION_TO_STATUS: dict[str, str] = {
                    "success": "completed",
                    "failure": "failed",
                    "cancelled": "cancelled",
                }
                store_status: str
                if status.state == "completed":
                    conclusion = status.conclusion or "failure"
                    store_status = _CONCLUSION_TO_STATUS.get(conclusion, conclusion)
                    completed_at: datetime | None = datetime.now(tz=UTC)
                else:
                    store_status = status.state
                    completed_at = None
                try:
                    run_store.set_status(rid, store_status, completed_at)
                except Exception:
                    _log.exception(
                        "RunRecordingHarness status sink failed for run_id=%s status=%s",
                        rid,
                        status,
                    )

            self._harness.register_run_status_sink(run_id, _status_sink)

            # Catch-up: some backends (FakeExecutionBackend, or a synchronous
            # watcher that completes during dispatch()) may have already called
            # set_status before we registered the sink above.  Read the current
            # live status from the event_store and apply it now so the run_store
            # is consistent even when the backend finished synchronously.
            live_status = self._harness.get_live_status(run_id)  # type: ignore[attr-defined]
            if live_status.state != "queued":
                _status_sink(run_id, live_status)

        return handle

    # Delegate transcript-access methods when the underlying harness exposes them.
    # These are not part of the HarnessPort Protocol (which covers dispatch/status
    # only) but are used by OrchestratorService.stream_run / get_run to read the
    # live RunEventStore transcript.  Delegation is conditional so RunRecordingHarness
    # continues to work with FakeHarnessPort (which lacks these methods).

    def get_run_events(self, run_id: str) -> list[RunEvent]:
        """Return the transcript event backlog from the underlying harness."""
        # Cast to Any to access the extra method that is present on
        # ClaudeCodeHarnessPort but not on the minimal HarnessPort Protocol.
        harness: Any = self._harness
        if hasattr(harness, "get_run_events"):
            result: list[RunEvent] = harness.get_run_events(run_id)
            return result
        return []

    def subscribe_run_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Subscribe to backfill + live events from the underlying harness."""
        # Cast to Any to access the extra method that is present on
        # ClaudeCodeHarnessPort but not on the minimal HarnessPort Protocol.
        harness: Any = self._harness
        if hasattr(harness, "subscribe_run_events"):
            it: AsyncIterator[RunEvent] = harness.subscribe_run_events(run_id)
            return it
        # Fallback: return an empty async iterator for harnesses that don't
        # expose event streaming (e.g. FakeHarnessPort in unit tests).
        return _empty_async_iter()

    # Delegate all other HarnessPort methods to the wrapped harness.
    async def trigger_workflow(self, name: str, ref: str, inputs: dict[str, object]) -> None:
        await self._harness.trigger_workflow(name, ref, inputs)

    async def trigger_ci(self, pr_ref: PRRef) -> None:
        await self._harness.trigger_ci(pr_ref)

    async def get_run_status(self, handle: RunHandle) -> RunStatus:
        return await self._harness.get_run_status(handle)

    async def cancel(self, handle: RunHandle) -> None:
        await self._harness.cancel(handle)

    async def get_run_verdict(self, handle: RunHandle) -> Verdict | None:
        return await self._harness.get_run_verdict(handle)


def _extract_repo(payload: dict[str, object]) -> RepoRef | None:
    repo_data = payload.get("repository", {})
    if not isinstance(repo_data, dict):
        return None
    owner_data = repo_data.get("owner")
    owner = (
        str(owner_data.get("login", "")) if isinstance(owner_data, dict) else str(owner_data or "")
    )
    return RepoRef(owner=owner, name=str(repo_data.get("name", "")))


class OrchestratorService:
    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
        audit: AuditLog | None = None,
        allowlist: list[str] | None = None,
        owner: str = "",
        counter: CounterStore | None = None,
        converge_state: ConvergeStateStore | None = None,
        lock_provider: LockProvider | None = None,
        dedup_window: int = _DEFAULT_DEDUP_WINDOW,
        registry: RepoRegistryPort | None = None,
        run_store: FakeRunStore | SQLiteRunStore | None = None,
        triager_reconcile_delay_s: float = 60.0,
    ) -> None:
        # Seconds to wait after intake before attempting triager divergence reconciliation.
        # The triager agent runs asynchronously; this delay gives it time to post its comment.
        # Default: 60 s (well within the triager's typical completion time).
        self._triager_reconcile_delay_s = triager_reconcile_delay_s
        # Run store — single source of truth for dispatched run metadata.
        # FakeRunStore is the default (in-memory, no persistence); callers may
        # inject SQLiteRunStore for pod-lifetime durability.
        self._run_store: FakeRunStore | SQLiteRunStore = (
            run_store if run_store is not None else FakeRunStore()
        )

        # Wrap the harness with the recording shim so every dispatch — whether
        # triggered via Engine, IntakeEngine, promote(), or dev_dispatch() — is
        # recorded in the run store with the correct repo context.
        recording_harness = RunRecordingHarness(harness, self._run_store)

        self.engine = Engine(
            forge=forge,
            harness=recording_harness,
            session=session,
            counter=counter,
            converge_state=converge_state,
        )
        self.forge = forge
        # self.harness is RunRecordingHarness (a HarnessPort structural subtype) so
        # OrchestratorService.stream_run / get_run can call the extra transcript-access
        # methods (get_run_events / subscribe_run_events) without a cast.
        self.harness: RunRecordingHarness = recording_harness
        self.session = session
        self._counter = counter
        self._converge_state = converge_state

        # Per-entity advisory lock (SPEC §11.3 step 1).
        # Default: single-process asyncio.Lock per entity key.
        # Swap for a Postgres pg_advisory_xact_lock provider in multi-replica deployments.
        self._lock_provider: LockProvider = (
            lock_provider if lock_provider is not None else AsyncioLockProvider()
        )

        # Audit log — default to in-memory if none provided
        self._audit = audit if audit is not None else AuditLog()
        self._allowlist = allowlist if allowlist is not None else []
        self._owner = owner

        # Repo registry — multi-repo support (issue #49).
        # When None, the service operates in single-repo mode using _allowlist/_owner.
        self._registry = registry

        self._intake_engine = IntakeEngine(
            forge=forge,
            harness=recording_harness,
            session=session,
            audit=self._audit,
            allowlist=self._allowlist,
            owner=self._owner,
        )

        # Delivery-ID LRU dedup cache (SPEC §11.3) — bounded by dedup_window entries
        self._dedup_cache: OrderedDict[str, bool] = OrderedDict()
        self._dedup_window = dedup_window

        # Reconciler cron task handle
        self._reconcile_task: asyncio.Task[None] | None = None

        # In-flight converge tasks, keyed by PR. The converge sub-machine runs
        # for minutes (dispatches review agents, polls CI across rounds); it must
        # NOT run inline in the webhook request or GitHub's ~10s delivery timeout
        # fires and redelivers, double-dispatching work. We spawn it as a
        # background task and return the webhook immediately. The dict both keeps
        # a strong ref (an unreferenced task can be GC'd mid-flight) and dedupes
        # concurrent converges for the same PR.
        self._converge_tasks: dict[str, asyncio.Task[PRState]] = {}

        # In-flight triager-divergence reconciliation tasks, keyed by issue.
        # Reconciliation runs after a delay (to give the triager time to post its
        # comment) and is idempotent — spawned at most once per intake run.
        self._triager_reconcile_tasks: dict[str, asyncio.Task[bool]] = {}

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def startup(self) -> None:
        """Initialise async resources (call once from the ASGI lifespan handler)."""
        await self._audit.init()

    async def start_reconciler(self, repo: RepoRef | None = None) -> None:
        """Start the reconciler cron loop.

        When ``repo`` is supplied, the loop reconciles that one repo every tick.
        When ``repo`` is None (multi-repo mode), each tick calls ``reconcile_now()``
        which iterates all enabled repos from the registry (or the dev default).

        Runs every ``RECONCILER_CRON`` cadence (15 min) in the background.
        Call ``stop_reconciler()`` to cancel the task cleanly.
        """

        async def _loop() -> None:
            while True:
                await asyncio.sleep(_cron_to_seconds(RECONCILER_CRON))
                try:
                    if repo is not None:
                        await self.engine.reconcile(repo)
                    else:
                        await self.reconcile_now()
                except Exception:
                    pass  # isolated: reconciler errors never crash the loop

        self._reconcile_task = asyncio.create_task(_loop())

    async def stop_reconciler(self) -> None:
        """Cancel the reconciler cron loop and drain in-flight tasks."""
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None

        # Drain in-flight background converge tasks. Converge round state is
        # persisted to ConvergeStateStore, so a cancelled converge resumes at the
        # correct round on the next trigger / reconcile re-arm (RC-3).
        for task in list(self._converge_tasks.values()):
            task.cancel()
        for task in list(self._converge_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._converge_tasks.clear()

        # Drain in-flight triager-divergence reconciliation tasks.
        for rtask in list(self._triager_reconcile_tasks.values()):
            rtask.cancel()
        for rtask in list(self._triager_reconcile_tasks.values()):
            try:
                await rtask
            except (asyncio.CancelledError, Exception):
                pass
        self._triager_reconcile_tasks.clear()

    # -----------------------------------------------------------------------
    # Event routing
    # -----------------------------------------------------------------------

    async def handle_event(
        self,
        event_name: str,
        payload: dict[str, object],
        delivery_id: str | None = None,
    ) -> dict[str, object]:
        """Route forge webhook events to the engine.

        Returns ``{"handled": True}`` on normal dispatch or
        ``{"handled": False, "reason": "duplicate_delivery_id"}`` when the delivery
        has already been processed (SPEC §11.3 delivery-ID dedup).
        """
        # Step 1 — delivery-ID dedup (SPEC §11.3)
        if delivery_id is not None:
            if delivery_id in self._dedup_cache:
                return {"handled": False, "reason": "duplicate_delivery_id"}
            # Record before processing; evict oldest entry if window is full
            self._dedup_cache[delivery_id] = True
            if len(self._dedup_cache) > self._dedup_window:
                self._dedup_cache.popitem(last=False)

        issue_ref: IssueRef | None = None
        pr_ref: PRRef | None = None
        comment_body: str | None = None

        # Extract refs from payload
        if "issue" in payload:
            issue_data = payload["issue"]
            if isinstance(issue_data, dict):
                repo = _extract_repo(payload)
                if repo is not None:
                    issue_ref = IssueRef(
                        repo=repo,
                        number=int(issue_data.get("number", 0)),
                    )

        if "pull_request" in payload:
            pr_data = payload["pull_request"]
            if isinstance(pr_data, dict):
                repo = _extract_repo(payload)
                if repo is not None:
                    pr_ref = PRRef(
                        repo=repo,
                        number=int(pr_data.get("number", 0)),
                    )

        if "comment" in payload:
            comment_data = payload["comment"]
            if isinstance(comment_data, dict):
                comment_body = str(comment_data.get("body", ""))

        # Per-repo config lookup: when the registry is set, look up the config
        # for the event's repo and use its allowlist/owner.  If the repo is not
        # registered or not enabled, ignore the event (no-op, not an error).
        if self._registry is not None:
            event_repo = _extract_repo(payload)
            if event_repo is not None:
                repo_config = await self._registry.get_repo(event_repo)
                if repo_config is None or not repo_config.enabled:
                    # Unknown or disabled repo — silently ignore.
                    return {"handled": False, "reason": "repo_not_registered"}
                # Rebuild the intake engine with per-repo allowlist/owner for this event.
                intake_engine = IntakeEngine(
                    forge=self.forge,
                    harness=self.harness,
                    session=self.session,
                    audit=self._audit,
                    allowlist=repo_config.allowlist,
                    owner=repo_config.repo.owner,
                )
            else:
                intake_engine = self._intake_engine
        else:
            intake_engine = self._intake_engine

        if event_name == "issues" and issue_ref is not None:
            # SPEC §11.1 routing table — issues actions:
            #   opened / reopened  → Engine.intake  (only if intake_enabled)
            #   labeled (LABEL_AGENT_WORK only) → Engine.dispatch
            #   anything else      → no-op (fall through)
            action = str(payload.get("action", ""))
            if action in ("opened", "reopened"):
                # Resolve intake_enabled from the registry when wired; default True
                # so single-repo / no-registry behaviour is preserved unchanged.
                intake_enabled: bool = True
                if self._registry is not None:
                    event_repo = _extract_repo(payload)
                    if event_repo is not None:
                        _cfg = await self._registry.get_repo(event_repo)
                        if _cfg is not None:
                            intake_enabled = _cfg.intake_enabled
                if intake_enabled:
                    result = await intake_engine.intake(issue_ref)
                    if result.handle is not None and result.decision is not None:
                        # Spawn background reconciliation: compare intake decision with
                        # the triager's recommendation once the triager has posted its
                        # comment.  The triager runs async (fire-and-forget); reconciliation
                        # is deferred by triager_reconcile_delay_s.  SPEC §10.4 step 6.
                        self._spawn_triager_reconcile(
                            intake_engine, issue_ref, result.decision
                        )
            elif action == "labeled":
                label_data = payload.get("label", {})
                label_name = (
                    str(label_data.get("name", ""))
                    if isinstance(label_data, dict)
                    else ""
                )
                if label_name == LABEL_AGENT_WORK:
                    await self.engine.dispatch(
                        "issues",
                        issue_ref=issue_ref,
                        pr_ref=pr_ref,
                        comment_body=comment_body,
                    )
            # All other issues actions (labeled:other, closed, edited, assigned, …) → no-op
        elif event_name == "pull_request" and pr_ref is not None:
            # SPEC §11.1 event routing table — pull_request actions:
            #   ready_for_review  → Engine.converge (P2)
            #   labeled (converge label only) → Engine.converge (P2/P7)
            #   synchronize       → Engine.converge (P7; idempotency gate returns
            #                       immediately for draft PRs, so this is always safe)
            #   anything else     → no-op (fall through without dispatch)
            #   Converge runs in the BACKGROUND (see _spawn_converge): it is a
            #   minutes-long sub-machine and must not block the webhook response.
            action = str(payload.get("action", ""))
            if action == "ready_for_review" or action == "synchronize":
                self._spawn_converge(pr_ref)
            elif action == "labeled":
                label_data = payload.get("label", {})
                label_name = (
                    str(label_data.get("name", ""))
                    if isinstance(label_data, dict)
                    else ""
                )
                if label_name == LABEL_CONVERGE:
                    self._spawn_converge(pr_ref)
        else:
            await self.engine.dispatch(
                event_name,
                issue_ref=issue_ref,
                pr_ref=pr_ref,
                comment_body=comment_body,
            )

        return {"handled": True}

    # -----------------------------------------------------------------------
    # Reconciler
    # -----------------------------------------------------------------------

    async def reconcile_now(self, repo: RepoRef | None = None) -> list[ReconcileReport]:
        """Run reconcile immediately — iterates all enabled repos from the registry.

        When ``repo`` is explicitly supplied the reconciler is scoped to that
        one repo (useful for targeted operator invocations or tests).  When
        ``repo`` is None and a registry is set, ALL enabled repos are reconciled
        concurrently.  Without a registry, falls back to the single-repo default
        (``demo/repo`` in dev mode).

        Idempotent: calling twice produces the same effect as calling once since
        the reconciler re-reads live forge label state and skips already-acted entities.
        """
        if repo is not None:
            report = await self.engine.reconcile(repo)
            return [report]

        if self._registry is not None:
            enabled = await self._registry.enabled_repos()
            if not enabled:
                return []
            reports = await asyncio.gather(
                *[self.engine.reconcile(cfg.repo) for cfg in enabled]
            )
            return list(reports)

        # No registry — fall back to the single-repo default (dev / legacy mode).
        target = RepoRef(owner="demo", name="repo")
        report = await self.engine.reconcile(target)
        return [report]

    # -----------------------------------------------------------------------
    # Converge
    # -----------------------------------------------------------------------

    async def converge_pr(self, pr_ref: PRRef) -> PRState:
        """Run the converge sub-machine for *pr_ref*.

        The CI gate trusts the repo's actual check runs — every present check
        must be completed and green (SPEC §7 CI green definition).  No named
        allow-list; no per-repo override.  Pending checks are awaited (up to
        ``CI_WAIT_S``) before the approve/escalate decision is made.
        """
        return await self.engine.converge(pr_ref)

    def _spawn_triager_reconcile(
        self,
        intake_engine: IntakeEngine,
        issue_ref: IssueRef,
        intake_decision: str,
    ) -> bool:
        """Spawn a background task to detect/surface intake–triager recommendation divergence.

        Waits ``triager_reconcile_delay_s`` seconds before calling
        ``intake_engine.reconcile_triager_divergence`` — giving the triager agent
        time to complete and post its structured comment.

        De-duplicated per issue: if a reconciliation for this issue is already
        in flight, returns False (idempotent on re-delivery).

        Returns True if a new task was spawned, False if one was already in flight.
        SPEC §10.4 step 6.
        """
        key = f"{issue_ref.repo.owner}/{issue_ref.repo.name}#{issue_ref.number}"
        existing = self._triager_reconcile_tasks.get(key)
        if existing is not None and not existing.done():
            return False

        delay = self._triager_reconcile_delay_s

        async def _reconcile() -> bool:
            import asyncio as _asyncio

            await _asyncio.sleep(delay)
            try:
                return await intake_engine.reconcile_triager_divergence(
                    issue_ref, intake_decision
                )
            except Exception:
                _log.exception(
                    "Triager divergence reconciliation failed for %s", key
                )
                return False

        task: asyncio.Task[bool] = asyncio.create_task(_reconcile())
        self._triager_reconcile_tasks[key] = task

        def _done(t: asyncio.Task[bool]) -> None:
            if self._triager_reconcile_tasks.get(key) is t:
                del self._triager_reconcile_tasks[key]

        task.add_done_callback(_done)
        return True

    def _spawn_converge(self, pr_ref: PRRef) -> bool:
        """Run ``converge_pr`` as a background task; return immediately.

        The converge sub-machine is long-running (review dispatches + multi-round
        CI polling). Awaiting it inline in the webhook handler blows GitHub's
        webhook delivery timeout, causing redelivery and duplicate dispatches, and
        holds DB write transactions long enough to starve other writers
        ("database is locked"). Spawning it decouples the HTTP response from the work.

        De-duplicated per PR: if a converge for this PR is already in flight, this
        is a no-op (returns False) so a burst of synchronize/labeled events — or a
        redelivery — does not stack concurrent converge runs on the same PR.

        Returns True if a new task was spawned, False if one was already running.
        """
        key = f"{pr_ref.repo.owner}/{pr_ref.repo.name}#{pr_ref.number}"
        existing = self._converge_tasks.get(key)
        if existing is not None and not existing.done():
            return False

        task = asyncio.create_task(self.converge_pr(pr_ref))
        self._converge_tasks[key] = task

        def _done(t: asyncio.Task[PRState]) -> None:
            # Drop the ref only if it is still the task we registered (a newer
            # converge may have replaced it).
            if self._converge_tasks.get(key) is t:
                del self._converge_tasks[key]
            if not t.cancelled() and (exc := t.exception()) is not None:
                _log.error("Background converge for %s failed: %r", key, exc)

        task.add_done_callback(_done)
        return True

    # -----------------------------------------------------------------------
    # Run observation
    # -----------------------------------------------------------------------

    async def list_runs(self, repo: RepoRef) -> list[RunSummary]:
        """Return dispatched runs for the given repo, newest first.

        Reads from the run_store — the single source of truth populated at
        dispatch time.  Falls back to the session port for any runs that were
        seeded directly (dev-mode demo data, backward-compat).
        """
        store_runs = await self._run_store.list_runs(repo)
        if store_runs:
            return store_runs
        # Backward-compat fallback: session port (used by dev-mode demo seeds).
        return await self.session.list_runs(repo)

    async def get_run(self, run_id: str) -> RunDetail:
        """Return detail for a single run, with transcript events from the harness.

        Loads run metadata from the run_store (status, repo, type, timestamps),
        then merges transcript events from the harness RunEventStore so the initial
        page load shows the full transcript captured during the run.

        The harness RunEventStore is the authoritative source for events (see
        file comment at orchestrator.py:65 and the root-cause fix for the
        disconnected-stores bug).  The run_store holds run *metadata* only;
        events are in the RunEventStore which the backend writes into.

        Falls back to the session port for dev-mode seeded runs (backward-compat).
        """
        detail = await self._run_store.get_run(run_id)
        if detail is not None:
            # Merge transcript events from the harness RunEventStore.
            # run_store.get_run returns events=[] because the harness never writes
            # into the run_store events list — it writes into the RunEventStore.
            # We union them here: prefer harness events (the authoritative source)
            # and include any events already in the run_store record (there should
            # be none in practice, but we deduplicate by position to be safe).
            harness_events = self.harness.get_run_events(run_id)
            if harness_events:
                return detail.model_copy(update={"events": harness_events})
            return detail
        # Backward-compat fallback: session port (dev-mode demo seeds).
        return await self.session.get_run(run_id)

    def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Stream live events for a run: backfill + live from the harness RunEventStore.

        Previously this called self.session.stream_events(run_id), which read from a
        different store (SessionPort) that the harness backend never writes to — so
        the stream was always empty (root-cause fix for the disconnected-stores bug).

        The harness RunEventStore is the authoritative source: the backend (subprocess
        or K8s) streams the agent's JSONL into it via RunEventStore.append().
        subscribe_run_events() yields the full backlog first (late subscribers see
        events from before they opened the stream) then live events until completion.
        """
        return self.harness.subscribe_run_events(run_id)

    async def status(self, repo: RepoRef) -> HealthReport:
        return await pipeline_health(repo, self.forge)

    async def dev_dispatch(self, repo: RepoRef) -> RunHandle:
        """Fire a fake issues:labeled agent-work event (dev/demo path only).

        Bypasses the full Engine.dispatch path so forge state setup is unnecessary.
        """
        from src.decisions.route_entry import route_entry as _route_entry
        from src.domain.types import DispatchContext

        issue_ref = IssueRef(repo=repo, number=1)
        result = _route_entry("issues")
        context = DispatchContext(
            issue_ref=issue_ref,
            contract=result.contract,
            model=result.model,
            max_turns=result.max_turns,
            forge_token_scope="repo-branch",
            allowed_agent_refs=None,
        )
        return await self.harness.dispatch(context)

    # -----------------------------------------------------------------------
    # Escalation management
    # -----------------------------------------------------------------------

    async def list_escalations(self, repo: RepoRef) -> list[dict[str, object]]:
        """List PRs currently carrying LABEL_NEEDS_HUMAN (escalated).

        Returns a list of dicts with pr_number, labels, and cause hint derived
        from labels (E7=merge-conflict, E8=stale build-cap, etc.).
        """
        prs = await self.forge.list_prs(repo, state="open", labels=[LABEL_NEEDS_HUMAN])
        result: list[dict[str, object]] = []
        for pr in prs:
            cause = _infer_escalation_cause(pr.labels)
            result.append(
                {
                    "pr_number": pr.ref.number,
                    "labels": pr.labels,
                    "title": pr.title,
                    "cause": cause,
                }
            )
        return result

    async def deescalate_pr(
        self,
        pr_ref: PRRef,
        operator: str,
    ) -> None:
        """Remove LABEL_NEEDS_HUMAN from a PR, reset counters, clear converge state.

        P16/P17 recovery path (SPEC §11.3):
          1. Acquire per-entity advisory lock on pr_ref (TOCTOU guard).
          2. Read current labels for audit record.
          3. Remove LABEL_NEEDS_HUMAN from the PR.
          4. Reset ``stale-pr`` and ``converge-retry`` counters (SPEC §11.3 + §8.2a).
          5. Clear converge state so next converge starts at R1 (H3 fix).
          6. Write audit record — after all mutations (observer pattern, I6).
          7. Release lock.
        """
        # Step 1 — per-entity advisory lock — serializes concurrent de-escalation calls
        # (operator double-click, two API replicas, reconciler racing a human clear).
        async with self._lock_provider.lock(pr_ref):
            # Step 2 — read pre-mutation labels for the audit record
            pr = await self.forge.get_pr(pr_ref)
            pr_labels_at_deescalation = list(pr.labels)

            # Step 3 — remove the escalation label
            await self.forge.remove_label(pr_ref, LABEL_NEEDS_HUMAN)

            # Step 4 — reset stale-pr counter so RC-1 starts fresh.
            # A None counter store would let RC-1 immediately re-escalate (redispatch_count
            # stays at cap); raise so the operator knows the store is mis-wired.
            if self._counter is None:
                raise RuntimeError(
                    "deescalate_pr: counter store is None — "
                    "cannot reset stale-pr/converge-retry counters; "
                    "RC-1 would re-escalate immediately"
                )
            await self._counter.reset(pr_ref, "stale-pr")
            await self._counter.reset(pr_ref, "converge-retry")

            # Step 5 — clear converge loop state so next Engine.converge starts at R1.
            # A None converge_state store would leave stale round data; raise so the
            # operator knows the store is mis-wired.
            if self._converge_state is None:
                raise RuntimeError(
                    "deescalate_pr: converge_state store is None — "
                    "cannot clear converge round; stale ConvergeState may cause incorrect re-entry"
                )
            await self._converge_state.clear_converge_state(pr_ref)

            # Step 6 — single complete §11.3 audit record (I6, observer pattern: written
            # after all state changes so the trail records only committed state).
            escalation_cause = _infer_escalation_cause(pr_labels_at_deescalation)
            await self._audit.record(
                repo=pr_ref.repo,
                entity_ref=pr_ref,
                action="deescalate_pr",
                operator=operator,
                escalation_cause=escalation_cause if escalation_cause else None,
                pr_labels=pr_labels_at_deescalation,
            )

    # -----------------------------------------------------------------------
    # Triage (human intake gate) — SPEC §11.3
    # -----------------------------------------------------------------------

    async def run_intake(self, issue_ref: IssueRef) -> RunHandle | None:
        """Run the intake gate for an issue (called by event routing).

        When a registry is configured and the issue's repo is registered, the
        per-repo allowlist and owner are used.  Falls back to the service-level
        defaults for unregistered repos or when no registry is set.

        Returns the triager RunHandle (or None when intake was skipped).
        Spawns the background triager-divergence reconciliation task (SPEC §10.4 step 6).
        """
        if self._registry is not None:
            repo_config = await self._registry.get_repo(issue_ref.repo)
            if repo_config is not None and repo_config.enabled:
                engine = IntakeEngine(
                    forge=self.forge,
                    harness=self.harness,
                    session=self.session,
                    audit=self._audit,
                    allowlist=repo_config.allowlist,
                    owner=repo_config.repo.owner,
                )
                result = await engine.intake(issue_ref)
                if result.handle is not None and result.decision is not None:
                    self._spawn_triager_reconcile(engine, issue_ref, result.decision)
                return result.handle
        result = await self._intake_engine.intake(issue_ref)
        if result.handle is not None and result.decision is not None:
            self._spawn_triager_reconcile(self._intake_engine, issue_ref, result.decision)
        return result.handle

    async def list_triage(self, repo: RepoRef) -> list[TriageItem]:
        """List issues currently in AWAITING_PROMOTION state.

        queued_at reflects the actual time the issue was queued (intake:queue audit entry).
        Falls back to current time if no audit entry is found (e.g. legacy data).
        """
        issues = await self.forge.list_issues(repo, [LABEL_AWAITING_PROMOTION])
        now = datetime.now(tz=UTC)
        items: list[TriageItem] = []
        for issue in issues:
            entries = await self._audit.list_entries(repo, issue.ref)
            queue_entries = [e for e in entries if e["action"] == "intake:queue"]
            if queue_entries:
                queued_at = datetime.fromisoformat(str(queue_entries[0]["ts"]))
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=UTC)
            else:
                queued_at = now
            items.append(
                TriageItem(
                    issue_ref=issue.ref,
                    title=issue.title,
                    body=issue.body,
                    author=issue.author,
                    labels=issue.labels,
                    queued_at=queued_at,
                )
            )
        return items

    async def promote(self, issue_ref: IssueRef, operator: str) -> RunHandle:
        """Promote an issue from AWAITING_PROMOTION to AGENT_WORK.

        Steps (SPEC §11.3, I7):
          1. Acquire per-entity advisory lock on issue_ref (TOCTOU guard).
          2. Atomic label swap: set_labels([LABEL_TRIAGE, LABEL_AGENT_WORK]).
          3. Dispatch agent — bypasses dedup guard (explicit human promotion always dispatches).
          4. Write audit record (I6 + I7) — after observable state is changed.
          5. Release lock.

        # operator is a placeholder; Phase 9 auth will derive it from an authenticated session.
        """
        from src.decisions.route_entry import route_entry as _route_entry
        from src.domain.types import DispatchContext

        # Step 1: per-entity advisory lock — serializes concurrent promote calls on the
        # same issue (operator double-click, two API replicas racing).
        async with self._lock_provider.lock(issue_ref):
            # Step 2: atomic label swap (PUT semantics — I7)
            await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AGENT_WORK])

            # Step 3: dispatch directly via harness — bypasses dedup guard so an explicit
            # human promotion always results in a real agent run, never a silent no-op.
            result = _route_entry("issues")
            context = DispatchContext(
                issue_ref=issue_ref,
                contract=result.contract,
                model=result.model,
                max_turns=result.max_turns,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            handle = await self.harness.dispatch(context)

            # Step 4: audit the human promotion (I6) — written after observable state is set
            await self._audit.record(
                repo=issue_ref.repo,
                entity_ref=issue_ref,
                action="promote",
                operator=operator,
            )

        return handle

    async def decline(self, issue_ref: IssueRef, operator: str) -> None:
        """Decline an issue: close it via label removal + audit.

        In a real forge adapter, decline would call close_issue(); here we
        remove the awaiting-promotion label so it no longer appears in triage
        and record the decline in the audit log.
        """
        # Remove awaiting-promotion label (issue remains open; real impl would close)
        await self.forge.remove_label(issue_ref, LABEL_AWAITING_PROMOTION)

        # Audit the decline (I6)
        await self._audit.record(
            repo=issue_ref.repo,
            entity_ref=issue_ref,
            action="decline",
            operator=operator,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _empty_async_iter() -> AsyncGenerator[RunEvent, None]:
    """Async generator that yields nothing — fallback for harnesses without streaming."""
    # Unreachable yield makes this an async generator function rather than a coroutine.
    # Without any yield, Python would treat this as a regular coroutine function and
    # calling it would return a coroutine object rather than an AsyncGenerator/AsyncIterator.
    if False:  # pragma: no cover
        yield


def _cron_to_seconds(cron: str) -> int:
    """Convert a simple cron expression to seconds between ticks.

    Supports only ``*/N * * * *`` (every N minutes) format — sufficient for
    the RECONCILER_CRON default ``*/15 * * * *`` (900 seconds).
    """
    parts = cron.strip().split()
    if len(parts) == 5 and parts[0].startswith("*/") and all(p == "*" for p in parts[1:]):
        minutes = int(parts[0][2:])
        return minutes * 60
    return 900  # fallback: 15 minutes


def _infer_escalation_cause(labels: list[str]) -> str:
    """Heuristic escalation cause from PR labels (for the UI escalation section)."""
    if "converge" in labels:
        return "E5:cap-reached or E2:no-progress"
    if "agent:implementing" in labels:
        return "E8:stale-build-cap or E9:stale-no-issue"
    return "E7:merge-conflict or manual"
