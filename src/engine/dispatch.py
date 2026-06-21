"""Engine — core dispatch logic."""

from __future__ import annotations

from src.decisions.route_entry import route_entry
from src.domain.types import (
    LABEL_IMPLEMENTING,
    DispatchContext,
    IssueRef,
    PRRef,
    RunHandle,
)
from src.ports.base import ForgePort, HarnessPort, SessionPort


class Engine:
    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
    ) -> None:
        self.forge = forge
        self.harness = harness
        self.session = session

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
                if f"Closes #{issue_ref.number}" in pr.body:
                    return None  # already dispatching

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

        elif event_name in ("issue_comment", "pull_request_review_comment"):
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
