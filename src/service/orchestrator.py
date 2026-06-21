"""OrchestratorService — top-level service wiring."""

from __future__ import annotations

from datetime import UTC, datetime

from src.db.audit import AuditLog
from src.decisions.pipeline_health import pipeline_health
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_TRIAGE,
    HealthReport,
    IssueRef,
    PRRef,
    RepoRef,
    RunDetail,
    RunHandle,
    RunSummary,
    TriageItem,
)
from src.engine.dispatch import Engine
from src.engine.intake import IntakeEngine
from src.ports.base import ForgePort, HarnessPort, SessionPort


class OrchestratorService:
    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
        audit: AuditLog | None = None,
        allowlist: list[str] | None = None,
    ) -> None:
        self.engine = Engine(forge, harness, session)
        self.forge = forge
        self.harness = harness
        self.session = session

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

    # -----------------------------------------------------------------------
    # Triage (human intake gate) — SPEC §11.3
    # -----------------------------------------------------------------------

    async def run_intake(self, issue_ref: IssueRef) -> RunHandle | None:
        """Run the intake gate for an issue (called by event routing)."""
        return await self._intake_engine.intake(issue_ref)

    async def list_triage(self, repo: RepoRef) -> list[TriageItem]:
        """List issues currently in AWAITING_PROMOTION state."""
        issues = await self.forge.list_issues(repo, [LABEL_AWAITING_PROMOTION])
        now = datetime.now(tz=UTC)
        return [
            TriageItem(
                issue_ref=issue.ref,
                title=issue.title,
                body=issue.body,
                author=issue.author,
                labels=issue.labels,
                queued_at=now,
            )
            for issue in issues
        ]

    async def promote(self, issue_ref: IssueRef, operator: str) -> RunHandle:
        """Promote an issue from AWAITING_PROMOTION to AGENT_WORK.

        Steps (SPEC §11.3, I7):
          1. Atomic label swap: set_labels([LABEL_TRIAGE, LABEL_AGENT_WORK]).
          2. Write audit record (I6 + I7).
          3. Dispatch agent.
        """
        # Step 1: atomic label swap (PUT semantics — I7, no TOCTOU)
        await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AGENT_WORK])

        # Step 2: audit the human promotion (I6)
        await self._audit.record(
            repo=issue_ref.repo,
            entity_ref=issue_ref,
            action="promote",
            operator=operator,
        )

        # Step 3: dispatch the implementer (label change fires issues:labeled in real forge,
        # but we dispatch directly in dev/test mode for immediate feedback)
        handle = await self.engine.dispatch("issues", issue_ref=issue_ref)
        if handle is None:
            # Dedup guard fired — return a null handle sentinel
            from src.domain.types import RunHandle

            handle = RunHandle(run_id="no-op-dedup")
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
