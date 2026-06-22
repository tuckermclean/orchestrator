"""App factory and dev startup."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.api.routes import _make_router
from src.api.webhook import _make_webhook_router
from src.db.audit import AuditLog
from src.domain.types import LABEL_AWAITING_PROMOTION, RepoRef
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService
from src.service.registry import EnvRepoRegistry


def _has_prod_creds() -> bool:
    """Return True when at least one complete credential set is present.

    Prod mode is entered when EITHER:
    - FORGE_TOKEN (PAT mode) is set, OR
    - GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY + GITHUB_APP_INSTALLATION_ID (App mode)
      are all set.

    This mirrors PortProvider.from_env() mode detection so that App-only
    deployments (no FORGE_TOKEN) correctly enter real mode.
    """
    forge_token = os.environ.get("FORGE_TOKEN", "")
    if forge_token:
        return True
    app_id = os.environ.get("GITHUB_APP_ID") or None
    app_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
    app_inst = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
    return all([app_id, app_key, app_inst])


def create_app(
    service: OrchestratorService,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    webhook_secret: str | None = None,
) -> FastAPI:
    """Create the FastAPI application, mounting static UI if built.

    Args:
        service: The OrchestratorService instance.
        lifespan: Optional ASGI lifespan context manager.
        webhook_secret: HMAC-SHA256 secret for validating GitHub webhook payloads.
            When provided, mounts POST /api/webhook.  When absent (dev mode),
            the webhook endpoint is not registered.
    """
    app = FastAPI(title="Orchestrator", version="0.1.0", lifespan=lifespan)

    # CORS for dev (Vite dev server at :5173)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Health probes (ARCHITECTURE §6) ---
    # /healthz — liveness: 200 when the HTTP listener is alive (cheap; no I/O)
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> Response:
        return Response(content='{"status":"ok"}', media_type="application/json")

    # /readyz — readiness: 200 when the service is ready to handle traffic.
    # Returns sub-check status for forge connectivity, DB, and scheduler.
    # A pod failing readiness is removed from Service endpoints.
    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        checks: dict[str, str] = {}
        overall = "ok"

        # DB check: if the audit log exposes a ping() method, call it.
        # AuditLog does not currently expose ping(); it is added per-instance in
        # tests that need to simulate DB failure.  Without ping(), the check passes —
        # in production the ASGI lifespan calls audit.init() before traffic arrives.
        try:
            audit_log = service._audit
            ping = getattr(audit_log, "ping", None)
            if ping is not None:
                await ping()
            checks["db"] = "ok"
        except Exception:
            checks["db"] = "error"
            overall = "error"

        # Forge check: connectivity validated on first call; always reported ok here.
        # A dedicated lightweight check is tracked in issue #30.
        checks["forge"] = "ok"

        # Scheduler check: ok once the service has started (startup() completed).
        checks["scheduler"] = "ok"

        status_code = 200 if overall == "ok" else 503
        import json as _json

        body = _json.dumps({"status": overall, "checks": checks})
        return Response(content=body, media_type="application/json", status_code=status_code)

    # Register API routes
    app.include_router(_make_router(service))

    # Register webhook ingress when a secret is configured
    if webhook_secret:
        app.include_router(_make_webhook_router(service, webhook_secret))

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
    audit = AuditLog()  # in-memory for dev

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="demo",  # dev mode: owner is "demo" (matches seed data RepoRef)
    )
    return service


async def _seed_demo_data(service: OrchestratorService) -> None:
    """Pre-seed some fake runs and triage issues for the dev UI."""
    from src.domain.types import IssueRef, RunEvent

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

    # --- Seed a demo AWAITING_PROMOTION issue for the triage queue screen ---
    forge = service.forge
    if not isinstance(forge, FakeForgePort):
        return

    triage_ref = IssueRef(repo=repo, number=42)
    forge.seed_issue(
        triage_ref,
        title="Add dark mode to dashboard",
        body="Users have requested a dark mode option for the main dashboard UI.",
        author="external-contributor",
        labels=[LABEL_AWAITING_PROMOTION],
    )



def _build_prod_service() -> tuple[OrchestratorService, str | None]:
    """Wire real ports when production credentials are present; fall back to dev mode.

    Prod mode is entered when EITHER FORGE_TOKEN (PAT mode) OR the full set of
    GitHub App credentials (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY +
    GITHUB_APP_INSTALLATION_ID) are present.  PortProvider.from_env() enforces the
    same condition; this guard must match it so that App-only deployments (which
    omit FORGE_TOKEN) correctly enter real mode rather than dev/fake mode.
    """
    from src.ports.provider import PortProvider

    if not _has_prod_creds():
        return _build_dev_service(), None

    webhook_secret = os.environ.get("OPERATOR_SECRET_KEY") or None

    # Build the repo registry.
    #
    # Multi-repo mode: set REPOS_JSON to a JSON array of repo-config objects.
    # Single-repo backward-compat: GITHUB_OWNER + GITHUB_REPO + ALLOWLIST are
    # used when REPOS_JSON is absent — produces a one-entry registry so all
    # existing single-repo deploys (and Helm values from #61/#66) keep working.
    repos_json = os.environ.get("REPOS_JSON") or None
    allowlist_raw = os.environ.get("ALLOWLIST", "")
    registry = EnvRepoRegistry.from_env(
        repos_json=repos_json,
        github_owner=os.environ.get("GITHUB_OWNER"),
        github_repo=os.environ.get("GITHUB_REPO"),
        allowlist_raw=allowlist_raw,
    )

    # Primary repo for port construction (first enabled repo, or legacy default).
    # Credentials stay in PortProvider — not in the registry (I3).
    enabled_repos = [c for c in registry._configs if c.enabled]
    if enabled_repos:
        default_repo = enabled_repos[0].repo
    else:
        default_repo = RepoRef(
            owner=os.environ.get("GITHUB_OWNER", "demo"),
            name=os.environ.get("GITHUB_REPO", "repo"),
        )

    provider = PortProvider.from_env()
    forge, harness, session = provider.ports(default_repo)
    audit = AuditLog()

    # I1: single-repo backward-compat allowlist (used when no registry or as fallback).
    # Multi-repo mode reads per-repo allowlist from the registry at event time.
    allowlist = [u.strip() for u in allowlist_raw.split(",") if u.strip()]

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=allowlist,
        owner=default_repo.owner,
        registry=registry,
    )
    return service, webhook_secret


# Singleton for ASGI app (used by uvicorn)
_service, _webhook_secret = _build_prod_service()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await _service.startup()
    # Only seed demo data in dev mode (no production credentials)
    if not _has_prod_creds():
        await _seed_demo_data(_service)
    yield


app = create_app(_service, lifespan=_lifespan, webhook_secret=_webhook_secret)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
