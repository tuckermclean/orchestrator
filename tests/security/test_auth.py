"""JWT authentication security tests.

Verifies:
1. Unauthenticated request to a protected route → 401
2. Expired JWT → 401
3. POST /api/webhook bypasses JWT auth (still works with valid HMAC)
4. Health probes /healthz and /readyz bypass auth

These tests are NOT coverage-mapped to SPEC §8 truth-table rows — auth
enforcement is infrastructure, not a state-machine decision function.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api.auth import hash_password, issue_token
from src.api.main import create_app
from src.db.audit import AuditLog
from src.db.operator_store import FakeOperatorStore
from src.db.push_store import FakePushStore
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService

_TEST_SECRET = "test-secret-for-auth-tests"
_TEST_OPERATOR_SECRET = "test-operator-secret-key-padded-to-32b"


def _make_service() -> OrchestratorService:
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=AuditLog(),
        allowlist=[],
    )


def _make_operator_store() -> FakeOperatorStore:
    store = FakeOperatorStore()
    store.seed("admin", hash_password("password123"))
    return store


def _make_client(*, with_webhook: bool = False) -> TestClient:
    service = _make_service()
    op_store = _make_operator_store()
    push_store = FakePushStore()
    app = create_app(
        service,
        webhook_secret=_TEST_SECRET if with_webhook else None,
        operator_store=op_store,
        push_store=push_store,
    )
    return TestClient(app, raise_server_exceptions=True)


def _valid_token(operator_id: str = "admin") -> str:
    """Issue a valid JWT for tests (uses the test operator secret key)."""
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        return issue_token(operator_id)


def _expired_token(operator_id: str = "admin") -> str:
    """Craft an expired JWT (exp in the past)."""
    import jwt

    now = int(time.time())
    payload = {
        "sub": operator_id,
        "iat": now - 7200,  # issued 2 hours ago
        "exp": now - 3600,  # expired 1 hour ago
    }
    return jwt.encode(payload, _TEST_OPERATOR_SECRET, algorithm="HS256")


# ---------------------------------------------------------------------------
# 1. Unauthenticated request → 401
# ---------------------------------------------------------------------------


def test_unauthenticated_request_returns_401() -> None:
    """GET /api/status without a token returns 401."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/status")
    assert resp.status_code == 401


def test_unauthenticated_runs_returns_401() -> None:
    """GET /api/runs without a token returns 401."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/runs")
    assert resp.status_code == 401


def test_unauthenticated_triage_returns_401() -> None:
    """GET /api/triage without a token returns 401."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/triage")
    assert resp.status_code == 401


def test_unauthenticated_operators_returns_401() -> None:
    """GET /api/operators without a token returns 401."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/api/operators")
    assert resp.status_code == 401


def test_unauthenticated_push_subscribe_returns_401() -> None:
    """POST /api/push/subscribe without a token returns 401."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/push/subscribe",
            json={
                "endpoint": "https://example.com/push/test",
                "keys": {"p256dh": "abc", "auth": "def"},
            },
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. Expired JWT → 401
# ---------------------------------------------------------------------------


def test_expired_jwt_returns_401() -> None:
    """GET /api/status with an expired JWT returns 401."""
    client = _make_client()
    expired = _expired_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 401


def test_expired_jwt_on_runs_returns_401() -> None:
    """GET /api/runs with an expired JWT returns 401."""
    client = _make_client()
    expired = _expired_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(
            "/api/runs",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 401


def test_valid_jwt_allows_access() -> None:
    """GET /api/runs with a valid JWT returns 200."""
    client = _make_client()
    token = _valid_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(
            "/api/runs",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. Webhook bypasses JWT auth (HMAC still required)
# ---------------------------------------------------------------------------


def _sign_webhook(body: bytes, secret: str = _TEST_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_bypasses_jwt_no_token_valid_hmac() -> None:
    """POST /api/webhook with valid HMAC but NO JWT returns 200 (not 401)."""
    client = _make_client(with_webhook=True)
    payload = json.dumps(
        {"ref": "refs/heads/main", "repository": {"name": "repo", "owner": {"login": "acme"}}}
    ).encode()
    sig = _sign_webhook(payload)
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "auth-test-delivery-001",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


def test_webhook_bypasses_jwt_expired_token_valid_hmac() -> None:
    """POST /api/webhook with valid HMAC and expired JWT still returns 200.

    The webhook endpoint authenticates via HMAC only — the JWT state is irrelevant.
    """
    client = _make_client(with_webhook=True)
    payload = json.dumps(
        {"ref": "refs/heads/feat", "repository": {"name": "repo", "owner": {"login": "acme"}}}
    ).encode()
    sig = _sign_webhook(payload)
    expired = _expired_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "auth-test-delivery-002",
                "Authorization": f"Bearer {expired}",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200


def test_webhook_invalid_hmac_still_rejected() -> None:
    """POST /api/webhook with valid JWT but wrong HMAC returns 403.

    Auth bypass is not a free pass — HMAC is still enforced.
    """
    client = _make_client(with_webhook=True)
    payload = b'{"ref": "refs/heads/main"}'
    token = _valid_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/webhook",
            content=payload,
            headers={
                "X-Hub-Signature-256": "sha256=deadbeefdeadbeefdeadbeef",
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "auth-test-delivery-003",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. Health probes bypass auth
# ---------------------------------------------------------------------------


def test_healthz_no_auth_returns_200() -> None:
    """/healthz requires no authentication."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/healthz")
    assert resp.status_code == 200


def test_readyz_no_auth_returns_200() -> None:
    """/readyz requires no authentication."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get("/readyz")
    assert resp.status_code == 200


def test_healthz_expired_jwt_still_returns_200() -> None:
    """/healthz returns 200 even when an expired JWT is supplied."""
    client = _make_client()
    expired = _expired_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(
            "/healthz",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 200


def test_readyz_expired_jwt_still_returns_200() -> None:
    """/readyz returns 200 even when an expired JWT is supplied."""
    client = _make_client()
    expired = _expired_token()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(
            "/readyz",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Login endpoint bypasses auth (it IS the auth endpoint)
# ---------------------------------------------------------------------------


def test_login_endpoint_no_token_required() -> None:
    """POST /api/auth requires no pre-existing token."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/auth",
            json={"username": "admin", "password": "password123"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body


def test_login_returns_401_for_wrong_password() -> None:
    """POST /api/auth returns 401 for incorrect credentials."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/auth",
            json={"username": "admin", "password": "wrongpassword"},
        )
    assert resp.status_code == 401
    assert "Invalid credentials" in resp.json().get("detail", "")


def test_login_returns_401_for_unknown_user() -> None:
    """POST /api/auth returns 401 for unknown username."""
    client = _make_client()
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(
            "/api/auth",
            json={"username": "nonexistent", "password": "password123"},
        )
    assert resp.status_code == 401
    # Error message must NOT distinguish unknown username from wrong password (WEBUI.md §5.7)
    assert "Invalid credentials" in resp.json().get("detail", "")
