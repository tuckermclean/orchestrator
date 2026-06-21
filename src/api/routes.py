"""FastAPI router for the orchestrator API."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Response
from fastapi.responses import StreamingResponse

from src.domain.types import HealthReport, RepoRef, RunDetail, RunSummary
from src.service.orchestrator import OrchestratorService

# Default demo repo used in dev mode
_DEV_REPO = RepoRef(owner="demo", name="repo")


def _make_router(service: OrchestratorService) -> APIRouter:
    r = APIRouter()

    @r.get("/api/status", response_model=HealthReport)
    async def get_status() -> HealthReport:
        return await service.status(_DEV_REPO)

    @r.get("/api/runs", response_model=list[RunSummary])
    async def list_runs() -> list[RunSummary]:
        return await service.list_runs(_DEV_REPO)

    @r.get("/api/runs/{run_id}", response_model=RunDetail)
    async def get_run(run_id: str) -> RunDetail:
        return await service.get_run(run_id)

    @r.get("/api/runs/{run_id}/stream")
    async def stream_run(run_id: str) -> StreamingResponse:
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
    async def dev_dispatch(response: Response) -> dict[str, str]:
        response.headers["X-Dev-Mode"] = "true"
        handle = await service.dev_dispatch(_DEV_REPO)
        return {"run_id": handle.run_id}

    return r
