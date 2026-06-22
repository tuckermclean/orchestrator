"""Webhook ingress security tests.

Verifies HMAC-SHA256 validation and delivery-ID deduplication.
These tests run WITHOUT any credentials — the webhook secret is a known
test value and signatures are computed in the test itself.

The valid-payload tests use 'push' events (no-op in the routing table) so
the engine never touches the audit log or forge port.  The HMAC tests use
arbitrary payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.api.main import create_app
from src.db.audit import AuditLog
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService

_TEST_SECRET = "test-webhook-secret-abc123"


def _sign(body: bytes, secret: str = _TEST_SECRET) -> str:
    """Compute the GitHub-style HMAC-SHA256 signature for a body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


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


@pytest.fixture
def client() -> TestClient:
    service = _make_service()
    app = create_app(service, webhook_secret=_TEST_SECRET)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def service_and_client() -> tuple[OrchestratorService, TestClient]:
    service = _make_service()
    app = create_app(service, webhook_secret=_TEST_SECRET)
    return service, TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Valid payload — use 'push' events (no-op in engine routing table)
# ---------------------------------------------------------------------------


def test_webhook_valid_signature_returns_200(client: TestClient) -> None:
    """HMAC-valid push event returns 200."""
    payload = json.dumps(
        {
            "ref": "refs/heads/main",
            "repository": {"name": "repo", "owner": {"login": "acme"}},
        }
    ).encode()
    sig = _sign(payload)
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-001",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200


def test_webhook_valid_signature_body_handled(client: TestClient) -> None:
    """HMAC-valid event returns handled=true in response body."""
    payload = json.dumps(
        {
            "ref": "refs/heads/feat/x",
            "repository": {"name": "repo", "owner": {"login": "acme"}},
        }
    ).encode()
    sig = _sign(payload)
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-002",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("handled") is True


# ---------------------------------------------------------------------------
# Invalid / missing signature → 403
# ---------------------------------------------------------------------------


def test_webhook_missing_signature_returns_403(client: TestClient) -> None:
    """Request with no X-Hub-Signature-256 header is rejected with 403."""
    payload = b'{"ref": "refs/heads/main"}'
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-003",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


def test_webhook_wrong_signature_returns_403(client: TestClient) -> None:
    """Request with an invalid signature value is rejected with 403."""
    payload = b'{"ref": "refs/heads/main"}'
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeefdeadbeefdeadbeefdeadbeef",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-004",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


def test_webhook_tampered_body_returns_403(client: TestClient) -> None:
    """Signature was computed on original body; tampered body → 403."""
    original = b'{"ref": "refs/heads/main"}'
    sig = _sign(original)
    tampered = b'{"ref": "refs/heads/evil"}'
    resp = client.post(
        "/api/webhook",
        content=tampered,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-005",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


def test_webhook_wrong_secret_returns_403(client: TestClient) -> None:
    """Signature computed with the wrong secret is rejected with 403."""
    payload = b'{"ref": "refs/heads/main"}'
    sig = _sign(payload, secret="wrong-secret")
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-006",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


def test_webhook_empty_signature_value_returns_403(client: TestClient) -> None:
    """Empty signature header value is rejected with 403."""
    payload = b'{"ref": "refs/heads/main"}'
    resp = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": "",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-007",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Delivery-ID deduplication — use push events (no-op engine path)
# ---------------------------------------------------------------------------


def test_webhook_replayed_delivery_id_deduped(
    service_and_client: tuple[OrchestratorService, TestClient],
) -> None:
    """Second request with the same X-GitHub-Delivery returns 200 but handled=false."""
    _, client = service_and_client
    payload = json.dumps(
        {
            "ref": "refs/heads/feat/dup",
            "repository": {"name": "repo", "owner": {"login": "acme"}},
        }
    ).encode()
    sig = _sign(payload)
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-replay-001",
        "Content-Type": "application/json",
    }

    # First delivery — handled
    resp1 = client.post("/api/webhook", content=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json().get("handled") is True

    # Second delivery with same ID — deduped, not re-processed
    resp2 = client.post("/api/webhook", content=payload, headers=headers)
    assert resp2.status_code == 200
    data = resp2.json()
    assert data.get("handled") is False
    assert data.get("reason") == "duplicate_delivery_id"


def test_webhook_different_delivery_ids_both_handled(
    service_and_client: tuple[OrchestratorService, TestClient],
) -> None:
    """Different delivery IDs on the same payload are each processed."""
    _, client = service_and_client
    payload = json.dumps(
        {
            "ref": "refs/heads/feat/new",
            "repository": {"name": "repo", "owner": {"login": "acme"}},
        }
    ).encode()
    sig = _sign(payload)

    resp1 = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "unique-id-A",
            "Content-Type": "application/json",
        },
    )
    resp2 = client.post(
        "/api/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "unique-id-B",
            "Content-Type": "application/json",
        },
    )
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json().get("handled") is True
    assert resp2.json().get("handled") is True


def test_webhook_no_delivery_id_always_handled(client: TestClient) -> None:
    """Requests without X-GitHub-Delivery are never deduped."""
    payload = json.dumps(
        {
            "ref": "refs/heads/feat/z",
            "repository": {"name": "repo", "owner": {"login": "acme"}},
        }
    ).encode()
    sig = _sign(payload)
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
    }

    resp1 = client.post("/api/webhook", content=payload, headers=headers)
    resp2 = client.post("/api/webhook", content=payload, headers=headers)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Both handled (no delivery ID to dedup on)
    assert resp1.json().get("handled") is True
    assert resp2.json().get("handled") is True
