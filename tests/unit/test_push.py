"""VAPID push subscription and notification tests.

Verifies:
1. Subscription stored in push store via POST /api/push/subscribe
2. Subscription removed via DELETE /api/push/subscribe
3. push.broadcast_push emits on escalation (simulated), promotion, and approval
4. broadcast_push returns sent count; is a no-op when push not configured

These tests use FakePushStore (no I/O) and do NOT make real VAPID HTTP calls
(push_enabled() is False unless PUSH_VAPID_* env vars are set, which they're not
in CI).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.api.auth import hash_password, issue_token  # noqa: E402
from src.api.push import broadcast_push, push_enabled, send_push  # noqa: E402
from src.db.push_store import FakePushStore  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_OPERATOR_SECRET = "test-operator-secret-push-padded-to-32b"


def _valid_token(operator_id: str = "admin") -> str:
    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        return issue_token(operator_id)


def _make_test_client() -> object:
    """Build a minimal test app client with auth + push routes."""
    from fastapi.testclient import TestClient

    from src.api.main import create_app
    from src.db.audit import AuditLog
    from src.db.operator_store import FakeOperatorStore
    from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
    from src.service.orchestrator import OrchestratorService

    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)
    forge = FakeForgePort()
    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=AuditLog(), allowlist=[]
    )
    op_store = FakeOperatorStore()
    op_store.seed("admin", hash_password("pass"))
    push_store = FakePushStore()

    app = create_app(service, operator_store=op_store, push_store=push_store)
    return TestClient(app, raise_server_exceptions=True), push_store


# ---------------------------------------------------------------------------
# 1. Subscription stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_subscription_stored_via_api() -> None:
    """POST /api/push/subscribe stores the subscription in the push store."""
    client, push_store = _make_test_client()
    assert isinstance(push_store, FakePushStore)
    token = _valid_token()

    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.post(  # type: ignore[union-attr]
            "/api/push/subscribe",
            json={
                "endpoint": "https://push.example.com/subscription/abc123",
                "keys": {"p256dh": "fake_p256dh_key", "auth": "fake_auth_key"},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 201  # type: ignore[union-attr]

    # Verify persisted in the store
    subs = await push_store.all_subscriptions()
    assert len(subs) == 1
    assert subs[0]["endpoint"] == "https://push.example.com/subscription/abc123"
    assert subs[0]["operator_id"] == "admin"


@pytest.mark.asyncio
async def test_push_subscription_removed_via_api() -> None:
    """DELETE /api/push/subscribe removes the subscription."""
    client, push_store = _make_test_client()
    assert isinstance(push_store, FakePushStore)
    token = _valid_token()
    endpoint = "https://push.example.com/subscription/to-remove"

    # Add subscription directly
    await push_store.add_subscription(
        "admin", endpoint, {"p256dh": "k", "auth": "a"}, "2024-01-01T00:00:00Z"
    )
    assert len(await push_store.all_subscriptions()) == 1

    import json as _json

    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.request(  # type: ignore[union-attr]
            "DELETE",
            "/api/push/subscribe",
            content=_json.dumps({"endpoint": endpoint}),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    assert resp.status_code == 200  # type: ignore[union-attr]

    subs = await push_store.all_subscriptions()
    assert len(subs) == 0


@pytest.mark.asyncio
async def test_push_subscriptions_listed() -> None:
    """GET /api/push/subscriptions returns the operator's subscriptions."""
    client, push_store = _make_test_client()
    assert isinstance(push_store, FakePushStore)
    token = _valid_token()

    await push_store.add_subscription(
        "admin", "https://ex.com/1", {"p256dh": "k1", "auth": "a1"}, "2024-01-01"
    )
    await push_store.add_subscription(
        "admin", "https://ex.com/2", {"p256dh": "k2", "auth": "a2"}, "2024-01-02"
    )

    with patch.dict("os.environ", {"OPERATOR_SECRET_KEY": _TEST_OPERATOR_SECRET}):
        resp = client.get(  # type: ignore[union-attr]
            "/api/push/subscriptions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200  # type: ignore[union-attr]
    body = resp.json()  # type: ignore[union-attr]
    assert len(body) == 2


# ---------------------------------------------------------------------------
# 2. FakePushStore contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_push_store_add_and_list() -> None:
    """FakePushStore stores and retrieves subscriptions."""
    store = FakePushStore()
    await store.add_subscription("op1", "https://a.example.com/push", {"p256dh": "k"}, "ts1")
    await store.add_subscription("op2", "https://b.example.com/push", {"p256dh": "k"}, "ts2")

    op1_subs = await store.list_subscriptions("op1")
    assert len(op1_subs) == 1
    assert op1_subs[0]["endpoint"] == "https://a.example.com/push"

    all_subs = await store.all_subscriptions()
    assert len(all_subs) == 2


@pytest.mark.asyncio
async def test_fake_push_store_remove() -> None:
    """FakePushStore removes a subscription by endpoint."""
    store = FakePushStore()
    ep = "https://push.example.com/sub/xyz"
    await store.add_subscription("op1", ep, {"p256dh": "k"}, "ts")
    await store.remove_subscription("op1", ep)

    subs = await store.all_subscriptions()
    assert len(subs) == 0


@pytest.mark.asyncio
async def test_fake_push_store_remove_noexist_is_noop() -> None:
    """FakePushStore.remove_subscription is a no-op for missing endpoints."""
    store = FakePushStore()
    # Should not raise
    await store.remove_subscription("op1", "https://does-not-exist.example.com/push")
    assert await store.all_subscriptions() == []


# ---------------------------------------------------------------------------
# 3. broadcast_push emits on escalation, promotion, approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_push_escalation_emitted() -> None:
    """broadcast_push sends to all subscriptions for escalation events."""
    store = FakePushStore()
    await store.add_subscription("op1", "https://push.example.com/sub1", {}, "ts")

    escalation_payload: dict[str, object] = {
        "type": "escalation",
        "repo": "acme/api",
        "issue_or_pr_number": 42,
        "title": "Protected path changed",
        "url": "https://github.com/acme/api/issues/42",
    }

    with patch("src.api.push.push_enabled", return_value=True), patch(
        "src.api.push.send_push", new_callable=AsyncMock, return_value=True
    ) as mock_send:
        sent = await broadcast_push(store, escalation_payload)

    assert sent == 1
    mock_send.assert_called_once()
    call_payload = mock_send.call_args[0][1]
    assert call_payload["type"] == "escalation"


@pytest.mark.asyncio
async def test_broadcast_push_promotion_emitted() -> None:
    """broadcast_push sends to all subscriptions for promotion events."""
    store = FakePushStore()
    await store.add_subscription("op1", "https://push.example.com/sub2", {}, "ts")

    promotion_payload: dict[str, object] = {
        "type": "promotion",
        "repo": "acme/api",
        "issue_or_pr_number": 10,
        "title": "Add feature X",
        "url": "https://github.com/acme/api/issues/10",
    }

    with patch("src.api.push.push_enabled", return_value=True), patch(
        "src.api.push.send_push", new_callable=AsyncMock, return_value=True
    ) as mock_send:
        sent = await broadcast_push(store, promotion_payload)

    assert sent == 1
    call_payload = mock_send.call_args[0][1]
    assert call_payload["type"] == "promotion"


@pytest.mark.asyncio
async def test_broadcast_push_approval_emitted() -> None:
    """broadcast_push sends to all subscriptions for approval events."""
    store = FakePushStore()
    await store.add_subscription("op1", "https://push.example.com/sub3", {}, "ts")

    approval_payload: dict[str, object] = {
        "type": "approval",
        "repo": "acme/api",
        "issue_or_pr_number": 99,
        "title": "PR ready to merge",
        "url": "https://github.com/acme/api/pull/99",
    }

    with patch("src.api.push.push_enabled", return_value=True), patch(
        "src.api.push.send_push", new_callable=AsyncMock, return_value=True
    ) as mock_send:
        sent = await broadcast_push(store, approval_payload)

    assert sent == 1
    call_payload = mock_send.call_args[0][1]
    assert call_payload["type"] == "approval"


@pytest.mark.asyncio
async def test_broadcast_push_multiple_subscribers() -> None:
    """broadcast_push sends to all registered subscriptions."""
    store = FakePushStore()
    await store.add_subscription("op1", "https://push.example.com/sub-a", {}, "ts")
    await store.add_subscription("op2", "https://push.example.com/sub-b", {}, "ts")

    payload: dict[str, object] = {"type": "escalation", "repo": "x/y"}

    with patch("src.api.push.push_enabled", return_value=True), patch(
        "src.api.push.send_push", new_callable=AsyncMock, return_value=True
    ) as mock_send:
        sent = await broadcast_push(store, payload)

    assert sent == 2
    assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_broadcast_push_no_subscribers_returns_zero() -> None:
    """broadcast_push returns 0 when no subscriptions are registered."""
    store = FakePushStore()
    payload: dict[str, object] = {"type": "escalation", "repo": "x/y"}

    with patch("src.api.push.push_enabled", return_value=True), patch(
        "src.api.push.send_push", new_callable=AsyncMock, return_value=True
    ) as mock_send:
        sent = await broadcast_push(store, payload)

    assert sent == 0
    mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 4. push_enabled guard
# ---------------------------------------------------------------------------


def test_push_enabled_false_when_keys_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """push_enabled() returns False when VAPID keys are not configured."""
    monkeypatch.delenv("PUSH_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("PUSH_VAPID_PUBLIC_KEY", raising=False)
    assert push_enabled() is False


def test_push_enabled_false_when_only_public_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """push_enabled() returns False when only the public key is set."""
    monkeypatch.delenv("PUSH_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("PUSH_VAPID_PUBLIC_KEY", "some-key")
    assert push_enabled() is False


def test_push_enabled_true_when_both_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """push_enabled() returns True when both VAPID keys are configured."""
    monkeypatch.setenv("PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("PUSH_VAPID_PUBLIC_KEY", "public-key")
    assert push_enabled() is True


# ---------------------------------------------------------------------------
# 5. send_push returns False when push not enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_push_returns_false_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """send_push returns False without error when VAPID keys are absent."""
    monkeypatch.delenv("PUSH_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("PUSH_VAPID_PUBLIC_KEY", raising=False)

    sub: dict[str, object] = {
        "operator_id": "op1",
        "endpoint": "https://push.example.com/sub",
        "keys": {"p256dh": "k", "auth": "a"},
        "created_at": "2024-01-01",
    }
    result = await send_push(sub, {"type": "test"})
    assert result is False
