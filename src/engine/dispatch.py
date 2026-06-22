"""Engine — core dispatch logic."""

from __future__ import annotations

import time

from src.decisions.route_entry import route_entry
from src.domain.types import (
    _CLOSING_RE,
    CI_WAIT_S,
    LABEL_AGENT_WORK,
    LABEL_IMPLEMENTING,
    DispatchContext,
    IssueRef,
    PRRef,
    PRState,
    RunHandle,
)
from src.ports.base import ConvergeStateStore, CounterStore, ForgePort, HarnessPort, SessionPort


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
            # Dedup guard — skip if an implementing PR already references this issue
            repo = issue_ref.repo
            open_prs = await self.forge.list_prs(
                repo, state="open", labels=[LABEL_IMPLEMENTING]
            )
            for pr in open_prs:
                matched_nums = {int(m) for m in _CLOSING_RE.findall(pr.body)}
                if issue_ref.number in matched_nums:
                    return None  # already dispatching

            # Open a draft PR and mark the issue as in-progress (§10.1 step 2)
            pr_body = f"Closes #{issue_ref.number}"
            await self.forge.create_pr(
                repo=issue_ref.repo,
                title=f"Fix #{issue_ref.number}",
                body=pr_body,
                head=f"fix/issue-{issue_ref.number}",
                base="main",
                draft=True,
            )
            await self.forge.add_label(issue_ref, LABEL_IMPLEMENTING)

            context = DispatchContext(
                issue_ref=issue_ref,
                contract=result.contract,
                model=result.model,
                max_turns=result.max_turns,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            handle = await self.harness.dispatch(context)
            return handle

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
            handle = await self.harness.dispatch(context)
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
            handle = await self.harness.dispatch(context)
            return handle

        return None

    async def converge(self, pr_ref: PRRef) -> PRState:
        """Run the converge sub-machine for one PR (SPEC §10.2)."""
        from src.engine.converge import converge as _converge

        return await _converge(self, pr_ref)

    async def _await_run(self, handle: RunHandle) -> bool:
        """Poll a dispatched run until completed or CI_WAIT_S elapses.

        Returns True if the run completed. On `CI_WAIT_S` timeout, cancels the run
        (`harness.cancel`) before returning False so a ghost agent cannot complete later
        and overwrite the next round's init sentinel or verdict file (SPEC §9.2, §10.2
        step 4b). Idempotent cancel — safe for both reviewer and fixer handles. The fake
        harness completes synchronously; the real adapter honours the wall-clock budget.
        """
        deadline = time.monotonic() + CI_WAIT_S
        while True:
            status = await self.harness.get_run_status(handle)
            if status.state == "completed":
                return True
            if time.monotonic() >= deadline:
                await self.harness.cancel(handle)
                return False
