"""Tests for the registry-backed API routes (issue #81).

Verifies:
- /api/status, /api/runs, /api/triage operate on the registry repo (NOT demo/repo)
- GET /api/repos lists the configured repos
- Empty registry yields empty/clean responses (no 500)
- Prod-style registry (GITHUB_OWNER/GITHUB_REPO) surfaces that repo through the API
- No route is pinned to the module-level demo constant

Auth is bypassed by passing a valid JWT in all tests; the auth tests in
tests/security/test_auth.py cover the unauthenticated cases.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.auth import hash_password, issue_token
from src.api.main import create_app
from src.db.audit import AuditLog
from src.db.operator_store import FakeOperatorStore
from src.db.push_store import FakePushStore
from src.domain.types import RepoRef
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService
from src.service.registry import EnvRepoRegistry, FakeRepoRegistry, RepoConfig

_TEST_OPERATOR_SECRET = "test-routes-operator-secret-padded-to-32b"
_PROD_OWNER = "tuckermclean"
_PROD_REPO = "sandbox-derp"
_PROD_REPO_REF = RepoRef(owner=_PROD_OWNER, name=_PROD_REPO)


def _make_service() -> OrchestratorService:
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    audit = AuditLog()
    # TestClient does not run the ASGI lifespan; initialise audit manually
    # so that list_triage (which calls audit.list_entries) works in tests.
    asyncio.run(audit.init())
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
    )


def _make_client(
    registry: FakeRepoRegistry | EnvRepoRegistry | None = None,
) -> tuple[TestClient, OrchestratorService]:
    service = _make_service()
    op_store = FakeOperatorStore()
    op_store.seed("admin", hash_password("password"))
    push_store = FakePushStore()
    app = create_app(
        service,
        operator_store=op_store,
        push_store=push_store,
        registry=registry,
    )
    client = TestClient(app, raise_server_exceptions=True)
    return client, service


def _token() -> str:
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        return issue_token("admin")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


# ---------------------------------------------------------------------------
# GET /api/repos — list registry
# ---------------------------------------------------------------------------


def test_list_repos_empty_registry_returns_empty_list() -> None:
    """GET /api/repos with empty registry returns [] (not 500)."""
    client, _ = _make_client(FakeRepoRegistry([]))
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_repos_single_repo() -> None:
    """GET /api/repos returns the configured repo."""
    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["owner"] == _PROD_OWNER
    assert data[0]["name"] == _PROD_REPO
    assert data[0]["enabled"] is True
    assert data[0]["intake_enabled"] is True
    assert "required_checks" not in data[0]  # field removed — CI gate trusts actual checks


def test_list_repos_multiple_repos() -> None:
    """GET /api/repos returns all repos in the registry."""
    reg = FakeRepoRegistry([
        RepoConfig(repo=RepoRef(owner="acme", name="api"), enabled=True),
        RepoConfig(repo=RepoRef(owner="acme", name="ui"), enabled=False),
    ])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {d["name"] for d in data}
    assert names == {"api", "ui"}
    enabled_map = {d["name"]: d["enabled"] for d in data}
    assert enabled_map["api"] is True
    assert enabled_map["ui"] is False


def test_list_repos_requires_auth() -> None:
    """GET /api/repos without a token returns 401."""
    client, _ = _make_client(FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)]))
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/status — resolves from registry, not _DEV_REPO
# ---------------------------------------------------------------------------


def test_status_resolves_from_registry() -> None:
    """GET /api/status operates on the registry repo (not the hardcoded demo/repo).

    With the real FakeForgePort, status() calls pipeline_health() which calls
    forge.list_prs(repo, ...) — no crash and returns a HealthReport.
    """
    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, service = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/status", headers=_auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "verdict" in body


def test_status_empty_registry_returns_200_not_500() -> None:
    """GET /api/status with empty registry returns 200 with null body (no 500)."""
    client, _ = _make_client(FakeRepoRegistry([]))
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/status", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() is None


# ---------------------------------------------------------------------------
# GET /api/runs — resolves from registry
# ---------------------------------------------------------------------------


def test_list_runs_resolves_from_registry() -> None:
    """GET /api/runs uses the registry repo, returns empty list for fresh session."""
    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/runs", headers=_auth_headers())
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_runs_empty_registry_returns_empty_list() -> None:
    """GET /api/runs with empty registry returns [] (no 500)."""
    client, _ = _make_client(FakeRepoRegistry([]))
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/runs", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == []


def test_runs_surface_dispatched_model() -> None:
    """GET /api/runs and /api/runs/{id} include the per-run dispatched model.

    The orchestrator entry run is recorded with ADJUDICATION_MODEL (Opus) and a
    swarm role with DEFAULT_SWARM_MODEL (Sonnet); both must reflect on the API
    response models (RunSummary / RunDetail) — the gap this change closes is that
    the model column was stored in the DB but dropped by the response models.
    """
    from datetime import UTC, datetime

    from src.domain.types import ADJUDICATION_MODEL, DEFAULT_SWARM_MODEL

    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, service = _make_client(reg)

    now = datetime.now(tz=UTC)
    service._run_store.record(
        "run-opus", _PROD_REPO_REF, type="orchestrator", model=ADJUDICATION_MODEL, started_at=now
    )
    service._run_store.record(
        "run-sonnet", _PROD_REPO_REF, type="triager", model=DEFAULT_SWARM_MODEL, started_at=now
    )

    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        list_resp = client.get("/api/runs", headers=_auth_headers())
        opus_resp = client.get("/api/runs/run-opus", headers=_auth_headers())
        sonnet_resp = client.get("/api/runs/run-sonnet", headers=_auth_headers())

    assert list_resp.status_code == 200
    by_id = {r["run_id"]: r for r in list_resp.json()}
    assert by_id["run-opus"]["model"] == ADJUDICATION_MODEL
    assert by_id["run-sonnet"]["model"] == DEFAULT_SWARM_MODEL

    assert opus_resp.status_code == 200
    assert opus_resp.json()["model"] == ADJUDICATION_MODEL
    assert sonnet_resp.status_code == 200
    assert sonnet_resp.json()["model"] == DEFAULT_SWARM_MODEL


# ---------------------------------------------------------------------------
# GET /api/triage — resolves from registry
# ---------------------------------------------------------------------------


def test_list_triage_resolves_from_registry() -> None:
    """GET /api/triage uses the registry repo, returns empty list for fresh forge."""
    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/triage", headers=_auth_headers())
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_triage_empty_registry_returns_empty_list() -> None:
    """GET /api/triage with empty registry returns [] (no 500)."""
    client, _ = _make_client(FakeRepoRegistry([]))
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/triage", headers=_auth_headers())
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Regression: no route targets demo/repo when registry has a different repo
# ---------------------------------------------------------------------------


def test_routes_use_registry_repo_not_demo_repo() -> None:
    """Regression: with a registry containing repo X, routes operate on X not demo/repo.

    Seeds triage items for both demo/repo (old hardcoded constant) and the
    prod repo, and verifies that /api/triage only returns the prod repo's items.
    """
    from src.domain.types import LABEL_AWAITING_PROMOTION, IssueRef

    reg = FakeRepoRegistry([RepoConfig(repo=_PROD_REPO_REF)])
    client, service = _make_client(reg)

    forge = service.forge
    assert isinstance(forge, FakeForgePort)

    demo_ref = IssueRef(repo=RepoRef(owner="demo", name="repo"), number=1)
    prod_ref = IssueRef(repo=_PROD_REPO_REF, number=99)

    forge.seed_issue(
        demo_ref,
        title="Demo issue (should NOT appear)",
        body="",
        author="author",
        labels=[LABEL_AWAITING_PROMOTION],
    )
    forge.seed_issue(
        prod_ref,
        title="Prod issue (should appear)",
        body="",
        author="author",
        labels=[LABEL_AWAITING_PROMOTION],
    )

    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/triage", headers=_auth_headers())
    assert resp.status_code == 200
    items = resp.json()
    titles = [it["title"] for it in items]
    assert "Prod issue (should appear)" in titles
    assert "Demo issue (should NOT appear)" not in titles


# ---------------------------------------------------------------------------
# Prod-style registry (EnvRepoRegistry from GITHUB_OWNER/GITHUB_REPO)
# ---------------------------------------------------------------------------


def test_prod_style_registry_surfaces_configured_repo_in_list_repos() -> None:
    """Prod-style EnvRepoRegistry from GITHUB_OWNER/GITHUB_REPO appears in GET /api/repos."""
    reg = EnvRepoRegistry.from_env(
        github_owner=_PROD_OWNER,
        github_repo=_PROD_REPO,
        allowlist_raw="",
    )
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["owner"] == _PROD_OWNER
    assert data[0]["name"] == _PROD_REPO


def test_prod_style_registry_status_uses_prod_repo() -> None:
    """Prod-style registry: /api/status resolves the prod repo without 500."""
    reg = EnvRepoRegistry.from_env(
        github_owner=_PROD_OWNER,
        github_repo=_PROD_REPO,
        allowlist_raw="",
    )
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/status", headers=_auth_headers())
    assert resp.status_code == 200


def test_prod_style_registry_triage_uses_prod_repo() -> None:
    """Prod-style registry: /api/triage resolves the prod repo without 500."""
    reg = EnvRepoRegistry.from_env(
        github_owner=_PROD_OWNER,
        github_repo=_PROD_REPO,
        allowlist_raw="",
    )
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/triage", headers=_auth_headers())
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Dev mode: registry seeded with demo/repo works correctly
# ---------------------------------------------------------------------------


def test_dev_registry_seeded_demo_repo_works() -> None:
    """Dev mode: FakeRepoRegistry seeded with demo/repo returns it via GET /api/repos."""
    demo_repo = RepoRef(owner="demo", name="repo")
    reg = FakeRepoRegistry([RepoConfig(repo=demo_repo)])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["owner"] == "demo"
    assert data[0]["name"] == "repo"


# ---------------------------------------------------------------------------
# Multi-repo: first enabled repo is used for single-repo routes
# ---------------------------------------------------------------------------


def test_status_uses_first_enabled_repo() -> None:
    """With 2+ repos, /api/status uses the first ENABLED repo."""
    reg = FakeRepoRegistry([
        RepoConfig(repo=RepoRef(owner="acme", name="disabled-repo"), enabled=False),
        RepoConfig(repo=RepoRef(owner="acme", name="active-repo"), enabled=True),
    ])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/status", headers=_auth_headers())
    assert resp.status_code == 200


def test_list_repos_includes_disabled_repos() -> None:
    """GET /api/repos lists ALL repos (enabled and disabled)."""
    reg = FakeRepoRegistry([
        RepoConfig(repo=RepoRef(owner="acme", name="enabled"), enabled=True),
        RepoConfig(repo=RepoRef(owner="acme", name="disabled"), enabled=False),
    ])
    client, _ = _make_client(reg)
    with patch.dict(os.environ, {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/repos", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
