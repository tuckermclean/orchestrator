"""Webhook ingress — POST /api/webhook.

Validates GitHub's HMAC-SHA256 signature (X-Hub-Signature-256) before routing
the event to OrchestratorService.handle_event.  Deduplication of replayed
X-GitHub-Delivery IDs is handled by OrchestratorService itself.

Security invariants:
  - Invalid HMAC → 403 (no body inspection before signature check)
  - Missing signature header → 403
  - Replayed delivery ID → 200 with {"handled": false, "reason": "duplicate_delivery_id"}
  - WEBHOOK_SECRET sourced from environment only (never from request)
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, Header, HTTPException, Request

from src.service.orchestrator import OrchestratorService


def _make_webhook_router(
    service: OrchestratorService,
    webhook_secret: str,
) -> APIRouter:
    r = APIRouter()

    @r.post("/api/webhook")
    async def receive_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> dict[str, object]:
        body = await request.body()

        # --- HMAC-SHA256 validation (must run before any body inspection) ---
        if not x_hub_signature_256:
            raise HTTPException(status_code=403, detail="Missing X-Hub-Signature-256")

        expected_sig = (
            "sha256="
            + hmac.new(
                webhook_secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
        )
        if not hmac.compare_digest(expected_sig, x_hub_signature_256):
            raise HTTPException(status_code=403, detail="Invalid HMAC signature")

        # --- Route to service ---
        import json

        try:
            payload: dict[str, object] = json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        event_name = x_github_event or "unknown"
        delivery_id = x_github_delivery

        result = await service.handle_event(
            event_name=event_name,
            payload=payload,
            delivery_id=delivery_id,
        )
        return result

    return r
