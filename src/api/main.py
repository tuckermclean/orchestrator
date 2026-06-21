"""App factory and dev startup."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.routes import _make_router
from src.domain.types import RepoRef
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService


def create_app(service: OrchestratorService) -> FastAPI:
    """Create the FastAPI application, mounting static UI if built."""
    app = FastAPI(title="Orchestrator", version="0.1.0")

    # CORS for dev (Vite dev server at :5173)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register API routes
    app.include_router(_make_router(service))

    # Serve Vite build if it exists
    ui_dist = Path(__file__).parent.parent.parent / "ui" / "dist"
    if ui_dist.exists():
        app.mount("/", StaticFiles(directory=str(ui_dist), html=True), name="static")

    return app


def _build_dev_service() -> OrchestratorService:
    """Wire fake ports for dev/demo mode."""
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()

    service = OrchestratorService(forge=forge, harness=harness, session=session)
    return service


async def _seed_demo_data(service: OrchestratorService) -> None:
    """Pre-seed some fake runs for the dev UI."""
    from src.domain.types import RunEvent

    repo = RepoRef(owner="demo", name="repo")

    session = service.session
    if not isinstance(session, FakeSessionPort):
        return

    now = datetime.now(tz=UTC)

    session.seed_run_summary(
        run_id="demo-run-1",
        repo=repo,
        type="issues",
        status="completed",
        started_at=now,
        events=[
            RunEvent(event_type="queued", data={}, timestamp=now),
            RunEvent(event_type="completed", data={"conclusion": "success"}, timestamp=now),
        ],
    )
    session.seed_run_summary(
        run_id="demo-run-2",
        repo=repo,
        type="issue_comment",
        status="in_progress",
        started_at=now,
        events=[
            RunEvent(event_type="queued", data={}, timestamp=now),
            RunEvent(event_type="in_progress", data={"message": "Running"}, timestamp=now),
        ],
    )


# Singleton for ASGI app (used by uvicorn)
_service = _build_dev_service()
app = create_app(_service)


@app.on_event("startup")
async def on_startup() -> None:
    await _seed_demo_data(_service)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
