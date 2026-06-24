"""Engine — core dispatch logic.

Dispatch sub-machine (SPEC §10.1 amended, §251):
  issues:labeled agent-work triggers a TWO-RUN sequential sub-machine:
    1. Orchestrator run (Opus, agents/orchestrator.md) — plans the implementation,
       opens the draft PR, adds LABEL_IMPLEMENTING, commits a skeleton.  The
       orchestrator does NOT write code and does NOT spawn the implementer inline.
    2. Implementer run (Sonnet, agents/implementer.md) — dispatched by the engine
       after the orchestrator opens the PR.  Reads the plan, writes code + tests,
       leaves gates green, marks the PR ready_for_review (P2).

  The sub-machine runs as a background task (_spawn_dispatch, OrchestratorService)
  so the webhook response is immediate, exactly like the converge sub-machine
  (_spawn_converge).  Both runs are awaited inside the same asyncio task via
  Engine._await_run.

  Error policy:
    - Orchestrator fails or opens no PR → do NOT dispatch the implementer.
      The issue stays QUEUED; RC-4 handles the orphan on the next reconciler tick.
    - Implementer fails → leave the PR draft (BUILDING); RC-1 handles stale drafts.
    - AllHarnessesExhausted at either dispatch point → HOLD; no label change;
      RC-4 / RC-3 re-arms as appropriate (SPEC §14.5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from src.decisions.route_entry import route_entry
from src.domain.types import (
    _CLOSING_RE,
    _IMPLEMENTER_MAX_TURNS,
    CI_WAIT_S,
    DEFAULT_SWARM_MODEL,
    IMPLEMENTER_CONTRACT,
    LABEL_AGENT_WORK,
    LABEL_IMPLEMENTING,
    POLL_INTERVAL_S,
    DispatchContext,
    IssueRef,
    PRRef,
    PRState,
    RepoRef,
    RunHandle,
)
from src.ports.base import ConvergeStateStore, CounterStore, ForgePort, HarnessPort, SessionPort
from src.ports.harness_registry import AllHarnessesExhausted, SessionLimitHold

if TYPE_CHECKING:
    from src.engine.reconcile import ReconcileReport

_log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
        counter: CounterStore | None = None,
        converge_state: ConvergeStateStore | None = None,
    ) -> None:
        self.forge = forge
        self.harness = harness
        self.session = session
        self.counter = counter
        self.converge_state = converge_state

    async def dispatch(
        self,
        event_name: str,
        issue_ref: IssueRef | None = None,
        pr_ref: PRRef | None = None,
        comment_body: str | None = None,
    ) -> RunHandle | None:
        result = route_entry(event_name)

        if event_name == "issues" and issue_ref is not None:
            # Dedup guard (§10.1 step 2 Layer B): skip if an implementing PR already
            # references this issue.  The orchestrator agent opens the draft PR
            # itself (agents/orchestrator.md Step 1) and adds LABEL_IMPLEMENTING
            # (Step 2); the control plane must not duplicate those actions.
            repo = issue_ref.repo
            open_prs = await self.forge.list_prs(
                repo, state="open", labels=[LABEL_IMPLEMENTING]
            )
            for pr in open_prs:
                matched_nums = {int(m) for m in _CLOSING_RE.findall(pr.body)}
                if issue_ref.number in matched_nums:
                    return None  # already building

            # --- Step 1: Orchestrator run (Opus) ---
            # route_entry("issues") returns ADJUDICATION_MODEL (Opus) / 40 turns /
            # agents/orchestrator.md — the orchestrator PLANS and opens the PR;
            # it does NOT write code and does NOT spawn the implementer inline.
            orchestrator_context = DispatchContext(
                issue_ref=issue_ref,
                contract=result.contract,   # agents/orchestrator.md
                model=result.model,         # claude-opus-4-8 (Opus)
                max_turns=result.max_turns, # 40
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            try:
                orch_handle = await self.harness.dispatch(orchestrator_context)
            except AllHarnessesExhausted:
                # All harnesses on cooldown — HOLD; entity stays QUEUED (SPEC §14.5).
                return None

            # Await the orchestrator so we can find the PR it opened before dispatching
            # the implementer.  This mirrors how Engine.converge awaits reviewers/fixers
            # via _await_run (SPEC §10.1 amended).  Timeout: CI_WAIT_S — same budget
            # as any other harness await; a stale orchestrator is caught by RC-1.
            orch_completed = await self._await_run(orch_handle)
            if not orch_completed:
                # Orchestrator timed out.  Leave the issue QUEUED; RC-4 re-dispatches.
                _log.warning(
                    "Orchestrator run timed out for issue %s#%d — skipping implementer",
                    issue_ref.repo.owner + "/" + issue_ref.repo.name,
                    issue_ref.number,
                )
                return orch_handle

            # --- Locate the PR the orchestrator opened ---
            # The orchestrator adds LABEL_IMPLEMENTING (Step 2, agents/orchestrator.md)
            # and includes `Closes #N` in the PR body (Step 1).  We find it by
            # filtering open implementing PRs for the `Closes #N` token.
            impl_prs = await self.forge.list_prs(
                repo, state="open", labels=[LABEL_IMPLEMENTING]
            )
            found_pr_ref: PRRef | None = None
            for candidate in impl_prs:
                matched_nums = {int(m) for m in _CLOSING_RE.findall(candidate.body)}
                if issue_ref.number in matched_nums:
                    found_pr_ref = candidate.ref
                    break

            if found_pr_ref is None:
                # Orchestrator did not open a PR (e.g. protected-path abort, crash).
                # Leave the issue QUEUED; RC-4 or operator handles re-dispatch.
                _log.warning(
                    "Orchestrator opened no implementing PR for issue %s#%d — "
                    "skipping implementer dispatch",
                    issue_ref.repo.owner + "/" + issue_ref.repo.name,
                    issue_ref.number,
                )
                return orch_handle

            # --- Step 2: Implementer run (Sonnet) ---
            # Dispatched by the engine (not by the orchestrator) on DEFAULT_SWARM_MODEL
            # so the model tier is enforced at the dispatch boundary (SPEC §251).
            # The implementer reads the plan from the PR, writes code + tests, and
            # marks the PR ready_for_review (P2), which triggers converge.
            implementer_context = DispatchContext(
                issue_ref=issue_ref,
                pr_ref=found_pr_ref,
                contract=IMPLEMENTER_CONTRACT,   # agents/implementer.md
                model=DEFAULT_SWARM_MODEL,       # claude-sonnet-4-6 (Sonnet)
                max_turns=_IMPLEMENTER_MAX_TURNS,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            try:
                impl_handle = await self.harness.dispatch(implementer_context)
            except AllHarnessesExhausted:
                # Harness exhausted after orchestrator completed — HOLD.  The PR draft
                # exists; RC-1 will recover the stale draft on the next tick.
                return orch_handle

            # Await the implementer.  On timeout the PR stays BUILDING (draft);
            # RC-1 handles stale drafts (SPEC §4 RC-1, SPEC §8.5).
            impl_completed = await self._await_run(impl_handle)
            if not impl_completed:
                _log.warning(
                    "Implementer run timed out for issue %s#%d — PR draft left for RC-1",
                    issue_ref.repo.owner + "/" + issue_ref.repo.name,
                    issue_ref.number,
                )

            return impl_handle

        elif event_name == "issue_comment" and issue_ref is not None:
            # H5 guard — only dispatch if the issue carries LABEL_AGENT_WORK (§10, §8.1)
            issue = await self.forge.get_issue(issue_ref)
            if LABEL_AGENT_WORK not in issue.labels:
                return None

            context = DispatchContext(
                issue_ref=issue_ref,
                pr_ref=pr_ref,
                contract=result.contract,
                model=result.model,
                max_turns=result.max_turns,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            try:
                handle = await self.harness.dispatch(context)
            except AllHarnessesExhausted:
                # All harnesses on cooldown — HOLD; entity stays QUEUED (SPEC §14.5).
                return None
            return handle

        elif event_name == "pull_request_review_comment" and pr_ref is not None:
            # H5 guard — only dispatch if the PR carries LABEL_IMPLEMENTING (§10, §8.1)
            pr = await self.forge.get_pr(pr_ref)
            if LABEL_IMPLEMENTING not in pr.labels:
                return None

            context = DispatchContext(
                issue_ref=issue_ref,
                pr_ref=pr_ref,
                contract=result.contract,
                model=result.model,
                max_turns=result.max_turns,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            try:
                handle = await self.harness.dispatch(context)
            except AllHarnessesExhausted:
                # All harnesses on cooldown — HOLD; entity stays CONVERGING (SPEC §14.5).
                return None
            return handle

        return None

    async def converge(
        self,
        pr_ref: PRRef,
    ) -> PRState:
        """Run the converge sub-machine for one PR (SPEC §10.2).

        ``ci_green`` is computed by trusting the repo's actual check runs —
        every present check must be completed and green (SPEC §7 CI green
        definition).  No allow-list; no per-repo override.
        """
        from src.engine.converge import converge as _converge

        return await _converge(self, pr_ref)

    async def reconcile(self, repo: RepoRef) -> ReconcileReport:
        """Run the four RC channels for a repo (SPEC §10.3)."""
        from src.engine.reconcile import reconcile as _reconcile

        return await _reconcile(self, repo)

    async def _await_run(self, handle: RunHandle) -> bool:
        """Poll a dispatched run until completed or CI_WAIT_S elapses.

        Returns True if the run completed successfully (not awaiting_quota).

        On `CI_WAIT_S` timeout, cancels the run (`harness.cancel`) before
        returning False so a ghost agent cannot complete later and overwrite
        the next round's init sentinel or verdict file (SPEC §9.2, §10.2
        step 4b). Idempotent cancel — safe for both reviewer and fixer handles.
        The fake harness completes synchronously; the real adapter honours the
        wall-clock budget.

        On ``awaiting_quota`` conclusion, raises ``SessionLimitHold`` (a
        subclass of ``AllHarnessesExhausted``) instead of returning True
        (SPEC §14.8). This makes the HOLD deterministic at the await boundary:
        callers that check the return value (fixer, nitpicker, adjudicator,
        orchestrator, implementer) are not reached, and existing
        ``except AllHarnessesExhausted`` handlers in converge.py and
        reconcile.py treat it as a HOLD — no label change, entity stays
        CONVERGING / BUILDING / QUEUED.  The harness cooldown is already
        armed by FailoverHarnessPort's status sink.
        """
        deadline = time.monotonic() + CI_WAIT_S
        while True:
            status = await self.harness.get_run_status(handle)
            if status.state == "completed":
                if status.conclusion == "awaiting_quota":
                    # Session/usage limit hit — raise HOLD instead of returning
                    # True so callers cannot mistake this for a successful run.
                    raise SessionLimitHold(
                        run_id=handle.run_id,
                        quota_reset_at=status.quota_reset_at,
                    )
                return True
            if time.monotonic() >= deadline:
                await self.harness.cancel(handle)
                return False
            await asyncio.sleep(POLL_INTERVAL_S)
