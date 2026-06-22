"""FastAPI router for VAPID push subscription management.

Routes (all require JWT auth):
  POST   /api/push/subscribe       — register a push subscription
  DELETE /api/push/subscribe       — unregister current device
  GET    /api/push/subscriptions   — list operator's subscriptions
  POST   /api/push/test            — send a test push to current device
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from src.api.auth import require_auth
from src.api.push import get_vapid_public_key, push_enabled, send_push
from src.db.push_store import PushStorePort

# Re-export for type annotation
AuthPayload = dict[str, object]


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeRequest(BaseModel):
    endpoint: str
    keys: SubscriptionKeys
    created_at: str = ""


class UnsubscribeRequest(BaseModel):
    endpoint: str


def _make_push_router(push_store: PushStorePort) -> APIRouter:
    r = APIRouter()

    @r.post("/api/push/subscribe", status_code=status.HTTP_201_CREATED)
    async def subscribe(
        body: SubscribeRequest,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Register a VAPID push subscription for the current operator."""
        operator_id = str(auth.get("sub", "anonymous"))
        from datetime import UTC, datetime

        created_at = body.created_at or datetime.now(tz=UTC).isoformat()
        await push_store.add_subscription(
            operator_id=operator_id,
            endpoint=body.endpoint,
            keys={"p256dh": body.keys.p256dh, "auth": body.keys.auth},
            created_at=created_at,
        )
        return {"status": "subscribed", "endpoint": body.endpoint}

    @r.delete("/api/push/subscribe", status_code=status.HTTP_200_OK)
    async def unsubscribe(
        body: UnsubscribeRequest,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Remove the given push subscription for the current operator."""
        operator_id = str(auth.get("sub", "anonymous"))
        await push_store.remove_subscription(operator_id, body.endpoint)
        return {"status": "unsubscribed"}

    @r.get("/api/push/subscriptions")
    async def list_subscriptions(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[dict[str, object]]:
        """List all push subscriptions for the current operator."""
        operator_id = str(auth.get("sub", "anonymous"))
        subs = await push_store.list_subscriptions(operator_id)
        # Omit raw keys from listing (endpoint + created_at only)
        return [
            {"endpoint": str(s["endpoint"]), "created_at": str(s["created_at"])}
            for s in subs
        ]

    @r.post("/api/push/test")
    async def test_push(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Send a test push notification to all of the operator's subscriptions."""
        if not push_enabled():
            return {"status": "disabled", "sent": 0}
        operator_id = str(auth.get("sub", "anonymous"))
        subs = await push_store.list_subscriptions(operator_id)
        if not subs:
            return {"status": "no_subscriptions", "sent": 0}
        payload: dict[str, object] = {
            "type": "test",
            "title": "Test notification",
            "body": "Push notifications are working.",
        }
        sent = 0
        for sub in subs:
            ok = await send_push(sub, payload)
            if ok:
                sent += 1
        return {"status": "sent", "sent": sent, "total": len(subs)}

    @r.get("/api/push/vapid-public-key")
    async def vapid_public_key(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Return the VAPID application server public key for push subscription."""
        key = get_vapid_public_key()
        if not key:
            return {"enabled": False, "public_key": None}
        return {"enabled": True, "public_key": key}

    return r
