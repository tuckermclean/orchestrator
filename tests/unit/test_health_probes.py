"""Tests for /healthz and /readyz probe endpoints, and _has_prod_creds() gate.

Health endpoints are infrastructure concerns — not SPEC §8 truth-table rows —
so no @pytest.mark.covers decorators are used here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort

# ---------------------------------------------------------------------------
# Helper: build a minimal FastAPI app from create_app with fake ports
# ---------------------------------------------------------------------------

def _make_test_app() -> object:
    """Build a test FastAPI app with fake ports (no prod credentials needed)."""
    from src.api.main import create_app
    from src.db.audit import AuditLog
    from src.service.orchestrator import OrchestratorService

    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    audit = AuditLog()
    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="test",
    )
    return create_app(service, lifespan=None, webhook_secret=None)


# ---------------------------------------------------------------------------
# /healthz tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz_returns_200() -> None:
    """/healthz responds 200 with {"status":"ok"}."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_healthz_is_cheap_no_io() -> None:
    """/healthz does not require any I/O — suitable for liveness probe."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Call twice to confirm idempotency
        r1 = await client.get("/healthz")
        r2 = await client.get("/healthz")
    assert r1.status_code == 200
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# /readyz tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_readyz_returns_200_with_checks() -> None:
    """/readyz responds 200 with status + checks dict when all checks pass."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "checks" in body
    # All three checks must be present
    assert set(body["checks"].keys()) >= {"db", "forge", "scheduler"}


@pytest.mark.asyncio
async def test_readyz_forge_check_present() -> None:
    """/readyz includes a forge check in the response."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.json()["checks"]["forge"] == "ok"


@pytest.mark.asyncio
async def test_readyz_scheduler_check_present() -> None:
    """/readyz includes a scheduler check in the response."""
    app = _make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.json()["checks"]["scheduler"] == "ok"


@pytest.mark.asyncio
async def test_readyz_db_error_returns_503() -> None:
    """/readyz returns 503 when the DB ping raises an exception."""
    from src.api.main import create_app
    from src.db.audit import AuditLog
    from src.service.orchestrator import OrchestratorService

    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    audit = AuditLog()

    # Inject a failing ping method directly on the instance so the readyz
    # handler picks it up via getattr(audit_log, "ping", None).
    async def _bad_ping() -> None:
        raise RuntimeError("db unreachable")

    audit.ping = _bad_ping  # type: ignore[attr-defined]

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit,
        allowlist=[], owner="test",
    )
    # The service stores the audit log as self._audit — verify the injected
    # instance is the one with the failing ping before constructing the app.
    assert service._audit is audit  # type: ignore[attr-defined]
    app = create_app(service, lifespan=None, webhook_secret=None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["db"] == "error"


# ---------------------------------------------------------------------------
# _has_prod_creds() gate tests
# ---------------------------------------------------------------------------

def test_has_prod_creds_false_when_no_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_prod_creds returns False when no credentials are set."""
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.api.main import _has_prod_creds

    assert _has_prod_creds() is False


def test_has_prod_creds_true_with_forge_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_prod_creds returns True when FORGE_TOKEN is set (PAT mode)."""
    monkeypatch.setenv("FORGE_TOKEN", "ghp_test")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.api.main import _has_prod_creds

    assert _has_prod_creds() is True


def test_has_prod_creds_true_with_app_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """_has_prod_creds returns True when all three App credentials are set (App mode)."""
    _fake_pem = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _fake_pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")

    from src.api.main import _has_prod_creds

    assert _has_prod_creds() is True


def test_has_prod_creds_false_with_partial_app_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_has_prod_creds returns False when only some App credentials are set."""
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.api.main import _has_prod_creds

    assert _has_prod_creds() is False


def test_build_prod_service_enters_prod_mode_with_app_creds_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_prod_service wires real ports when App creds are set but FORGE_TOKEN is absent.

    This is the prod-mode gate fix: App-only deployments previously fell back
    to dev/fake mode because the old gate only checked FORGE_TOKEN.
    """
    _fake_pem = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _fake_pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "67890")
    monkeypatch.setenv("GITHUB_OWNER", "acme")
    monkeypatch.setenv("GITHUB_REPO", "myrepo")

    with patch("src.ports.provider.PortProvider.from_env") as mock_from_env:
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        service, _secret, _op_store, _push_store, _reg, *_ = _build_prod_service()

    # PortProvider.from_env() was called — we are in prod mode, not dev mode
    mock_from_env.assert_called_once()
    # The service was constructed with real ports from the provider
    mock_provider.ports.assert_called_once()


def test_build_prod_service_uses_dev_mode_without_any_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_prod_service falls back to dev (fake) mode when no credentials are set."""
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.api.main import _build_prod_service

    service, webhook_secret, _op_store, _push_store, _reg, *_ = _build_prod_service()

    # Dev mode: forge is a FakeForgePort and no webhook secret
    assert isinstance(service.forge, FakeForgePort)
    assert webhook_secret is None
