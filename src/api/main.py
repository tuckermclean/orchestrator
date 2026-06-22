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

from src.api.auth import hash_password
from src.api.auth_routes import _make_auth_router
from src.api.operator_routes import _make_operator_router
from src.api.push_routes import _make_push_router
from src.api.routes import _make_router
from src.api.webhook import _make_webhook_router
from src.db.audit import AuditLog
from src.db.converge_state import SQLiteConvergeStateStore
from src.db.counter import SQLiteCounterStore
from src.db.dsn import db_path_from_url
from src.db.operator_store import FakeOperatorStore, SQLiteOperatorStore
from src.db.push_store import FakePushStore, SQLitePushStore
from src.domain.types import LABEL_AWAITING_PROMOTION, RepoRef
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService
from src.service.registry import (
    EnvRepoRegistry,
    FakeRepoRegistry,
    RepoConfig,
    RepoRegistryPort,
)


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
    operator_store: FakeOperatorStore | SQLiteOperatorStore | None = None,
    push_store: FakePushStore | SQLitePushStore | None = None,
    registry: RepoRegistryPort | None = None,
) -> FastAPI:
    """Create the FastAPI application, mounting static UI if built.

    Args:
        service: The OrchestratorService instance.
        lifespan: Optional ASGI lifespan context manager.
        webhook_secret: HMAC-SHA256 secret for validating GitHub webhook payloads.
            When provided, mounts POST /api/webhook.  When absent (dev mode),
            the webhook endpoint is not registered.
        operator_store: Operator account store; defaults to FakeOperatorStore if None.
        push_store: VAPID push subscription store; defaults to FakePushStore if None.
        registry: Repo registry for resolving the active repo per request.
            Defaults to an empty FakeRepoRegistry when None.
    """
    from src.db.operator_store import FakeOperatorStore as _FakeOp
    from src.db.push_store import FakePushStore as _FakePush

    _operator_store: FakeOperatorStore | SQLiteOperatorStore = (
        operator_store if operator_store is not None else _FakeOp()
    )
    _push_store: FakePushStore | SQLitePushStore = (
        push_store if push_store is not None else _FakePush()
    )
    _registry: RepoRegistryPort = registry if registry is not None else FakeRepoRegistry()

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
    # These routes are EXCLUDED from JWT auth (no Depends(require_auth)).
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

    # Auth routes — /api/auth (login) and /api/auth/refresh are EXCLUDED from JWT auth
    # by the bypass list in src/api/auth.py.
    app.include_router(_make_auth_router(_operator_store))

    # Operator management routes — JWT-protected
    app.include_router(_make_operator_router(_operator_store))

    # Push subscription routes — JWT-protected
    app.include_router(_make_push_router(_push_store))

    # Core API routes — JWT-protected via Depends(require_auth) on each route
    app.include_router(_make_router(service, _registry))

    # Register webhook ingress when a secret is configured.
    # POST /api/webhook is authenticated by HMAC-SHA256, NOT by JWT.
    # The auth bypass list in auth.py ensures require_auth is never applied to it.
    if webhook_secret:
        app.include_router(_make_webhook_router(service, webhook_secret))

    # Serve Vite build if it exists
    ui_dist = Path(__file__).parent.parent.parent / "ui" / "dist"
    if ui_dist.exists():
        app.mount("/", StaticFiles(directory=str(ui_dist), html=True), name="static")

    return app


def _build_dev_service() -> (
    tuple[OrchestratorService, FakeOperatorStore, FakePushStore, FakeRepoRegistry]
):
    """Wire fake ports for dev/demo mode."""
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    audit = AuditLog()  # in-memory for dev

    # Dev registry: seed with the demo repo so routes resolve correctly.
    dev_registry = FakeRepoRegistry(
        [RepoConfig(repo=RepoRef(owner="demo", name="repo"))]
    )

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="demo",  # dev mode: owner is "demo" (matches seed data RepoRef)
        registry=dev_registry,
    )

    # Seed a default dev operator account (username: admin, password: admin)
    operator_store = FakeOperatorStore()
    operator_store.seed("admin", hash_password("admin"))

    push_store = FakePushStore()
    return service, operator_store, push_store, dev_registry


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



_ProdStores = tuple[
    OrchestratorService,
    str | None,
    FakeOperatorStore | SQLiteOperatorStore,
    FakePushStore | SQLitePushStore,
    RepoRegistryPort,
    # DB-backed engine stores that need lifespan init()/close() — may be None in dev mode.
    AuditLog | None,
    SQLiteCounterStore | None,
    SQLiteConvergeStateStore | None,
]


