"""Engine — core dispatch logic."""

from __future__ import annotations

from src.decisions.route_entry import route_entry
from src.domain.types import (
    _CLOSING_RE,
    LABEL_AGENT_WORK,
    LABEL_IMPLEMENTING,
    DispatchContext,
    IssueRef,
    PRRef,
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
