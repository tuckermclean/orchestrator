"""OrchestratorService — top-level service wiring."""

from __future__ import annotations

from src.decisions.pipeline_health import pipeline_health
from src.domain.types import (
    HealthReport,
    IssueRef,
    PRRef,
    RepoRef,
    RunDetail,
    RunHandle,
    RunSummary,
)
from src.engine.dispatch import Engine
from src.ports.base import ForgePort, HarnessPort, SessionPort


class OrchestratorService:
    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
    ) -> None:
        self.engine = Engine(forge, harness, session)
        self.forge = forge
        self.harness = harness
        self.session = session

    async def handle_event(self, event_name: str, payload: dict[str, object]) -> None:
        """Route forge webhook events to the engine."""
        issue_ref: IssueRef | None = None
        pr_ref: PRRef | None = None
        comment_body: str | None = None

        # Extract refs from payload
        if "issue" in payload:
            issue_data = payload["issue"]
            if isinstance(issue_data, dict):
                repo_data = payload.get("repository", {})
                if isinstance(repo_data, dict):
                    repo = RepoRef(
                        owner=str(repo_data.get("owner", {}).get("login", ""))
                        if isinstance(repo_data.get("owner"), dict)
                        else str(repo_data.get("owner", "")),
                        name=str(repo_data.get("name", "")),
                    )
                    issue_ref = IssueRef(
                        repo=repo,
                        number=int(issue_data.get("number", 0)),
                    )

        if "pull_request" in payload:
            pr_data = payload["pull_request"]
            if isinstance(pr_data, dict):
                repo_data = payload.get("repository", {})
                if isinstance(repo_data, dict):
                    repo = RepoRef(
                        owner=str(repo_data.get("owner", {}).get("login", ""))
                        if isinstance(repo_data.get("owner"), dict)
                        else str(repo_data.get("owner", "")),
                        name=str(repo_data.get("name", "")),
                    )
                    pr_ref = PRRef(
                        repo=repo,
                        number=int(pr_data.get("number", 0)),
                    )

        if "comment" in payload:
            comment_data = payload["comment"]
            if isinstance(comment_data, dict):
                comment_body = str(comment_data.get("body", ""))

        await self.engine.dispatch(
            event_name,
            issue_ref=issue_ref,
            pr_ref=pr_ref,
            comment_body=comment_body,
        )

    async def list_runs(self, repo: RepoRef) -> list[RunSummary]:
        return await self.session.list_runs(repo)

    async def get_run(self, run_id: str) -> RunDetail:
        return await self.session.get_run(run_id)

    async def status(self, repo: RepoRef) -> HealthReport:
        return await pipeline_health(repo, self.forge)

    async def dev_dispatch(self, repo: RepoRef) -> RunHandle:
        """Fire a fake issues:labeled agent-work event."""
        issue_ref = IssueRef(repo=repo, number=1)
        handle = await self.engine.dispatch("issues", issue_ref=issue_ref)
        if handle is None:
            # If dedup guard fired, create a fresh dispatch directly
            from src.decisions.route_entry import route_entry as _route_entry
            from src.domain.types import DispatchContext

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
        return handle