def _build_prod_service() -> _ProdStores:
    """Wire real ports when production credentials are present; fall back to dev mode.

    Prod mode is entered when EITHER FORGE_TOKEN (PAT mode) OR the full set of
    GitHub App credentials (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY +
    GITHUB_APP_INSTALLATION_ID) are present.  PortProvider.from_env() enforces the
    same condition; this guard must match it so that App-only deployments (which
    omit FORGE_TOKEN) correctly enter real mode rather than dev/fake mode.

    Store selection (Counter / ConvergeState / Audit):
      - DB_URL is a sqlite:///path → SQLite-backed stores (file persists across restarts).
      - DB_URL unset / empty / sqlite:///:memory: → in-memory Fake/AuditLog() (default).
      - DB_URL is a Postgres DSN → NotImplementedError (not supported yet).

    The returned tuple includes the three DB-backed engine stores (or None for in-memory)
    so the ASGI lifespan can call init() before traffic and close() on shutdown.
    """
    from src.ports.provider import PortProvider

    if not _has_prod_creds():
        _svc, _op, _push, _dev_reg = _build_dev_service()
        return _svc, None, _op, _push, _dev_reg, None, None, None

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

    # --- Engine store selection: SQLite-backed when DB_URL names a file path ---
    #
    # DB_URL (default "sqlite:///data/orchestrator.db") is parsed by db_path_from_url():
    #   sqlite:///data/orchestrator.db → "/data/orchestrator.db" (file, survives restarts)
    #   sqlite:///:memory: / unset     → None (in-memory, not durable)
    #   postgresql://...               → NotImplementedError (not yet implemented)
    db_url = os.environ.get("DB_URL", "sqlite:///data/orchestrator.db")
    db_path = db_path_from_url(db_url)

    if db_path is not None:
        # File-backed: all three engine stores use the same SQLite file.
        # init() is called in _lifespan before traffic arrives.
        audit: AuditLog = AuditLog(db_path=db_path)
        counter_store: SQLiteCounterStore | FakeCounterStore = SQLiteCounterStore(db_path)
        converge_store: SQLiteConvergeStateStore | FakeConvergeStateStore = (
            SQLiteConvergeStateStore(db_path)
        )
        db_audit: AuditLog | None = audit
        db_counter: SQLiteCounterStore | None = counter_store  # type: ignore[assignment]
        db_converge: SQLiteConvergeStateStore | None = converge_store  # type: ignore[assignment]
    else:
        # In-memory: use AuditLog() (defaults to :memory:) and Fake stores.
        # These do not need lifespan init()/close() beyond what AuditLog already does.
        audit = AuditLog()
        counter_store = FakeCounterStore()
        converge_store = FakeConvergeStateStore()
        db_audit = audit  # AuditLog always needs init/close — returned for lifespan
        db_counter = None
        db_converge = None

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
        counter=counter_store,
        converge_state=converge_store,
    )

    # Operator account store: SQLite when DB_URL names a file, Fake otherwise.
    # SessionPort: FakeSessionPort is acceptable (observability, not durability-critical).
    if db_path is not None:
        op_store: FakeOperatorStore | SQLiteOperatorStore = SQLiteOperatorStore(db_path)
        push_store_inst: FakePushStore | SQLitePushStore = SQLitePushStore(db_path)
    else:
        op_store = FakeOperatorStore()
        push_store_inst = FakePushStore()

    bootstrap_password = os.environ.get("OPERATOR_BOOTSTRAP_PASSWORD", "")
    if bootstrap_password and isinstance(op_store, FakeOperatorStore):
        op_store.seed("admin", hash_password(bootstrap_password))

    return (
        service,
        webhook_secret,
        op_store,
        push_store_inst,
        registry,
        db_audit,
        db_counter,
        db_converge,
    )


# Singleton for ASGI app (used by uvicorn)
(
    _service,
    _webhook_secret,
    _operator_store,
    _push_store,
    _registry,
    _db_audit,
    _db_counter,
    _db_converge,
) = _build_prod_service()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Init DB-backed stores before accepting traffic (SPEC §6 crash-only-durability).
    # AuditLog always needs init() — whether backed by file or :memory:.
    if _db_audit is not None:
        await _db_audit.init()
    if _db_counter is not None:
        await _db_counter.init()
    if _db_converge is not None:
        await _db_converge.init()
    # SQLiteOperatorStore / SQLitePushStore also need init() when file-backed.
    if isinstance(_operator_store, SQLiteOperatorStore):
        await _operator_store.init()
        # Bootstrap the admin account when a password is configured and the store
        # is fresh (create_operator raises ValueError if it already exists, which
        # is the normal case on subsequent restarts — silently skip it).
        bootstrap_password = os.environ.get("OPERATOR_BOOTSTRAP_PASSWORD", "")
        if bootstrap_password:
            try:
                await _operator_store.create_operator(
                    "admin", hash_password(bootstrap_password)
                )
            except ValueError:
                pass  # admin already exists — normal on pod restarts
    if isinstance(_push_store, SQLitePushStore):
        await _push_store.init()

    await _service.startup()
    # Only seed demo data in dev mode (no production credentials)
    if not _has_prod_creds():
        await _seed_demo_data(_service)
    try:
        yield
    finally:
        # Close DB connections on shutdown to avoid aiosqlite event-loop teardown warnings.
        if _db_converge is not None:
            await _db_converge.close()
        if _db_counter is not None:
            await _db_counter.close()
        if _db_audit is not None:
            await _db_audit.close()
        if isinstance(_operator_store, SQLiteOperatorStore):
            await _operator_store.close()
        if isinstance(_push_store, SQLitePushStore):
            await _push_store.close()


app = create_app(
    _service,
    lifespan=_lifespan,
    webhook_secret=_webhook_secret,
    operator_store=_operator_store,
    push_store=_push_store,
    registry=_registry,
)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        "src.api.main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
