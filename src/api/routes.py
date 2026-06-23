"""FastAPI router for the orchestrator API."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.auth import require_auth
from src.domain.types import (
    HealthReport,
    IssueRef,
    PRRef,
    RepoRef,
    RunDetail,
    RunSummary,
    TriageItem,
)
from src.service.orchestrator import OrchestratorService
from src.service.registry import RepoRegistryPort

# Type alias for the auth dependency payload
AuthPayload = dict[str, object]


class RepoSummary(BaseModel):
    """Public view of a repo registry entry (no credentials — I3)."""

    owner: str
    name: str
    enabled: bool
    intake_enabled: bool


async def _active_repo(registry: RepoRegistryPort) -> RepoRef | None:
    """Return the first enabled repo from the registry, or None if empty."""
    enabled = await registry.enabled_repos()
    if not enabled:
        return None
    return enabled[0].repo


def _make_router(service: OrchestratorService, registry: RepoRegistryPort) -> APIRouter:
    r = APIRouter()

    @r.get("/api/status", response_model=HealthReport | None)
    async def get_status(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> HealthReport | None:
        repo = await _active_repo(registry)
        if repo is None:
            return None
        return await service.status(repo)

    @r.get("/api/runs", response_model=list[RunSummary])
    async def list_runs(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[RunSummary]:
        repo = await _active_repo(registry)
        if repo is None:
            return []
        return await service.list_runs(repo)

    @r.get("/api/runs/{run_id}", response_model=RunDetail)
    async def get_run(
        run_id: str,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> RunDetail:
        return await service.get_run(run_id)

    @r.get("/api/runs/{run_id}/stream")
    async def stream_run(
        run_id: str,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> StreamingResponse:
        async def _generate() -> AsyncGenerator[str, None]:
            async for event in service.stream_run(run_id):
                payload = json.dumps(
                    {
                        "event_type": event.event_type,
                        "data": event.data,
                        "timestamp": event.timestamp.isoformat(),
                    }
                )
                yield f"data: {payload}\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"X-Dev-Mode": "true", "Cache-Control": "no-cache"},
        )

    @r.post("/api/dev/dispatch")
    async def dev_dispatch(
        response: Response,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, str]:
        response.headers["X-Dev-Mode"] = "true"
        repo = await _active_repo(registry)
        if repo is None:
            raise HTTPException(status_code=404, detail="No enabled repo configured")
        handle = await service.dev_dispatch(repo)
        return {"run_id": handle.run_id}

    # ------------------------------------------------------------------
    # Triage endpoints
    # ------------------------------------------------------------------

    @r.get("/api/triage", response_model=list[TriageItem])
    async def list_triage(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[TriageItem]:
        repo = await _active_repo(registry)
        if repo is None:
            return []
        return await service.list_triage(repo)

    @r.post("/api/triage/{issue_number}/promote")
    async def promote_issue(
        issue_number: int,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, str]:
        operator = str(auth.get("sub", "operator"))
        repo = await _active_repo(registry)
        if repo is None:
            raise HTTPException(status_code=404, detail="No enabled repo configured")
        issue_ref = IssueRef(repo=repo, number=issue_number)
        handle = await service.promote(issue_ref, operator=operator)
        return {"status": "promoted", "run_id": handle.run_id}

    @r.post("/api/triage/{issue_number}/decline")
    async def decline_issue(
        issue_number: int,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, str]:
        operator = str(auth.get("sub", "operator"))
        repo = await _active_repo(registry)
        if repo is None:
            raise HTTPException(status_code=404, detail="No enabled repo configured")
        issue_ref = IssueRef(repo=repo, number=issue_number)
        await service.decline(issue_ref, operator=operator)
        return {"status": "declined"}

    # ------------------------------------------------------------------
    # Escalation endpoints (Step 6 — SPEC §8.9)
    # ------------------------------------------------------------------

    @r.get("/api/repos/{owner}/{repo_name}/escalations")
    async def list_escalations(
        owner: str,
        repo_name: str,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[dict[str, object]]:
        repo = RepoRef(owner=owner, name=repo_name)
        return await service.list_escalations(repo)

    @r.post("/api/repos/{owner}/{repo_name}/prs/{pr_number}/deescalate")
    async def deescalate_pr(
        owner: str,
        repo_name: str,
        pr_number: int,
        auth: Annotated[AuthPayload, Depends(require_auth)],
        operator: str = "operator",
    ) -> dict[str, str]:
        actual_operator = str(auth.get("sub", operator))
        repo = RepoRef(owner=owner, name=repo_name)
        pr_ref = PRRef(repo=repo, number=pr_number)
        await service.deescalate_pr(pr_ref, operator=actual_operator)
        return {"status": "deescalated"}

    @r.post("/api/dev/reconcile")
    async def dev_reconcile(
        response: Response,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[dict[str, object]]:
        response.headers["X-Dev-Mode"] = "true"
        reports = await service.reconcile_now()
        return [
            {
                "stale_acted": r.stale_acted,
                "conflicts_flagged": r.conflicts_flagged,
                "rearmed": r.rearmed,
                "redispatched": r.redispatched,
                "escalated": r.escalated,
            }
            for r in reports
        ]

    # ------------------------------------------------------------------
    # Repo registry endpoint
    # ------------------------------------------------------------------

    @r.get("/api/repos", response_model=list[RepoSummary])
    async def list_repos(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[RepoSummary]:
        configs = await registry.list_repos()
        return [
            RepoSummary(
                owner=cfg.repo.owner,
                name=cfg.repo.name,
                enabled=cfg.enabled,
                intake_enabled=cfg.intake_enabled,
            )
            for cfg in configs
        ]

    return r
