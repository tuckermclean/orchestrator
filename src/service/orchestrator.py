"""OrchestratorService — top-level service wiring."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from src.db.audit import AuditLog
from src.decisions.pipeline_health import pipeline_health
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_NEEDS_HUMAN,
    LABEL_TRIAGE,
    RECONCILER_CRON,
    HealthReport,
    IssueRef,
    PRRef,
    RepoRef,
    RunDetail,
    RunEvent,
    RunHandle,
    RunSummary,
    TriageItem,
)
from src.engine.dispatch import Engine
from src.engine.intake import IntakeEngine
from src.engine.reconcile import ReconcileReport
from src.ports.base import ConvergeStateStore, CounterStore, ForgePort, HarnessPort, SessionPort

# Default LRU dedup window (number of delivery IDs to remember)
_DEFAULT_DEDUP_WINDOW = 1000


def _extract_repo(payload: dict[str, object]) -> RepoRef | None:
    repo_data = payload.get("repository", {})
    if not isinstance(repo_data, dict):
        return None
    owner_data = repo_data.get("owner")
    owner = (
        str(owner_data.get("login", "")) if isinstance(owner_data, dict)
        else str(owner_data or "")
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
        counter: CounterStore | None = None,
        converge_state: ConvergeStateStore | None = None,
        dedup_window: int = _DEFAULT_DEDUP_WINDOW,
    ) -> None:
        self.engine = Engine(
            forge=forge,
            harness=harness,
            session=session,
            counter=counter,
            converge_state=converge_state,
        )
        self.forge = forge
        self.harness = harness
        self.session = session
        self._counter = counter
        self._converge_state = converge_state

        # Audit log — default to in-memory if none provided
        self._audit = audit if audit is not None else AuditLog()
        self._allowlist = allowlist if allowlist is not None else []

        self._intake_engine = IntakeEngine(
            forge=forge,
            harness=harness,
            session=session,
            audit=self._audit,
            allowlist=self._allowlist,
        )

        # Delivery-ID LRU dedup cache (SPEC §11.3) — bounded by dedup_window entries
        self._dedup_cache: OrderedDict[str, bool] = OrderedDict()
        self._dedup_window = dedup_window

        # Reconciler cron task handle
        self._reconcile_task: asyncio.Task[None] | None = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def startup(self) -> None:
        """Initialise async resources (call once from the ASGI lifespan handler)."""
        await self._audit.init()

    async def start_reconciler(self, repo: RepoRef) -> None:
        """Start the reconciler cron loop for the given repo.

        Runs ``Engine.reconcile`` every ``RECONCILER_CRON`` cadence (15 min) in the
        background.  Call ``stop_reconciler()`` to cancel the task cleanly.
        """
        async def _loop() -> None:
            while True:
                await asyncio.sleep(_cron_to_seconds(RECONCILER_CRON))
                try:
                    await self.engine.reconcile(repo)
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

        if event_name == "issues" and issue_ref is not None:
            await self.run_intake(issue_ref)
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
        """Run reconcile immediately for one repo (or the default repo).

        Idempotent: calling twice produces the same effect as calling once since
        the reconciler re-reads live forge label state and skips already-acted entities.
        """
        target = repo if repo is not None else RepoRef(owner="demo", name="repo")
        report = await self.engine.reconcile(target)
        return [report]

    # -----------------------------------------------------------------------
    # Run observation
    # -----------------------------------------------------------------------

    async def list_runs(self, repo: RepoRef) -> list[RunSummary]:
        return await self.session.list_runs(repo)

    async def get_run(self, run_id: str) -> RunDetail:
        return await self.session.get_run(run_id)

    def stream_run(self, run_id: str) -> AsyncIterator[RunEvent]:
        return self.session.stream_events(run_id)

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
          1. Read current labels for audit record.
          2. Remove LABEL_NEEDS_HUMAN from the PR.
          3. Reset ``stale-pr`` counter (SPEC §11.3 + §8.2a).
          4. Clear converge state so next converge starts at R1 (H3 fix).
          5. Write audit record — after all mutations (observer pattern, I6).
        """
        # Step 1 — read pre-mutation labels for the audit record
        pr = await self.forge.get_pr(pr_ref)
        pr_labels_at_deescalation = list(pr.labels)

        # Step 2 — remove the escalation label
        await self.forge.remove_label(pr_ref, LABEL_NEEDS_HUMAN)

        # Step 3 — reset stale-pr counter so RC-1 starts fresh
        if self._counter is not None:
            await self._counter.reset(pr_ref, "stale-pr")
            await self._counter.reset(pr_ref, "converge-retry")

        # Step 4 — clear converge loop state so next Engine.converge starts at R1
        if self._converge_state is not None:
            await self._converge_state.clear_converge_state(pr_ref)

        # Step 5 — audit record (I6, observer pattern: written after state change)
        await self._audit.record(
            repo=pr_ref.repo,
            entity_ref=pr_ref,
            action="deescalate_pr",
            operator=operator,
        )
        # Record the pre-mutation labels as context (stored as a second audit entry)
        label_str = ",".join(pr_labels_at_deescalation)
        await self._audit.record(
            repo=pr_ref.repo,
            entity_ref=pr_ref,
            action=f"deescalate_pr:labels={label_str}",
            operator=operator,
        )

    # -----------------------------------------------------------------------
    # Triage (human intake gate) — SPEC §11.3
    # -----------------------------------------------------------------------

    async def run_intake(self, issue_ref: IssueRef) -> RunHandle | None:
        """Run the intake gate for an issue (called by event routing)."""
        return await self._intake_engine.intake(issue_ref)

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
          1. Atomic label swap: set_labels([LABEL_TRIAGE, LABEL_AGENT_WORK]).
          2. Dispatch agent — bypasses dedup guard (explicit human promotion always dispatches).
          3. Write audit record (I6 + I7) — after observable state is changed.

        # TODO: operator must be derived from an authenticated session before production use.
        """
        from src.decisions.route_entry import route_entry as _route_entry
        from src.domain.types import DispatchContext

        # Step 1: atomic label swap (PUT semantics — I7, no TOCTOU)
        await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AGENT_WORK])

        # Step 2: dispatch directly via harness — bypasses dedup guard so an explicit
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

        # Step 3: audit the human promotion (I6) — written after observable state is set
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
